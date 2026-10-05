#!/usr/bin/env python3
"""Bridge CLI for bravoric-ssh-client used by Pi Agent extension."""

import json
import os
import sys
from pathlib import Path
from typing import Any

from bravoric_ssh_client.config import default_config_path, load_config
from bravoric_ssh_client.mcp_server import BravoricMcp, PaneDiffTracker
from bravoric_ssh_client.ssh import adapter

# Cache persistente per il diff incrementale delle pane (FEATURE 3).
# Il bridge è un processo usa-e-getta: senza un file di stato ogni chiamata
# ripartirebbe dalla baseline e non risparmierebbe token.
PANE_DIFF_CACHE = Path(
    os.environ.get(
        "BRAVORIC_PANE_DIFF_CACHE",
        str(Path.home() / ".cache" / "bravoric" / "pane_diff.json"),
    )
)


def _json_or_text(raw: Any) -> dict[str, Any]:
    """Normalizza l'output di un metodo MCP (stringa JSON o testo) in un dict.

    I metodi che ritornano JSON (pane_info, paste, pane_diff, restart_foreground,
    list_windows_parsed) vengono parsati; quelli che ritornano testo semplice
    vengono convertiti in ``{"ok": …, "detail": …}``.
    """
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    ok = not text.lower().startswith(("errore", "attenzione"))
    return {"ok": ok, "detail": text}


def get_mcp() -> BravoricMcp:
    return BravoricMcp()


def cmd_hosts() -> dict[str, Any]:
    cfg = load_config(default_config_path())
    hosts = []
    for h in cfg.hosts:
        is_prod = (
            "prod" in h.alias.lower()
            or "production" in h.alias.lower()
            or any("prod" in str(t).lower() for t in getattr(h, "tags", []))
        )
        hosts.append(
            {
                "alias": h.alias,
                "host": h.host,
                "user": h.user or "",
                "tags": list(getattr(h, "tags", [])),
                "is_prod": is_prod,
            }
        )
    return {"ok": True, "hosts": hosts}


def cmd_ping(alias: str) -> dict[str, Any]:
    m = get_mcp()
    try:
        h = m._host(alias)
        ok, detail = adapter.tcp_ping(h, timeout=2.0)
        return {"ok": ok, "detail": detail}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def cmd_sessions(alias: str) -> dict[str, Any]:
    m = get_mcp()
    try:
        h = m._host(alias)
        res = adapter.list_tmux_sessions(h, m._ssh_cfg())
        if not res.ok:
            return {"ok": False, "sessions": [], "error": res.error or "tmux non attivo"}
        return {"ok": True, "sessions": res.sessions, "error": ""}
    except Exception as e:
        return {"ok": False, "sessions": [], "error": str(e)}


def cmd_create_session(alias: str, name: str, command: str = "") -> dict[str, Any]:
    m = get_mcp()
    try:
        raw = m.create_session(alias=alias, name=name, command=command)
        # m.create_session non solleva eccezioni: ritorna una stringa "errore: …"
        if isinstance(raw, str) and raw.strip().lower().startswith("errore"):
            return {"ok": False, "error": raw.strip()}
        return {"ok": True, "result": json.loads(raw) if raw.startswith("{") else raw}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_resize(alias: str, session: str, width: int, height: int) -> dict[str, Any]:
    m = get_mcp()
    try:
        h = m._host(alias)
        cmd = f"tmux resize-window -t {adapter._sh_quote(session)} -x {int(width)} -y {int(height)}"
        res = adapter.run_tmux_action(h, cmd, m._ssh_cfg())
        return {"ok": res.ok, "error": res.stderr or ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_capture(alias: str, session: str, lines: int = 50, escape: bool = True) -> dict[str, Any]:
    m = get_mcp()
    try:
        h = m._host(alias)
        cap = adapter.tmux_capture_pane(h, session, m._ssh_cfg(), lines=lines, escape=escape)
        if not cap.ok:
            return {"ok": False, "output": "", "error": cap.stderr or "capture fallita"}
        return {"ok": True, "output": cap.stdout or "", "error": ""}
    except Exception as e:
        return {"ok": False, "output": "", "error": str(e)}


def cmd_send(alias: str, session: str, text: str) -> dict[str, Any]:
    m = get_mcp()
    try:
        m.send_keys(alias=alias, session=session, text=text)
        m.send_enter(alias=alias, session=session)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_send_raw(alias: str, session: str, keys: str) -> dict[str, Any]:
    m = get_mcp()
    try:
        raw = m.send_raw(alias=alias, session=session, keys=keys)
        return {"ok": "errore" not in raw.lower(), "detail": raw}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_pane_command(alias: str, session: str) -> dict[str, Any]:
    m = get_mcp()
    try:
        detail = m.pane_command(alias=alias, session=session)
        ok = not detail.strip().lower().startswith("errore")
        return {"ok": ok, "command": detail.strip(), "error": "" if ok else detail.strip()}
    except Exception as e:
        return {"ok": False, "command": "", "error": str(e)}


def cmd_close(
    alias: str, session: str, method: str = "auto", force: bool = False
) -> dict[str, Any]:
    m = get_mcp()
    try:
        detail = m.close_foreground(alias=alias, session=session, method=method, force=force)
        low = detail.strip().lower()
        ok = not (low.startswith("errore") or low.startswith("attenzione"))
        return {"ok": ok, "closed": low.startswith("chiuso"), "detail": detail.strip()}
    except Exception as e:
        return {"ok": False, "closed": False, "detail": "", "error": str(e)}


# ---------- FEATURE 1: pane_info esteso ----------


def cmd_pane_info(alias: str, session: str) -> dict[str, Any]:
    """Processo, CWD, PID, titolo e dimensioni della pane attiva.

    Nota: ``tmux display-message -t <sessione inesistente>`` esce con codice 0 e
    campi vuoti, quindi si verifica esplicitamente che la pane sia reale per non
    riportare un falso successo.
    """
    m = get_mcp()
    try:
        res = _json_or_text(m.pane_info(alias=alias, session=session))
        if res.get("ok") and not res.get("command") and not res.get("pid"):
            return {
                "ok": False,
                "host": alias,
                "session": session,
                "error": f"sessione '{session}' non trovata o pane non leggibile",
            }
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- FEATURE 2: bracketed paste ----------


def cmd_paste(
    alias: str,
    session: str,
    content: str,
    bracketed: bool = True,
    enter: bool = False,
) -> dict[str, Any]:
    """Incolla testo multiriga/file nella sessione con bracketed paste."""
    m = get_mcp()
    try:
        return _json_or_text(
            m.paste(
                alias=alias,
                session=session,
                content=content,
                bracketed=bracketed,
                enter=enter,
            )
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- FEATURE 3: snapshot & diff output pane ----------


def _load_diff_cache() -> dict[str, list[str]]:
    try:
        data = json.loads(PANE_DIFF_CACHE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_diff_cache(data: dict[str, list[str]]) -> None:
    try:
        PANE_DIFF_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PANE_DIFF_CACHE.write_text(json.dumps(data))
    except Exception:
        pass


def cmd_pane_diff(
    alias: str, session: str, max_lines: int = 200, reset: bool = False
) -> dict[str, Any]:
    """Snapshot & diff incrementale dell'output di una pane (token saver).

    Al primo campionamento ritorna una preview (baseline); ai successivi SOLO le
    righe nuove (``new_content``), con lo stato salvato in un file di cache così
    il risparmio di token vale anche tra invocazioni separate del bridge.
    """
    m = get_mcp()
    try:
        h = m._host(alias)
        cap = adapter.tmux_capture_pane(h, session, m._ssh_cfg(), lines=int(max_lines))
        if not cap.ok:
            return {"ok": False, "error": (cap.stderr or "capture fallita").strip()}
        current = (cap.stdout or "").splitlines()
        key = f"{alias}|{session}"
        cache = _load_diff_cache()
        if reset:
            cache.pop(key, None)
        old = cache.get(key)
        cache[key] = current
        _save_diff_cache(cache)
        if old is None:
            return {
                "ok": True,
                "host": alias,
                "session": session,
                "is_first_sample": True,
                "total_lines": len(current),
                "new_lines": current[-15:],
                "diff_count": 0,
            }
        delta = PaneDiffTracker._sliding_window_delta(old, current)
        return {
            "ok": True,
            "host": alias,
            "session": session,
            "is_first_sample": False,
            "has_changes": len(delta) > 0,
            "diff_count": len(delta),
            "total_lines": len(current),
            "new_content": "\n".join(delta),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- FEATURE 4: navigazione & gestione multi-window ----------


def cmd_windows(alias: str, session: str) -> dict[str, Any]:
    """Finestre di una sessione in forma strutturata (indice, nome, attiva)."""
    m = get_mcp()
    try:
        return _json_or_text(m.list_windows_parsed(alias=alias, session=session))
    except Exception as e:
        return {"ok": False, "windows": [], "error": str(e)}


def cmd_select_window(alias: str, session: str, window_index: int | str) -> dict[str, Any]:
    m = get_mcp()
    try:
        detail = m.select_window(alias=alias, session=session, window_index=int(window_index))
        ok = not detail.strip().lower().startswith("errore")
        return {"ok": ok, "detail": detail.strip(), "error": "" if ok else detail.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_new_window(alias: str, session: str, name: str = "") -> dict[str, Any]:
    m = get_mcp()
    try:
        detail = m.new_window(alias=alias, session=session, name=name or None)
        ok = not detail.strip().lower().startswith("errore")
        return {"ok": ok, "detail": detail.strip(), "error": "" if ok else detail.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- FEATURE 5: restart_foreground ----------


def cmd_restart(alias: str, session: str, fallback_command: str = "") -> dict[str, Any]:
    """Chiude e rilancia il processo in primo piano (riavvio deterministico)."""
    m = get_mcp()
    try:
        return _json_or_text(
            m.restart_foreground(alias=alias, session=session, fallback_command=fallback_command)
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_run(alias: str, command: str) -> dict[str, Any]:
    m = get_mcp()
    try:
        raw = m.run_command(alias=alias, command=command)
        return {"ok": True, "result": json.loads(raw) if raw.startswith("{") else raw}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "comando mancante"}))
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    try:
        if cmd == "hosts":
            res = cmd_hosts()
        elif cmd == "ping" and len(args) >= 1:
            res = cmd_ping(args[0])
        elif cmd == "sessions" and len(args) >= 1:
            res = cmd_sessions(args[0])
        elif cmd == "create_session" and len(args) >= 2:
            command = args[2] if len(args) > 2 else ""
            res = cmd_create_session(args[0], args[1], command)
        elif cmd == "capture" and len(args) >= 2:
            lines = int(args[2]) if len(args) > 2 else 50
            res = cmd_capture(args[0], args[1], lines)
        elif cmd == "resize" and len(args) >= 4:
            res = cmd_resize(args[0], args[1], int(args[2]), int(args[3]))
        elif cmd == "send" and len(args) >= 3:
            res = cmd_send(args[0], args[1], args[2])
        elif cmd == "send_raw" and len(args) >= 3:
            res = cmd_send_raw(args[0], args[1], args[2])
        elif cmd == "pane_command" and len(args) >= 2:
            res = cmd_pane_command(args[0], args[1])
        elif cmd == "pane_info" and len(args) >= 2:
            res = cmd_pane_info(args[0], args[1])
        elif cmd == "paste" and len(args) >= 3:
            alias, session = args[0], args[1]
            bracketed, enter, use_stdin = True, False, False
            content_parts: list[str] = []
            for a in args[2:]:
                if a == "--bracketed":
                    bracketed = True
                elif a == "--no-bracketed":
                    bracketed = False
                elif a == "--enter":
                    enter = True
                elif a == "--stdin":
                    use_stdin = True
                else:
                    content_parts.append(a)
            if use_stdin:
                content = sys.stdin.read()
            elif content_parts:
                content = " ".join(content_parts)
            else:
                content = ""
            if not content:
                res = {
                    "ok": False,
                    "error": "contenuto mancante (passa il testo o usa --stdin)",
                }
            else:
                res = cmd_paste(alias, session, content, bracketed, enter)
        elif cmd == "pane_diff" and len(args) >= 2:
            max_lines, reset = 200, False
            for a in args[2:]:
                if a == "--reset":
                    reset = True
                else:
                    try:
                        max_lines = int(a)
                    except ValueError:
                        pass
            res = cmd_pane_diff(args[0], args[1], max_lines, reset)
        elif cmd == "windows" and len(args) >= 2:
            res = cmd_windows(args[0], args[1])
        elif cmd == "select_window" and len(args) >= 3:
            res = cmd_select_window(args[0], args[1], args[2])
        elif cmd == "new_window" and len(args) >= 2:
            name = args[2] if len(args) > 2 else ""
            res = cmd_new_window(args[0], args[1], name)
        elif cmd == "restart" and len(args) >= 2:
            fallback = args[2] if len(args) > 2 else ""
            res = cmd_restart(args[0], args[1], fallback)
        elif cmd == "close" and len(args) >= 2:
            method = args[2] if len(args) > 2 else "auto"
            force = len(args) > 3 and args[3].lower() in ("1", "true", "yes", "force")
            res = cmd_close(args[0], args[1], method, force)
        elif cmd == "run" and len(args) >= 2:
            res = cmd_run(args[0], args[1])
        else:
            res = {"ok": False, "error": f"comando sconosciuto o argomenti insufficienti: {cmd}"}
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
