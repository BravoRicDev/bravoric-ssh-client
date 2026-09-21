"""Server MCP per bravoric-ssh-client.

Espone il client come server MCP (Model Context Protocol) su stdio, così un
agente (es. opencode) può controllare host, sessioni tmux, comandi batch,
broadcast, tunnel e log audit esattamente come dalla TUI.

Avvio: ``bravoric-ssh-mcp`` (entry point) oppure ``python -m bravoric_ssh_client.mcp_server``.

Modello di lavoro: l'agente crea/guida sessioni tmux *detached* (send-keys,
capture-pane) e l'utente attach dal suo terminale o dalla TUI. L'attach
interattivo non è esposto perché non è compatibile con il trasporto stdio.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Logger strutturato MCP
mcp_logger = logging.getLogger("bravoric_ssh_client.mcp")


def _setup_logging():
    """Configura un handler JSON-like per il logger MCP."""
    if mcp_logger.handlers:
        return
    mcp_logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()

    class JsonFormatter(logging.Formatter):
        def format(self, record):
            log_data = {
                "timestamp": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "message": record.getMessage(),
            }
            if hasattr(record, "correlation_id"):
                log_data["correlation_id"] = record.correlation_id
            if hasattr(record, "tool"):
                log_data["tool"] = record.tool
            if hasattr(record, "host"):
                log_data["host"] = record.host
            return json.dumps(log_data, ensure_ascii=False)

    handler.setFormatter(JsonFormatter())
    mcp_logger.addHandler(handler)


_setup_logging()

from . import __version__
from .config import Config, Host, Tunnel, default_config_path, load_config
from .credentials.factory import resolve_password
from .snippets import Snippet, add_snippet, load_snippets, remove_snippet
from .ssh import adapter
from .ssh.audit import cfg_path_logs_dir, read_log_gz
from .ssh.broadcast import (
    run_snippet_on_hosts,
    run_snippet_on_hosts_tmux,
    session_slug,
)
from .ssh.tunnels import TunnelManager


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# Shell/processi che indicano "nessun processo in primo piano da chiudere".
# Sorgente unica: adapter.SHELL_COMMANDS (NB: 'node' NON è incluso: molte TUI
# come opencode girano proprio come 'node').
_SHELL_COMMANDS = adapter.SHELL_COMMANDS


def _wait_run_and_read(
    mcp: BravoricMcp, host: Host, command: str, timeout: int, prefix: str
) -> tuple[bool, str, str]:
    """Esegue ``command`` in tmux detached redirigendo l'output su un file.

    Ritorna (ok, output, errore). L'output viene scritto in un file temporaneo
    sul server; si attende il marcatore nel pane, poi si legge il file: così
    l'output è pulito (niente prompt/echo).
    """
    import time

    marker = f"__BRAVORIC_DONE_{int(time.time() * 1000)}__"
    name = session_slug(prefix, host.alias)
    logfile = f"/tmp/{name}.log"
    q = adapter._sh_quote
    res = adapter.run_tmux_action(host, f"tmux new -d -s {q(name)}", mcp._ssh_cfg())
    if not res.ok:
        return False, "", (res.stderr or "").strip() or "creazione sessione fallita"
    wrapped = f"{{ {command} ; }} > {q(logfile)} 2>&1 ; echo {q(marker)}"
    adapter.run_tmux_action(host, f"tmux send-keys -t {q(name)} -l {q(wrapped)}", mcp._ssh_cfg())
    adapter.run_tmux_action(host, f"tmux send-keys -t {q(name)} Enter", mcp._ssh_cfg())
    done_line = marker.strip()
    deadline = time.time() + int(timeout)
    while time.time() < deadline:
        cap = adapter.tmux_capture_pane(host, name, mcp._ssh_cfg(), lines=200)
        text = cap.stdout or ""
        if any(ln.strip() == done_line for ln in text.splitlines()):
            adapter.run_tmux_action(
                host, f"tmux kill-session -t {q(name)} 2>/dev/null", mcp._ssh_cfg()
            )
            from .ssh.broadcast import run_snippet_on_host

            read_res = run_snippet_on_host(
                host,
                f"cat {q(logfile)} 2>/dev/null ; rm -f {q(logfile)}",
                mcp._ssh_cfg(),
                timeout=30,
            )
            return True, (read_res.stdout or "").strip(), ""
        time.sleep(1)
    adapter.run_tmux_action(host, f"tmux kill-session -t {q(name)} 2>/dev/null", mcp._ssh_cfg())
    return False, "", f"timeout dopo {timeout}s"


class PaneDiffTracker:
    """Cache di snapshot delle pane per restituire solo le righe NUOVE (token saver).

    La chiave è ``(alias, session)``: al primo campionamento si salva una baseline;
    ai successivi si calcola il delta incrementale a finestra scorrevole, così
    l'agente riceve solo il contenuto aggiunto invece dell'intera pane.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], list[str]] = {}

    def reset(self, key: tuple[str, str] | None = None) -> None:
        """Azzera un singolo snapshot (o l'intera cache se ``key`` è None)."""
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    @staticmethod
    def _sliding_window_delta(old: list[str], new: list[str]) -> list[str]:
        """Righe nuove di ``new`` rispetto a ``old``.

        Caso comune (scroll): la coda di ``old`` ricompare in testa a ``new`` e il
        delta è ciò che segue. Se non c'è overlap diretto (redraw di una TUI,
        troncamento del buffer) si ripiega su un diff per blocchi (difflib).
        """
        if not old:
            return list(new)
        if not new:
            return []
        max_overlap = min(len(old), len(new))
        for k in range(max_overlap, 0, -1):
            if old[-k:] == new[:k]:
                return new[k:]
        import difflib

        delta: list[str] = []
        sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                delta.extend(new[j1:j2])
        return delta

    def capture(self, key: tuple[str, str], current_lines: list[str]) -> tuple[bool, list[str]]:
        """Aggiorna lo snapshot e ritorna ``(is_first_sample, delta)``."""
        old = self._cache.get(key)
        self._cache[key] = current_lines
        if old is None:
            return True, current_lines[-15:]
        return False, self._sliding_window_delta(old, current_lines)


class BravoricMcp:
    """Wrapper dell'MCPServer che registra i tool e detiene lo stato."""

    def __init__(self, config: Config | None = None):
        from mcp.server.mcpserver import MCPServer

        self.config = config
        self.tunnels = TunnelManager()
        self.pane_diffs = PaneDiffTracker()
        self._current_correlation_id: str | None = None
        self._metrics = {"tool_calls": 0, "errors": 0, "total_duration_ms": 0}
        self.server = MCPServer(
            "bravoric-ssh",
            title="bravoric-ssh-client",
            version=__version__,
            instructions=(
                "Controllo di host SSH e sessioni tmux: comandi, broadcast, tunnel, audit."
            ),
        )
        self._register()

    @contextmanager
    def correlation(self, tool_name: str, host_alias: str | None = None):
        """Context manager per tracciare le chiamate ai tool con correlation ID."""
        old_id = self._current_correlation_id
        self._current_correlation_id = old_id or uuid.uuid4().hex
        start_time = time.time()
        self._metrics["tool_calls"] += 1

        extra = {"correlation_id": self._current_correlation_id, "tool": tool_name}
        if host_alias:
            extra["host"] = host_alias

        mcp_logger.info(f"Inizio esecuzione tool: {tool_name}", extra=extra)
        try:
            yield self._current_correlation_id
        except Exception as e:
            self._metrics["errors"] += 1
            mcp_logger.error(f"Errore durante esecuzione tool {tool_name}: {e}", extra=extra)
            raise
        finally:
            duration = (time.time() - start_time) * 1000
            self._metrics["total_duration_ms"] += duration
            mcp_logger.info(
                f"Fine esecuzione tool: {tool_name} (durata: {duration:.2f}ms)", extra=extra
            )
            self._current_correlation_id = old_id

    def get_metrics(self) -> str:
        """Restituisce le metriche operative del server MCP."""
        return self._dump(self._metrics)

    # ---------- stato / helper ----------

    def _load_config(self) -> Config:
        if self.config is None:
            self.config = load_config(default_config_path())
        self._wire_tunnels_path()
        return self.config

    def _wire_tunnels_path(self) -> None:
        """Allinea il file di stato dei tunnel (condiviso con la TUI)."""
        from .ssh.tunnels import default_tunnels_path

        if self.config is None:
            return
        if self.config.tunnels_file:
            self.tunnels.state_path = Path(self.config.tunnels_file).expanduser()
        elif self.config.path and self.config.path.parent:
            self.tunnels.state_path = self.config.path.parent / "tunnels.json"
        else:
            self.tunnels.state_path = default_tunnels_path()

    def _host(self, alias: str) -> Host:
        host = self._load_config().host(alias)
        if host is None:
            raise ValueError(f"host '{alias}' non trovato")
        return host

    def _password_for(self, host: Host) -> str | None:
        try:
            return resolve_password(self._load_config(), host)
        except Exception:
            return None

    def _jump_for(self, host: Host) -> tuple[Host | None, str | None]:
        if not host.jump_host:
            return None, None
        jump = self._load_config().host(host.jump_host)
        if jump is None:
            return None, None
        return jump, self._password_for(jump)

    def _ssh_cfg(self) -> adapter.SshConfig:
        self._load_config()

        def pw(host: Host) -> str | None:
            return self._password_for(host)

        def jump(host: Host) -> Host | None:
            return self._jump_for(host)[0]

        return adapter.SshConfig(password_provider=pw, jump_resolver=jump)

    def _hosts(self, aliases: list[str]) -> list[Host]:
        out: list[Host] = []
        for a in aliases:
            try:
                out.append(self._host(a))
            except ValueError:
                continue
        return out

    def _dump(self, obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)

    # ---------- host ----------

    def get_status(self) -> str:
        """Diagnostica: config, provider credenziali, tmux, dir log, host."""
        cfg = self._load_config()
        provider = cfg.credential_provider or "keyring"
        keyring_ok: bool | None = None
        if provider == "keyring":
            try:
                import keyring

                keyring_ok = keyring.get_keyring() is not None
            except Exception:
                keyring_ok = False
        from .ssh.audit import cfg_path_logs_dir

        try:
            logs_dir = str(cfg_path_logs_dir(cfg))
        except OSError:
            logs_dir = "n/d"
        try:
            local = next((h for h in cfg.hosts if h.is_local()), None)
            tmux_local = adapter.tmux_present(local) if local else None
        except Exception:
            tmux_local = None
        return self._dump(
            {
                "version": __version__,
                "config_path": str(cfg.path) if cfg.path else None,
                "provider": provider,
                "keyring_available": keyring_ok,
                "plain_file": cfg.plain_file,
                "hosts": len(cfg.hosts),
                "tmux_local": tmux_local,
                "logs_dir": logs_dir,
                "audit_log": cfg.audit_log,
                "snippets_file": cfg.snippets_file,
                "tunnels_file": cfg.tunnels_file,
                "restart_after_ssh": cfg.restart_after_ssh,
                "transport": "stdio",
            }
        )

    def list_hosts(self) -> str:
        cfg = self._load_config()
        return self._dump(
            [
                {
                    "alias": h.alias,
                    "host": h.host,
                    "user": h.effective_user(),
                    "port": h.port,
                    "auth": h.auth or cfg.credential_provider,
                    "group": h.group or "",
                    "jump_host": h.jump_host or "",
                    "local": h.is_local(),
                }
                for h in cfg.hosts
            ]
        )

    def get_host(self, alias: str) -> str:
        h = self._host(alias)
        return self._dump(
            {
                "alias": h.alias,
                "host": h.host,
                "user": h.effective_user(),
                "port": h.port,
                "auth": h.auth,
                "group": h.group or "",
                "jump_host": h.jump_host or "",
                "local": h.is_local(),
                "tunnels": [
                    {
                        "name": t.name,
                        "kind": t.kind,
                        "local_port": t.local_port,
                        "remote_host": t.remote_host,
                        "remote_port": t.remote_port,
                        "bind": t.bind,
                    }
                    for t in h.tunnels
                ],
            }
        )

    def ping(self, alias: str, timeout: float = 2.0) -> str:
        ok, detail = adapter.tcp_ping(self._host(alias), timeout=timeout)
        return f"{alias}: {'OK' if ok else 'KO'} — {detail}"

    def tmux_present(self, alias: str) -> str:
        """True se tmux è installato sull'host (locale o remoto)."""
        present = adapter.tmux_present(self._host(alias), self._ssh_cfg())
        return f"{alias}: tmux {'presente' if present else 'assente'}"

    def ping_all(self, timeout: float = 2.0) -> str:
        import concurrent.futures

        hosts = self._load_config().hosts
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(adapter.tcp_ping, h, timeout): h.alias for h in hosts}
            out = []
            for fut in concurrent.futures.as_completed(futures):
                alias = futures[fut]
                ok, detail = fut.result()
                out.append(f"{alias}: {'OK' if ok else 'KO'} — {detail}")
        out.sort()
        return "\n".join(out)

    # ---------- dashboard / panoramica ----------

    def hosts_summary(self, timeout: float = 2.0) -> str:
        """Dashboard: per ogni host, raggiungibilità, tmux e numero di sessioni."""
        import concurrent.futures

        cfg = self._load_config()

        def probe(h: Host) -> dict[str, Any]:
            ok, detail = adapter.tcp_ping(h, timeout=timeout)
            if not ok:
                return {
                    "alias": h.alias,
                    "reachable": False,
                    "detail": detail,
                    "tmux": None,
                    "sessions": None,
                }
            try:
                present = adapter.tmux_present(h, self._ssh_cfg())
                res = adapter.list_tmux_sessions(h, self._ssh_cfg())
                sessions = res.sessions if res.ok else None
            except Exception as exc:  # noqa: BLE001
                return {
                    "alias": h.alias,
                    "reachable": True,
                    "detail": str(exc),
                    "tmux": None,
                    "sessions": None,
                }
            return {
                "alias": h.alias,
                "reachable": True,
                "detail": detail,
                "tmux": present,
                "sessions": sessions,
                "session_count": len(sessions) if sessions is not None else None,
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(probe, cfg.hosts))
        results.sort(key=lambda r: r["alias"])
        return self._dump(results)

    def list_sessions_all(self) -> str:
        """Elenca le sessioni tmux di tutti gli host in parallelo."""
        import concurrent.futures

        cfg = self._load_config()

        def probe(h: Host) -> dict[str, Any]:
            res = adapter.list_tmux_sessions(h, self._ssh_cfg())
            if not res.ok:
                return {"alias": h.alias, "ok": False, "error": res.error, "sessions": []}
            return {"alias": h.alias, "ok": True, "error": "", "sessions": res.sessions}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(probe, cfg.hosts))
        results.sort(key=lambda r: r["alias"])
        return self._dump(results)

    def find_in_sessions(self, alias: str, pattern: str, lines: int = 2000) -> str:
        """Cerca ``pattern`` (regex) nelle pane delle sessioni tmux di un host."""
        import re

        host = self._host(alias)
        res = adapter.list_tmux_sessions(host, self._ssh_cfg())
        if not res.ok:
            return f"errore: {res.error}"
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"errore: pattern non valido: {exc}"
        out: list[dict[str, Any]] = []
        for session in res.sessions:
            cap = adapter.tmux_capture_pane(host, session, self._ssh_cfg(), lines=int(lines))
            if not cap.ok:
                continue
            hits = []
            for lineno, text in enumerate(cap.stdout.splitlines(), start=1):
                if rx.search(text):
                    hits.append({"line": lineno, "text": text[:500]})
            if hits:
                out.append({"session": session, "matches": hits[:50], "total": len(hits)})
        return self._dump(out)

    def run_command_all(self, command: str, timeout: int = 60) -> str:
        """Esegue ``command`` su TUTTI gli host raggiungibili, in parallelo."""
        from .ssh.broadcast import run_snippet_on_hosts

        cfg = self._load_config()
        hosts = cfg.hosts
        results = run_snippet_on_hosts(hosts, command, self._ssh_cfg(), timeout=int(timeout))
        return self._dump(
            [
                {
                    "host": r.host_alias,
                    "ok": r.ok,
                    "exit_code": r.exit_code,
                    "stdout": r.stdout,
                    "stderr": r.stderr,
                    "error": r.error,
                }
                for r in results
            ]
        )

    def run_and_wait(self, alias: str, command: str, timeout: int = 300) -> str:
        """Esegue ``command`` in una sessione tmux detached e attende la fine.

        L'output viene scritto su un file sul server (via redirect) e letto a
        completamento (marcatore): niente prompt/echo nell'output. La sessione
        viene rimossa al termine.
        """
        ok, output, error = _wait_run_and_read(self, self._host(alias), command, timeout, "wait")
        if not ok:
            return f"errore: {error}" + (f"\n{output}" if output else "")
        return output or "(comando completato, nessun output)"

    def broadcast_wait(self, aliases: list[str], command: str, timeout: int = 300) -> str:
        """Broadcast in modalità tmux e attesa di completamento su tutti gli host."""
        import concurrent.futures

        hosts = self._hosts(aliases)

        def run(h: Host) -> dict[str, Any]:
            ok, output, error = _wait_run_and_read(self, h, command, timeout, "bwait")
            return {"host": h.alias, "ok": ok, "error": error, "output": output}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, hosts))
        results.sort(key=lambda r: r["host"])
        return self._dump(results)

    # ---------- trasferimento file (scp/SFTP) ----------

    def sftp_download(self, alias: str, remote: str, local: str) -> str:
        """Scarica un file da un host in locale (scp, password dal provider)."""
        from .ssh import file_ops

        host = self._host(alias)
        local_path = Path(local).expanduser()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        res = file_ops.download(host, remote, str(local_path), self._password_for(host))
        return self._dump(
            {
                "host": alias,
                "remote": remote,
                "local": str(local_path),
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def sftp_upload(self, alias: str, local: str, remote: str) -> str:
        """Carica un file locale su un host (scp, password dal provider)."""
        from .ssh import file_ops

        host = self._host(alias)
        local_path = Path(local).expanduser()
        res = file_ops.upload(host, str(local_path), remote, self._password_for(host))
        return self._dump(
            {
                "host": alias,
                "local": str(local_path),
                "remote": remote,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def transfer_file(
        self, src_alias: str, src_remote: str, dst_alias: str, dst_remote: str
    ) -> str:
        """Trasferisce un file tra due host passando da un file temporaneo locale.

        Utile quando i due host non si raggiungono direttamente: scarica dal
        sorgente, carica sulla destinazione e riporta dimensione e md5.
        """
        import hashlib
        import tempfile

        from .ssh import file_ops

        src = self._host(src_alias)
        dst = self._host(dst_alias)
        with tempfile.TemporaryDirectory(prefix="bravoric-xfer-") as tmpdir:
            tmp = Path(tmpdir) / Path(src_remote).name
            down = file_ops.download(src, src_remote, str(tmp), self._password_for(src))
            if not down.ok:
                return self._dump(
                    {"ok": False, "stage": "download", "host": src_alias, "stderr": down.stderr}
                )
            size = tmp.stat().st_size
            md5 = hashlib.md5(tmp.read_bytes()).hexdigest()
            up = file_ops.upload(dst, str(tmp), dst_remote, self._password_for(dst))
            if not up.ok:
                return self._dump(
                    {"ok": False, "stage": "upload", "host": dst_alias, "stderr": up.stderr}
                )
        return self._dump(
            {
                "ok": True,
                "src": f"{src_alias}:{src_remote}",
                "dst": f"{dst_alias}:{dst_remote}",
                "bytes": size,
                "md5": md5,
            }
        )

    # ---------- nuovi tool SFTP nativi ----------

    def sftp_list(self, alias: str, path: str) -> str:
        """Elenca il contenuto di una directory remota via SFTP."""
        from .ssh import file_ops

        host = self._host(alias)
        res = file_ops.sftp_list(host, path, self._password_for(host))
        return self._dump(
            {
                "host": alias,
                "path": path,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def sftp_mkdir(self, alias: str, path: str) -> str:
        """Crea una directory remota via SFTP."""
        from .ssh import file_ops

        host = self._host(alias)
        res = file_ops.sftp_mkdir(host, path, self._password_for(host))
        return self._dump(
            {
                "host": alias,
                "path": path,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def sftp_rm(self, alias: str, path: str) -> str:
        """Rimuove un file o directory remota via SFTP."""
        from .ssh import file_ops

        host = self._host(alias)
        res = file_ops.sftp_rm(host, path, self._password_for(host))
        return self._dump(
            {
                "host": alias,
                "path": path,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def sftp_rename(self, alias: str, old_path: str, new_path: str) -> str:
        """Rinomina o sposta un file/directory remota via SFTP."""
        from .ssh import file_ops

        host = self._host(alias)
        res = file_ops.sftp_rename(host, old_path, new_path, self._password_for(host))
        return self._dump(
            {
                "host": alias,
                "old": old_path,
                "new": new_path,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def sftp_get(self, alias: str, remote: str, local: str, recursive: bool = False) -> str:
        """Scarica file o directory via SFTP (supporta ricorsione)."""
        from .ssh import file_ops

        host = self._host(alias)
        local_path = Path(local).expanduser()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        res = file_ops.sftp_get(host, remote, str(local_path), self._password_for(host), recursive=recursive)
        return self._dump(
            {
                "host": alias,
                "remote": remote,
                "local": str(local_path),
                "recursive": recursive,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def sftp_put(self, alias: str, local: str, remote: str, recursive: bool = False) -> str:
        """Carica file o directory via SFTP (supporta ricorsione)."""
        from .ssh import file_ops

        host = self._host(alias)
        local_path = Path(local).expanduser()
        res = file_ops.sftp_put(host, str(local_path), remote, self._password_for(host), recursive=recursive)
        return self._dump(
            {
                "host": alias,
                "local": str(local_path),
                "remote": remote,
                "recursive": recursive,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def sftp_batch(self, alias: str, commands: list[str]) -> str:
        """Esegue comandi SFTP arbitrari in batch."""
        from .ssh import file_ops

        host = self._host(alias)
        res = file_ops.sftp_batch(host, commands, self._password_for(host))
        return self._dump(
            {
                "host": alias,
                "commands": commands,
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def transfer_file_direct(
        self, src_alias: str, src_path: str, dst_alias: str, dst_path: str
    ) -> str:
        """Trasferisce un file direttamente tra due host remoti usando SFTP streaming (senza staging locale)."""
        from .ssh import file_ops

        src = self._host(src_alias)
        dst = self._host(dst_alias)
        res = file_ops.transfer_file_direct(
            src,
            src_path,
            dst,
            dst_path,
            self._password_for(src),
            self._password_for(dst),
        )
        return self._dump(
            {
                "src": f"{src_alias}:{src_path}",
                "dst": f"{dst_alias}:{dst_path}",
                "ok": res.ok,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    # ---------- sessioni tmux ----------

    def list_sessions(self, alias: str) -> str:
        res = adapter.list_tmux_sessions(self._host(alias), self._ssh_cfg())
        if not res.ok:
            return f"errore: {res.error}"
        return self._dump(res.sessions)

    def create_session(
        self,
        alias: str,
        name: str | None = None,
        command: str | None = None,
        cwd: str | None = None,
    ) -> str:
        host = self._host(alias)
        name = name or session_slug("mcp", alias)
        q = adapter._sh_quote
        tmux_cmd = f"tmux new -d -s {q(name)}"
        if cwd:
            tmux_cmd += f" -c {q(cwd)}"
        if command:
            tmux_cmd += f" {q(command)}"
        res = adapter.run_tmux_action(host, tmux_cmd, self._ssh_cfg())
        if not res.ok:
            return f"errore: {(res.stderr or '').strip() or f'exit {res.ok}'}"
        return f"sessione '{name}' creata su {alias} (detached)"

    def session_details(self, alias: str, name: str) -> str:
        res = adapter.tmux_session_details(self._host(alias), name, self._ssh_cfg())
        if not res.ok:
            return f"errore: {(res.stderr or '').strip()}"
        return res.stdout.strip() or "(nessun dettaglio)"

    def rename_session(self, alias: str, old_name: str, new_name: str) -> str:
        res = adapter.tmux_rename_session(self._host(alias), old_name, new_name, self._ssh_cfg())
        return (
            f"sessione rinominata in '{new_name}'"
            if res.ok
            else f"errore: {(res.stderr or '').strip()}"
        )

    def kill_session(self, alias: str, name: str) -> str:
        res = adapter.tmux_kill_session(self._host(alias), name, self._ssh_cfg())
        return f"sessione '{name}' terminata" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def kill_server(self, alias: str) -> str:
        res = adapter.tmux_kill_server(self._host(alias), self._ssh_cfg())
        return "server tmux terminato" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def detach_clients(self, alias: str, name: str) -> str:
        res = adapter.tmux_detach_clients(self._host(alias), name, self._ssh_cfg())
        return f"client staccati da '{name}'" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def list_windows(self, alias: str, session: str) -> str:
        res = adapter.tmux_list_windows(self._host(alias), session, self._ssh_cfg())
        if not res.ok:
            return f"errore: {(res.stderr or '').strip()}"
        return res.stdout.strip() or "(nessuna finestra)"

    def list_windows_parsed(self, alias: str, session: str) -> str:
        """Elenca le finestre in forma strutturata (indice, nome, attiva, pane)."""
        res = adapter.tmux_list_windows_parsed(self._host(alias), session, self._ssh_cfg())
        if not res.ok:
            return self._dump({"ok": False, "host": alias, "session": session, "error": res.error})
        return self._dump(
            {
                "ok": True,
                "host": alias,
                "session": session,
                "count": len(res.windows),
                "windows": [
                    {
                        "index": w.index,
                        "name": w.name,
                        "active": w.active,
                        "pane_count": w.pane_count,
                        "layout": w.layout,
                    }
                    for w in res.windows
                ],
            }
        )

    def select_window(self, alias: str, session: str, window_index: int) -> str:
        """Rende attiva (visibile) una finestra della sessione."""
        res = adapter.tmux_select_window(self._host(alias), session, window_index, self._ssh_cfg())
        return (
            f"finestra {window_index} attivata in '{session}'"
            if res.ok
            else f"errore: {(res.stderr or '').strip()}"
        )

    def new_window(self, alias: str, session: str, name: str | None = None) -> str:
        res = adapter.tmux_new_window(self._host(alias), session, name, self._ssh_cfg())
        return (
            f"finestra creata in '{session}'" if res.ok else f"errore: {(res.stderr or '').strip()}"
        )

    def rename_window(self, alias: str, session: str, window_id: str, new_name: str) -> str:
        res = adapter.tmux_rename_window(
            self._host(alias), session, window_id, new_name, self._ssh_cfg()
        )
        return "finestra rinominata" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def kill_window(self, alias: str, session: str, window_id: str) -> str:
        res = adapter.tmux_kill_window(self._host(alias), session, window_id, self._ssh_cfg())
        return "finestra chiusa" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def capture_pane(
        self, alias: str, session: str, lines: int = 200, window: str | None = None
    ) -> str:
        res = adapter.tmux_capture_pane(
            self._host(alias), session, self._ssh_cfg(), lines=int(lines), window=window
        )
        if not res.ok:
            return f"errore: {(res.stderr or '').strip()}"
        return res.stdout or "(pane vuoto)"

    def pane_diff(self, alias: str, session: str, max_lines: int = 200, reset: bool = False) -> str:
        """Snapshot & diff incrementale dell'output di una pane (token saver).

        Al primo campionamento restituisce una preview (baseline). Ai successivi
        restituisce SOLO le righe nuove (``new_content``) con ``diff_count``, così
        il monitoraggio di build/server log/TUI non ricarica centinaia di token.
        ``reset=True`` azzera la baseline per quella pane.
        """
        host = self._host(alias)
        key = (host.alias, session)
        if reset:
            self.pane_diffs.reset(key)
        cap = adapter.tmux_capture_pane(host, session, self._ssh_cfg(), lines=int(max_lines))
        if not cap.ok:
            return self._dump(
                {"ok": False, "error": (cap.stderr or "").strip() or "capture fallita"}
            )
        current = (cap.stdout or "").splitlines()
        is_first, delta = self.pane_diffs.capture(key, current)
        if is_first:
            return self._dump(
                {
                    "ok": True,
                    "host": alias,
                    "session": session,
                    "is_first_sample": True,
                    "total_lines": len(current),
                    "new_lines": delta,
                    "diff_count": 0,
                }
            )
        return self._dump(
            {
                "ok": True,
                "host": alias,
                "session": session,
                "is_first_sample": False,
                "has_changes": len(delta) > 0,
                "diff_count": len(delta),
                "total_lines": len(current),
                "new_content": "\n".join(delta),
            }
        )

    def send_keys(self, alias: str, session: str, text: str, enter: bool = False) -> str:
        res = adapter.tmux_send_keys(
            self._host(alias), session, text, self._ssh_cfg(), enter=bool(enter)
        )
        return "inviato" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def send_input(
        self,
        alias: str,
        session: str,
        text: str = "",
        enter: bool = True,
        mode: str = "auto",
        bracketed: bool = True,
        settle_delay: float = 0.0,
        capture_lines: int = 0,
    ) -> str:
        """Invia input a una sessione tmux con gestione universale di invio e multiriga.

        Tool standard e universale raccomandato per inviare qualsiasi input a tmux:
        - Invia Invio (Enter) di default (disattivabile con ``enter=False``).
        - Con ``mode="auto"`` usa automaticamente bracketed paste se il testo ha newline,
          tabulazioni, lunghezza > 100 caratteri o caratteri di controllo, altrimenti usa send-keys atomico.
        - ``mode`` può essere forzato a "auto", "paste" o "keys".
        - ``settle_delay``: attesa opzionale in secondi prima di Enter (utile per TUI lente).
        - ``capture_lines``: se > 0, concatena e restituisce immediatamente le ultime N righe
          della pane in un solo roundtrip SSH.
        """
        host = self._host(alias)
        mode_val = (mode or "auto").strip().lower()
        if mode_val not in ("auto", "paste", "keys"):
            return self._dump(
                {
                    "ok": False,
                    "error": f"mode non valido: '{mode}'. I valori ammessi sono 'auto', 'paste', 'keys'.",
                }
            )

        text_str = text or ""
        res = adapter.tmux_send_input(
            host,
            session,
            text_str,
            self._ssh_cfg(),
            enter=bool(enter),
            mode=mode_val,
            bracketed=bool(bracketed),
            settle_delay=float(settle_delay or 0.0),
            capture_lines=int(capture_lines or 0),
        )
        if not res.ok:
            return self._dump(
                {"ok": False, "error": (res.stderr or "").strip() or "send_input fallito"}
            )

        lines_count = len(text_str.splitlines()) if text_str else 0
        data: dict[str, Any] = {
            "ok": True,
            "host": alias,
            "session": session,
            "mode": res.mode or "keys",
            "chars": len(text_str),
            "lines": lines_count,
            "enter": bool(enter),
        }
        if capture_lines > 0:
            data["captured_lines"] = res.stdout.strip()
        return self._dump(data)

    def send_line(
        self,
        alias: str,
        session: str,
        text: str = "",
        capture_lines: int = 0,
    ) -> str:
        """Invia una riga di comando o testo con Enter automatico (alias di send_input)."""
        return self.send_input(
            alias=alias,
            session=session,
            text=text,
            enter=True,
            capture_lines=capture_lines,
        )

    def send_enter(self, alias: str, session: str) -> str:
        res = adapter.tmux_send_enter(self._host(alias), session, self._ssh_cfg())
        return "Invio inviato" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def send_raw(self, alias: str, session: str, keys: str) -> str:
        res = adapter.tmux_send_raw(self._host(alias), session, keys, self._ssh_cfg())
        return "tasti inviati" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def paste(
        self,
        alias: str,
        session: str,
        content: str,
        bracketed: bool = True,
        enter: bool = False,
    ) -> str:
        """Incolla testo multiriga/file nella sessione con bracketed paste.

        Usa un buffer tmux dedicato (niente send-keys riga per riga): l'indentazione
        e i caratteri speciali arrivano intatti e le TUI ricevono i marcatori di
        bracketed paste. Con ``enter=True`` invia anche Invio dopo l'incolla.
        """
        host = self._host(alias)
        res = adapter.tmux_paste_buffer(
            host, session, content, self._ssh_cfg(), bracketed=bool(bracketed)
        )
        if not res.ok:
            return self._dump({"ok": False, "error": (res.stderr or "").strip() or "paste fallita"})
        if enter:
            adapter.tmux_send_enter(host, session, self._ssh_cfg())
        return self._dump(
            {
                "ok": True,
                "host": alias,
                "session": session,
                "chars": len(content),
                "lines": content.count("\n") + 1,
                "bracketed": bool(bracketed),
                "enter": bool(enter),
            }
        )

    # ---------- chiusura del processo in primo piano ----------

    def _pane_current_command(self, host: Host, session: str) -> str:
        """Legge il processo in primo piano (nome base, senza path)."""
        res = adapter.tmux_pane_command(host, session, self._ssh_cfg())
        if not res.ok:
            return ""
        raw = (res.stdout or "").strip().splitlines()
        if not raw:
            return ""
        return raw[0].strip().lstrip("-").split("/")[-1]

    @staticmethod
    def _is_shell(cmd: str) -> bool:
        return cmd.strip().lstrip("-").split("/")[-1] in _SHELL_COMMANDS

    def pane_command(self, alias: str, session: str) -> str:
        """Mostra il processo in primo piano nella pane attiva (es. 'opencode', 'node')."""
        cur = self._pane_current_command(self._host(alias), session)
        if not cur:
            return f"errore: impossibile leggere il processo in primo piano su '{session}'"
        return cur

    def pane_info(self, alias: str, session: str) -> str:
        """Processo, CWD, PID, titolo e dimensioni della pane attiva in una sola chiamata."""
        info = adapter.tmux_pane_info(self._host(alias), session, self._ssh_cfg())
        if not info.ok:
            return self._dump({"ok": False, "host": alias, "session": session, "error": info.error})
        return self._dump(
            {
                "ok": True,
                "host": alias,
                "session": session,
                "command": info.command,
                "cwd": info.cwd,
                "pid": info.pid,
                "title": info.title,
                "size": f"{info.width}x{info.height}",
                "width": info.width,
                "height": info.height,
                "is_shell": info.is_shell,
            }
        )

    def close_foreground(
        self, alias: str, session: str, method: str = "auto", force: bool = False
    ) -> str:
        """Chiude il processo in primo piano in una sessione tmux (TUI o comando).

        Legge ``#{pane_current_command}``: se è solo una shell non fa nulla
        (a meno di ``force=True``). Altrimenti invia una sequenza di chiusura e
        ricontrolla dopo ogni passo.

        ``method``: ``auto`` (escalation C-c, C-c, C-d) | ``sigint`` (C-c) |
        ``sigint2`` (C-c C-c) | ``eof`` (C-d) | ``exit`` (/exit + Enter).
        """
        import time

        host = self._host(alias)
        cfg = self._ssh_cfg()

        def pane_cmd() -> str:
            return self._pane_current_command(host, session)

        before = pane_cmd()
        if not before:
            return f"errore: sessione '{session}' non trovata o pane non leggibile"
        if self._is_shell(before) and not force:
            return f"nessun processo in primo piano da chiudere (shell attiva: {before})"

        if method == "auto":
            steps: list[tuple[str, str]] = [("key", "C-c"), ("key", "C-c"), ("key", "C-d")]
        else:
            table: dict[str, list[tuple[str, str]]] = {
                "sigint": [("key", "C-c")],
                "sigint2": [("key", "C-c"), ("key", "C-c")],
                "eof": [("key", "C-d")],
                "exit": [("text", "/exit")],
            }
            steps_opt: list[tuple[str, str]] | None = table.get(method)
            if steps_opt is None:
                return f"errore: metodo sconosciuto '{method}' (usa auto|sigint|sigint2|eof|exit)"
            steps = steps_opt

        done: list[str] = []
        for kind, val in steps:
            if kind == "key":
                adapter.tmux_send_raw(host, session, val, cfg)
                done.append(val)
            else:
                adapter.tmux_send_keys(host, session, val, cfg)
                adapter.tmux_send_enter(host, session, cfg)
                done.append(f"{val}+Enter")
            time.sleep(0.7)
            now = pane_cmd()
            if not now or self._is_shell(now):
                return f"chiuso '{before}' nella sessione '{session}' (sequenza: {', '.join(done)})"

        now = pane_cmd()
        if now and not self._is_shell(now):
            return (
                f"attenzione: '{now}' risulta ancora attivo in '{session}' dopo: "
                f"{', '.join(done)}. Prova un altro metodo o invia i tasti manualmente."
            )
        return f"chiuso '{before}' nella sessione '{session}' (sequenza: {', '.join(done)})"

    def _try_recover_last_command(self, host: Host, session: str, cfg) -> str | None:
        """Tenta di recuperare l'ultimo comando dalla history della shell remota."""
        import time

        # Prova bash history, zsh history, fc -ln -1
        cmds = [
            "tail -n 1 ~/.bash_history 2>/dev/null",
            "tail -n 1 ~/.zsh_history 2>/dev/null | sed 's/^: [0-9]*:[0-9]*;//'",
            "fc -ln -1 2>/dev/null",
        ]
        for cmd in cmds:
            try:
                res = adapter.run_tmux_action(
                    host,
                    f"tmux send-keys -t {adapter._sh_quote(session)} -l {adapter._sh_quote(cmd)}",
                    cfg,
                )
                if not res.ok:
                    continue
                adapter.run_tmux_action(
                    host, f"tmux send-keys -t {adapter._sh_quote(session)} Enter", cfg
                )
                time.sleep(0.3)
                cap = adapter.tmux_capture_pane(host, session, cfg, lines=5)
                if cap.ok:
                    lines = cap.stdout.strip().splitlines()
                    for line in reversed(lines):
                        line = line.strip()
                        if (
                            line
                            and not line.startswith(cmd)
                            and not line.startswith("tail")
                            and not line.startswith("fc")
                        ):
                            return line
            except Exception:
                continue
        return None

    def restart_foreground(self, alias: str, session: str, fallback_command: str = "") -> str:
        """Chiude il processo in primo piano e lo rilancia (riavvio deterministico).

        Unisce ``close_foreground`` e il rilancio in una sola operazione sicura per
        ripristinare processi incastrati (agente crashato, server bloccato). Il
        comando da rilanciare è ``fallback_command``; se vuoto si rilancia lo stesso
        processo rilevato prima della chiusura. Se la pane era già una shell, prova
        a recuperare l'ultimo comando dalla history della shell; se fallisce richiede
        un ``fallback_command`` esplicito.
        """
        import time

        host = self._host(alias)
        cfg = self._ssh_cfg()

        # 1) rileva il processo attuale
        info = adapter.tmux_pane_info(host, session, cfg)
        if not info.ok:
            # Distingue sessione mancante da errore pane
            if "not found" in info.error.lower() or "does not exist" in info.error.lower():
                return self._dump(
                    {
                        "ok": False,
                        "host": alias,
                        "session": session,
                        "error": f"sessione '{session}' non esistente su '{alias}'",
                    }
                )
            return self._dump(
                {
                    "ok": False,
                    "host": alias,
                    "session": session,
                    "error": f"Impossibile analizzare la pane: {info.error}",
                }
            )
        previous = info.command
        fallback = (fallback_command or "").strip()

        # 2) pane già idle (shell): prova a recuperare l'ultimo comando dalla history
        if info.is_shell:
            if not fallback:
                recovered = self._try_recover_last_command(host, session, cfg)
                if recovered:
                    fallback = recovered
                else:
                    return self._dump(
                        {
                            "ok": False,
                            "host": alias,
                            "session": session,
                            "error": (
                                "Nessun processo attivo, history shell vuota/irraggiungibile, "
                                "e nessun fallback_command specificato"
                            ),
                        }
                    )
            adapter.tmux_send_input(host, session, fallback, cfg, enter=True)
            return self._dump(
                {
                    "ok": True,
                    "host": alias,
                    "session": session,
                    "restarted": fallback,
                    "method": "direct_launch",
                }
            )

        # Guard: previous non deve essere vuoto
        if not previous:
            return self._dump(
                {
                    "ok": False,
                    "host": alias,
                    "session": session,
                    "error": "Impossibile determinare il comando precedente (pane_command vuoto)",
                }
            )

        command = fallback or previous

        # 3) chiudi con la funzione verificata (escalation C-c, C-c, C-d)
        close_msg = self.close_foreground(alias, session, method="auto")
        if close_msg.strip().lower().startswith("errore"):
            return self._dump(
                {"ok": False, "host": alias, "session": session, "error": close_msg.strip()}
            )

        # 4) attesa attiva del rilascio del prompt shell (max 5 s)
        #    Verifica anche che la sessione esista ancora
        deadline = time.time() + 5.0
        prompt_ready = False
        session_gone = False
        while time.time() < deadline:
            # Verifica che la sessione esista ancora
            exists = adapter.run_tmux_action(
                host, f"tmux has-session -t {adapter._sh_quote(session)} 2>/dev/null", cfg
            )
            if not exists.ok:
                session_gone = True
                break
            cur = adapter.tmux_pane_info(host, session, cfg)
            if cur.ok and cur.is_shell:
                prompt_ready = True
                break
            time.sleep(0.5)
        if session_gone:
            return self._dump(
                {
                    "ok": False,
                    "host": alias,
                    "session": session,
                    "closed": previous,
                    "error": "La sessione tmux è scomparsa durante la chiusura",
                }
            )
        if not prompt_ready:
            return self._dump(
                {
                    "ok": False,
                    "host": alias,
                    "session": session,
                    "closed": previous,
                    "error": "Il processo precedente non ha liberato la shell in tempo",
                }
            )

        # 5) rilancio
        adapter.tmux_send_input(host, session, command, cfg, enter=True)
        return self._dump(
            {
                "ok": True,
                "host": alias,
                "session": session,
                "closed": previous,
                "restarted": command,
                "status": "success",
            }
        )

    def run_command(self, alias: str, command: str, timeout: int = 60) -> str:
        from .ssh.broadcast import run_snippet_on_host

        res = run_snippet_on_host(self._host(alias), command, self._ssh_cfg(), timeout=int(timeout))
        if res.error:
            return f"{alias}: ERRORE {res.error}"
        return self._dump(
            {
                "host": alias,
                "ok": res.ok,
                "exit_code": res.exit_code,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
        )

    def run_command_many(
        self, aliases: list[str], command: str, timeout: int = 60, max_workers: int = 8
    ) -> str:
        hosts = self._hosts(aliases)
        results = run_snippet_on_hosts(
            hosts, command, self._ssh_cfg(), timeout=int(timeout), max_workers=int(max_workers)
        )
        return self._dump(
            [
                {
                    "host": r.host_alias,
                    "ok": r.ok,
                    "exit_code": r.exit_code,
                    "stdout": r.stdout,
                    "stderr": r.stderr,
                    "error": r.error,
                }
                for r in results
            ]
        )

    # ---------- ispezione e file ad alta efficienza ----------

    def search_files(
        self,
        alias: str,
        path: str = ".",
        pattern: str = "*",
        mode: str = "compact",
        text: str = "",
        max_results: int = 50,
        include_hidden: bool = False,
        timeout: int = 30,
    ) -> str:
        """Cerca file sul filesystem dell'host in modo strutturato e leggero.

        mode:
          - 'compact': array di soli percorsi relativi (1 riga per file, minimo consumo token).
          - 'metadata': array con percorsi, dimensione in byte e timestamp di modifica.
          - 'grep': cerca file che corrispondono al pattern e contengono il testo specificato.
        """
        from .ssh.inspection import remote_search_files

        host = self._host(alias)
        res = remote_search_files(
            host,
            self._ssh_cfg(),
            path=path,
            pattern=pattern,
            mode=mode,
            text=text,
            max_results=int(max_results),
            include_hidden=bool(include_hidden),
            timeout=int(timeout),
        )
        return self._dump(res)

    def read_file(
        self,
        alias: str,
        path: str,
        offset: int = 1,
        limit: int = 100,
        unit: str = "lines",
        timeout: int = 30,
    ) -> str:
        """Legge una porzione mirata di un file (lines o bytes) per preservare il contesto."""
        from .ssh.inspection import remote_read_file

        host = self._host(alias)
        res = remote_read_file(
            host,
            self._ssh_cfg(),
            path=path,
            offset=int(offset),
            limit=int(limit),
            unit=unit,
            timeout=int(timeout),
        )
        return self._dump(res)

    def git_status(
        self,
        alias: str,
        path: str = ".",
        timeout: int = 30,
    ) -> str:
        """Restituisce lo stato sintetico di un repository git in JSON compatto."""
        from .ssh.inspection import remote_git_status

        host = self._host(alias)
        res = remote_git_status(
            host,
            self._ssh_cfg(),
            path=path,
            timeout=int(timeout),
        )
        return self._dump(res)

    def host_health(
        self,
        alias: str,
        timeout: int = 30,
    ) -> str:
        """Restituisce carico CPU, RAM libera, spazio disco e container Docker attivi."""
        from .ssh.inspection import remote_host_health

        host = self._host(alias)
        res = remote_host_health(
            host,
            self._ssh_cfg(),
            timeout=int(timeout),
        )
        return self._dump(res)

    def host_top_processes(
        self,
        alias: str,
        limit: int = 10,
        sort_by: str = "cpu",
        timeout: int = 30,
    ) -> str:
        """Restituisce i processi più pesanti per CPU o RAM sull'host."""
        from .ssh.inspection import remote_host_top_processes

        host = self._host(alias)
        res = remote_host_top_processes(
            host,
            self._ssh_cfg(),
            limit=int(limit),
            sort_by=sort_by,
            timeout=int(timeout),
        )
        return self._dump(res)

    def write_file(
        self,
        alias: str,
        path: str,
        content: str = "",
        mode: str = "overwrite",
        timeout: int = 30,
    ) -> str:
        """Scrive o appende contenuto a un file remoto.

        mode: 'overwrite' (sovrascrive) | 'append' (aggiunge in coda).
        Il contenuto viene passato via base64 per evitare problemi di quoting.
        """
        from .ssh.inspection import remote_write_file

        host = self._host(alias)
        res = remote_write_file(
            host,
            self._ssh_cfg(),
            path=path,
            content=content,
            mode=mode,
            timeout=int(timeout),
        )
        return self._dump(res)

    def edit_file(
        self,
        alias: str,
        path: str,
        pattern: str,
        replacement: str = "",
        count: int = 0,
        timeout: int = 30,
    ) -> str:
        """Sostituisce pattern nel file remoto con sed/espressione regolare semplice.

        count=0 sostituisce tutte le occorrenze. count>0 limita il numero di sostituzioni.
        """
        from .ssh.inspection import remote_edit_file

        host = self._host(alias)
        res = remote_edit_file(
            host,
            self._ssh_cfg(),
            path=path,
            pattern=pattern,
            replacement=replacement,
            count=int(count),
            timeout=int(timeout),
        )
        return self._dump(res)

    def session_audit_log(
        self,
        alias: str,
        session: str,
        max_lines: int = 0,
        timeout: int = 60,
    ) -> str:
        """Legge il log di audit di una sessione tmux specifica."""
        from .ssh.inspection import remote_session_audit_log

        host = self._host(alias)
        res = remote_session_audit_log(
            host,
            self._ssh_cfg(),
            session=session,
            max_lines=int(max_lines),
            timeout=int(timeout),
        )
        return self._dump(res)

    def tmux_run_and_wait_prompt(
        self, alias: str, session: str, command: str, prompt_regex: str, timeout: int = 30
    ) -> str:
        """Invia un comando a tmux e attende il prompt."""
        from .ssh.inspection import remote_tmux_run_and_wait_prompt

        host = self._host(alias)
        return self._dump(
            remote_tmux_run_and_wait_prompt(
                host, self._ssh_cfg(), session, command, prompt_regex, int(timeout)
            )
        )

    def replace_block(
        self, alias: str, path: str, old_text: str, new_text: str, timeout: int = 30
    ) -> str:
        """Sostituzione esatta multi-riga sicura (alternativa a sed)."""
        from .ssh.inspection import remote_replace_block

        host = self._host(alias)
        return self._dump(
            remote_replace_block(host, self._ssh_cfg(), path, old_text, new_text, int(timeout))
        )

    def project_tree(
        self, alias: str, path: str = ".", max_depth: int = 3, timeout: int = 30
    ) -> str:
        """Albero del progetto token-optimized (salta .git, node_modules ecc.)."""
        from .ssh.inspection import remote_project_tree

        host = self._host(alias)
        return self._dump(
            remote_project_tree(host, self._ssh_cfg(), path, int(max_depth), int(timeout))
        )

    def manage_service(
        self,
        alias: str,
        name: str,
        action: str = "status",
        manager: str = "systemd",
        timeout: int = 30,
    ) -> str:
        """Gestione strutturata di servizi (systemd, docker)."""
        from .ssh.inspection import remote_manage_service

        host = self._host(alias)
        return self._dump(
            remote_manage_service(host, self._ssh_cfg(), name, action, manager, int(timeout))
        )

    def read_service_logs(
        self,
        alias: str,
        name: str,
        lines: int = 100,
        level: str = "",
        grep: str = "",
        timeout: int = 30,
    ) -> str:
        """Estrazione filtrata lato server dei log di un servizio."""
        from .ssh.inspection import remote_read_service_logs

        host = self._host(alias)
        return self._dump(
            remote_read_service_logs(
                host, self._ssh_cfg(), name, int(lines), level, grep, int(timeout)
            )
        )

    def host_network_ports(self, alias: str, timeout: int = 30) -> str:
        """Mappatura strutturata JSON delle porte in ascolto."""
        from .ssh.inspection import remote_host_network_ports

        host = self._host(alias)
        return self._dump(remote_host_network_ports(host, self._ssh_cfg(), int(timeout)))

    def manage_packages(
        self, alias: str, action: str, packages: list[str], timeout: int = 300
    ) -> str:
        """Gestore pacchetti silenzioso per apt/dnf."""
        from .ssh.inspection import remote_manage_packages

        host = self._host(alias)
        return self._dump(
            remote_manage_packages(host, self._ssh_cfg(), action, packages, int(timeout))
        )

    def run_sql_query(
        self,
        alias: str,
        engine: str,
        db: str,
        query: str,
        user: str = "",
        password: str = "",
        host_addr: str = "",
        timeout: int = 60,
    ) -> str:
        """Esecuzione SQL formattata JSON compatta."""
        from .ssh.inspection import remote_run_sql_query

        host = self._host(alias)
        return self._dump(
            remote_run_sql_query(
                host, self._ssh_cfg(), engine, db, query, user, password, host_addr, int(timeout)
            )
        )

    # ---------- snippet / broadcast ----------

    def list_snippets(self) -> str:
        return self._dump(
            [
                {"name": s.name, "command": s.command, "description": s.description}
                for s in load_snippets(self._load_config())
            ]
        )

    def add_snippet(self, name: str, command: str, description: str = "") -> str:
        add_snippet(
            self._load_config(), Snippet(name=name, command=command, description=description)
        )
        return f"snippet '{name}' salvato"

    def remove_snippet(self, name: str) -> str:
        remove_snippet(self._load_config(), name)
        return f"snippet '{name}' rimosso"

    def get_health_summary(self) -> str:
        """Panoramica di tutti gli host con warning count."""
        cfg = self._load_config()
        summary = []
        for h in cfg.hosts:
            try:
                health = self.host_health(h.alias)
                data = json.loads(health)
                warnings = data.get("warnings", [])
                summary.append(
                    {
                        "alias": h.alias,
                        "reachable": data.get("reachable", False),
                        "warnings": warnings,
                        "warning_count": len(warnings),
                    }
                )
            except Exception as e:
                summary.append(
                    {"alias": h.alias, "reachable": False, "error": str(e), "warning_count": 0}
                )
        return self._dump({"ok": True, "hosts": summary, "total_hosts": len(summary)})

    def broadcast(
        self,
        aliases: list[str],
        command: str | None = None,
        snippet: str | None = None,
        mode: str = "tmux",
    ) -> str:
        cfg = self._load_config()
        if snippet:
            for s in load_snippets(cfg):
                if s.name == snippet:
                    command = s.command
                    break
            else:
                return f"errore: snippet '{snippet}' non trovato"
        if not command:
            return "errore: specificare command o snippet"
        hosts = self._hosts(aliases)
        if mode == "tmux":
            results = run_snippet_on_hosts_tmux(
                hosts, command, snippet or "broadcast", self._ssh_cfg()
            )
            return self._dump(
                [
                    {
                        "host": r.host_alias,
                        "ok": r.ok,
                        "session_name": r.session_name,
                        "error": r.error,
                    }
                    for r in results
                ]
            )
        results = run_snippet_on_hosts(hosts, command, self._ssh_cfg())
        return self._dump(
            [
                {
                    "host": r.host_alias,
                    "ok": r.ok,
                    "exit_code": r.exit_code,
                    "stdout": r.stdout,
                    "stderr": r.stderr,
                    "error": r.error,
                }
                for r in results
            ]
        )

    # ---------- tunnel ----------

    def list_tunnels(self, alias: str) -> str:
        host = self._host(alias)
        active = {t.port for t in self.tunnels.active(alias)}
        return self._dump(
            [
                {
                    "name": t.name,
                    "kind": t.kind,
                    "local_port": t.local_port,
                    "remote_host": t.remote_host,
                    "remote_port": t.remote_port,
                    "bind": t.bind,
                    "active": t.local_port in active,
                }
                for t in host.tunnels
            ]
        )

    def start_tunnel(
        self,
        alias: str,
        kind: str,
        local_port: int,
        remote_host: str = "",
        remote_port: int = 0,
        name: str = "",
        bind: str = "localhost",
    ) -> str:
        host = self._host(alias)
        spec = Tunnel(
            name=name,
            kind=kind.upper(),
            local_port=int(local_port),
            remote_host=remote_host,
            remote_port=int(remote_port),
            bind=bind,
        )
        password = self._password_for(host)
        jump, jump_password = self._jump_for(host)
        ok, msg = self.tunnels.start(
            host, spec, password, password_resolver=self._password_for, jump_host=jump
        )
        return msg if ok else f"errore: {msg}"

    def stop_tunnel(self, alias: str, local_port: int) -> str:
        ok = self.tunnels.stop(alias, int(local_port))
        return (
            f"tunnel {local_port} fermato" if ok else f"nessun tunnel attivo su porta {local_port}"
        )

    def stop_tunnels(self, alias: str) -> str:
        count = self.tunnels.stop_all(alias)
        return f"fermati {count} tunnel" if count else "nessun tunnel attivo"

    def list_tunnels_all(self) -> str:
        """Elenca i tunnel configurati e lo stato attivo su tutti gli host."""
        cfg = self._load_config()
        out = []
        for h in cfg.hosts:
            active = {t.port for t in self.tunnels.active(h.alias)}
            specs = [
                {
                    "name": t.name,
                    "kind": t.kind,
                    "local_port": t.local_port,
                    "remote_host": t.remote_host,
                    "remote_port": t.remote_port,
                    "bind": t.bind,
                    "active": t.local_port in active,
                }
                for t in h.tunnels
            ]
            if specs:
                out.append({"alias": h.alias, "tunnels": specs})
        return self._dump(out)

    def tunnel_health(self, alias: str) -> str:
        """Verifica che le porte locali dei tunnel attivi siano realmente in ascolto."""
        import socket

        self._host(alias)  # valida che l'alias esista
        procs = self.tunnels.active(alias)
        if not procs:
            return self._dump({"alias": alias, "active": 0, "tunnels": []})
        out = []
        for t in procs:
            # per -L/-D la porta è locale; per -R la porta è sul remoto
            if t.spec.kind == "R":
                out.append(
                    {
                        "name": t.spec.name,
                        "kind": "R",
                        "local_port": t.spec.local_port,
                        "listening": "n/a (porta remota)",
                    }
                )
                continue
            listening = False
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(("127.0.0.1", t.spec.local_port))
                    listening = True
            except OSError:
                listening = False
            out.append(
                {
                    "name": t.spec.name,
                    "kind": t.spec.kind,
                    "local_port": t.spec.local_port,
                    "listening": listening,
                }
            )
        return self._dump({"alias": alias, "active": len(procs), "tunnels": out})

    # ---------- cronologia / rotazioni ----------

    def session_history(self, limit: int = 50) -> str:
        """Cronologia delle sessioni recenti (history.json).

        ``limit``: numero massimo di voci da restituire (default 50, max 200).
        """
        from .history import load_history

        entries = load_history(self._load_config())
        limit = max(0, min(int(limit), 200))
        return self._dump([{"host": e.host, "session": e.session} for e in entries[:limit]])

    def list_rotations(self) -> str:
        """Elenca i profili di rotazione salvati."""
        from .rotation import load_rotations

        rotations = load_rotations(self._load_config())
        return self._dump(
            [
                {
                    "name": r.name,
                    "entries": [{"host": h, "session": s} for h, s in r.unique_entries()],
                }
                for r in rotations
            ]
        )

    def add_rotation(self, name: str, entries: list[str]) -> str:
        """Aggiunge (o sostituisce per nome) un profilo di rotazione.

        ``entries``: lista di coppie "host/sessione".
        """
        from .rotation import Rotation, add_rotation

        pairs: list[tuple[str, str]] = []
        for e in entries:
            if "/" in e:
                host, _, session = e.partition("/")
                if host and session:
                    pairs.append((host.strip(), session.strip()))
        if not pairs:
            return "errore: nessuna coppia host/sessione valida"
        add_rotation(self._load_config(), Rotation(name=name, entries=pairs))
        return f"rotazione '{name}' salvata ({len(pairs)} voci)"

    def remove_rotation(self, name: str) -> str:
        """Rimuove un profilo di rotazione."""
        from .rotation import remove_rotation

        remove_rotation(self._load_config(), name)
        return f"rotazione '{name}' rimossa"

    # ---------- audit ----------

    def _remote_audit_dir(self) -> str:
        return "$HOME/.bravoric-ssh-client/logs"

    def _audit_sanitize(self, filename: str) -> bool:
        """Accetta solo nomi di log sicuri (niente path traversal)."""
        import re

        return bool(re.fullmatch(r"[A-Za-z0-9._-]+\.log\.gz", filename))

    def list_remote_audit_logs(self, alias: str) -> str:
        """Elenca i log audit compressi sul server dell'host (se remoto)."""
        from .ssh.broadcast import run_snippet_on_host

        host = self._host(alias)
        if host.is_local():
            return "errore: host locale (usa list_audit_logs per i log locali)"
        cmd = f"ls -1 {self._remote_audit_dir()}/*.log.gz 2>/dev/null | xargs -n1 basename"
        res = run_snippet_on_host(host, cmd, self._ssh_cfg(), timeout=30)
        if res.error:
            return f"errore: {res.error}"
        files = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        return self._dump(files) if files else self._dump([])

    def read_remote_audit_log(self, alias: str, filename: str, max_lines: int = 0) -> str:
        """Legge un log audit .gz dal server dell'host (max_lines=0 = tutto)."""
        from .ssh.broadcast import run_snippet_on_host

        if not self._audit_sanitize(filename):
            return f"errore: nome file non consentito: {filename}"
        host = self._host(alias)
        if host.is_local():
            return "errore: host locale (usa read_audit_log per i log locali)"
        q = adapter._sh_quote(f"{self._remote_audit_dir()}/{filename}")
        tail = f" | tail -n {int(max_lines)}" if max_lines and int(max_lines) > 0 else ""
        cmd = f"gzip -cd {q} 2>/dev/null{tail}"
        res = run_snippet_on_host(host, cmd, self._ssh_cfg(), timeout=60)
        if res.error:
            return f"errore: {res.error}"
        return res.stdout or "(log vuoto)"

    def list_audit_logs(self) -> str:
        cfg = self._load_config()
        logs_dir = cfg_path_logs_dir(cfg)
        files = sorted(logs_dir.glob("*.log.gz"))
        return self._dump([str(f.name) for f in files])

    def read_audit_log(self, filename: str, max_lines: int = 0) -> str:
        cfg = self._load_config()
        logs_dir = cfg_path_logs_dir(cfg)
        path = (logs_dir / filename).resolve()
        if not path.is_relative_to(logs_dir.resolve()) or path.suffix != ".gz":
            return f"errore: percorso non consentito: {filename}"
        if not path.exists():
            return f"errore: log '{filename}' non trovato"
        return read_log_gz(path, max_lines=int(max_lines))

    # ---------- registrazione ----------

    def _register(self) -> None:
        specs: list[tuple[str, str, str]] = [
            ("list_hosts", "list_hosts", "Elenca gli host configurati."),
            ("get_host", "get_host", "Dettagli di un host."),
            (
                "get_status",
                "get_status",
                "Diagnostica del server MCP (config, provider, tmux, dir log).",
            ),
            ("ping", "ping", "Verifica la raggiungibilità TCP di un host."),
            ("tmux_present", "tmux_present", "True se tmux è installato sull'host."),
            ("ping_all", "ping_all", "Ping TCP di tutti gli host."),
            (
                "hosts_summary",
                "hosts_summary",
                "Dashboard: per ogni host raggiungibilità, tmux e numero sessioni.",
            ),
            (
                "list_sessions_all",
                "list_sessions_all",
                "Elenca le sessioni tmux di tutti gli host in parallelo.",
            ),
            (
                "find_in_sessions",
                "find_in_sessions",
                "Cerca una regex nelle pane delle sessioni tmux di un host.",
            ),
            (
                "run_command_all",
                "run_command_all",
                "Esegue un comando batch su tutti gli host raggiungibili.",
            ),
            (
                "run_and_wait",
                "run_and_wait",
                "Esegue un comando in tmux detached e attende il completamento (sentinel).",
            ),
            (
                "broadcast_wait",
                "broadcast_wait",
                "Broadcast tmux su più host con attesa di completamento e raccolta output.",
            ),
            ("list_sessions", "list_sessions", "Elenca le sessioni tmux di un host."),
            (
                "create_session",
                "create_session",
                "Crea una sessione tmux detached su un host (comando opzionale).",
            ),
            ("session_details", "session_details", "Dettagli di una sessione tmux."),
            ("rename_session", "rename_session", "Rinomina una sessione tmux."),
            ("kill_session", "kill_session", "Termina una sessione tmux."),
            (
                "kill_server",
                "kill_server",
                "Termina il server tmux di un host (tutte le sessioni).",
            ),
            ("detach_clients", "detach_clients", "Stacca gli altri client dalla sessione."),
            ("list_windows", "list_windows", "Elenca le finestre di una sessione."),
            (
                "list_windows_parsed",
                "list_windows_parsed",
                "Finestre di una sessione in forma strutturata (indice, nome, attiva).",
            ),
            (
                "select_window",
                "select_window",
                "Rende attiva (visibile) una finestra della sessione.",
            ),
            ("new_window", "new_window", "Crea una finestra in una sessione."),
            ("rename_window", "rename_window", "Rinomina una finestra."),
            ("kill_window", "kill_window", "Chiude una finestra."),
            (
                "capture_pane",
                "capture_pane",
                "Cattura l'output di una sessione (ultime lines righe).",
            ),
            ("send_keys", "send_keys", "Invia testo letterale alla sessione."),
            (
                "send_input",
                "send_input",
                "Invia testo universale (comandi, prompt, multiriga) a tmux con Invio atomico di default.",
            ),
            (
                "send_line",
                "send_line",
                "Invia riga di comando o testo con Enter automatico (alias rapido di send_input).",
            ),
            ("send_enter", "send_enter", "Invia Invio alla sessione."),
            ("send_raw", "send_raw", "Invia tasti tmux non letterali (es. C-c, Up)."),
            (
                "paste",
                "paste",
                "Incolla testo/file nella sessione con bracketed paste (buffer tmux).",
            ),
            (
                "pane_info",
                "pane_info",
                "Processo, CWD, PID, titolo e dimensioni della pane attiva.",
            ),
            (
                "pane_diff",
                "pane_diff",
                "Snapshot & diff incrementale dell'output di una pane (solo righe nuove).",
            ),
            (
                "pane_command",
                "pane_command",
                "Mostra il processo in primo piano nella pane attiva (es. node, bash).",
            ),
            (
                "close_foreground",
                "close_foreground",
                "Chiude il processo in primo piano (TUI/comando) in una sessione tmux.",
            ),
            (
                "restart_foreground",
                "restart_foreground",
                "Chiude e rilancia il processo in primo piano (riavvio deterministico).",
            ),
            ("run_command", "run_command", "Esegue un comando batch su un host (ssh)."),
            (
                "run_command_many",
                "run_command_many",
                "Esegue un comando batch in parallelo su più host.",
            ),
            ("list_snippets", "list_snippets", "Elenca gli snippet del catalogo."),
            ("add_snippet", "add_snippet", "Aggiunge uno snippet al catalogo."),
            ("remove_snippet", "remove_snippet", "Rimuove uno snippet dal catalogo."),
            (
                "broadcast",
                "broadcast",
                "Broadcast di un comando/snippet su più host (mode=tmux|direct).",
            ),
            ("list_tunnels", "list_tunnels", "Elenca i tunnel configurati di un host."),
            ("start_tunnel", "start_tunnel", "Avvia un tunnel SSH (kind L/R/D)."),
            ("stop_tunnel", "stop_tunnel", "Ferma il tunnel su una porta."),
            ("stop_tunnels", "stop_tunnels", "Ferma tutti i tunnel di un host."),
            (
                "list_tunnels_all",
                "list_tunnels_all",
                "Elenca tunnel e stato attivo su tutti gli host.",
            ),
            (
                "tunnel_health",
                "tunnel_health",
                "Verifica che le porte locali dei tunnel attivi ascoltino davvero.",
            ),
            (
                "session_history",
                "session_history",
                "Cronologia delle sessioni recenti (history.json).",
            ),
            ("list_rotations", "list_rotations", "Elenca i profili di rotazione salvati."),
            (
                "add_rotation",
                "add_rotation",
                "Aggiunge (o sostituisce per nome) un profilo di rotazione.",
            ),
            ("remove_rotation", "remove_rotation", "Rimuove un profilo di rotazione."),
            ("list_audit_logs", "list_audit_logs", "Elenca i log audit compressi locali."),
            (
                "read_audit_log",
                "read_audit_log",
                "Legge un log audit .gz locale (max_lines=0 = tutto).",
            ),
            (
                "list_remote_audit_logs",
                "list_remote_audit_logs",
                "Elenca i log audit compressi sul server remoto dell'host.",
            ),
            (
                "read_remote_audit_log",
                "read_remote_audit_log",
                "Legge un log audit .gz remoto (max_lines=0 = tutto).",
            ),
            (
                "sftp_download",
                "sftp_download",
                "Scarica un file da un host in locale (scp/SFTP, password dal provider).",
            ),
            (
                "sftp_upload",
                "sftp_upload",
                "Carica un file locale su un host (scp/SFTP, password dal provider).",
            ),
            (
                "transfer_file",
                "transfer_file",
                "Trasferisce un file tra due host via temp locale (md5 riportato).",
            ),
            (
                "sftp_list",
                "sftp_list",
                "Elenca il contenuto di una directory remota via SFTP nativo.",
            ),
            (
                "sftp_mkdir",
                "sftp_mkdir",
                "Crea una directory remota via SFTP nativo.",
            ),
            (
                "sftp_rm",
                "sftp_rm",
                "Rimuove un file o directory remota via SFTP nativo.",
            ),
            (
                "sftp_rename",
                "sftp_rename",
                "Rinomina o sposta un file/directory remota via SFTP nativo.",
            ),
            (
                "sftp_get",
                "sftp_get",
                "Scarica file o directory via SFTP nativo (supporta ricorsione).",
            ),
            (
                "sftp_put",
                "sftp_put",
                "Carica file o directory via SFTP nativo (supporta ricorsione).",
            ),
            (
                "sftp_batch",
                "sftp_batch",
                "Esegue comandi SFTP arbitrari in batch su un host.",
            ),
            (
                "transfer_file_direct",
                "transfer_file_direct",
                "Trasferisce un file direttamente tra due host remoti usando SFTP streaming (senza staging locale).",
            ),
            (
                "search_files",
                "search_files",
                "Cerca file per nome/glob sull'host con modalità compact, metadata o grep.",
            ),
            (
                "read_file",
                "read_file",
                "Legge una porzione mirata di un file (lines o bytes) con limit e offset.",
            ),
            (
                "git_status",
                "git_status",
                "Stato sintetico di un repository git in JSON compatto (branch, commit, modifiche).",
            ),
            (
                "host_health",
                "host_health",
                "Quadro sintetico risorse host: CPU load, RAM libera, spazio disco e container Docker.",
            ),
            (
                "host_top_processes",
                "host_top_processes",
                "Top processi per CPU/RAM sull'host (limit, sort_by='cpu'|'mem').",
            ),
            (
                "write_file",
                "write_file",
                "Scrive o appende contenuto a un file remoto (mode='overwrite'|'append').",
            ),
            (
                "edit_file",
                "edit_file",
                "Sostituisce pattern in file remoto con sed/espressione regolare semplice.",
            ),
            (
                "tmux_run_and_wait_prompt",
                "tmux_run_and_wait_prompt",
                "Esegue comando in tmux e attende match del prompt regex.",
            ),
            ("replace_block", "replace_block", "Sostituzione multi-riga esatta sicura in un file."),
            (
                "project_tree",
                "project_tree",
                "Restituisce un albero di directory JSON-optimized per token.",
            ),
            (
                "manage_service",
                "manage_service",
                "Restituisce JSON strutturato stato o gestisce systemd/docker.",
            ),
            (
                "read_service_logs",
                "read_service_logs",
                "Legge log di sistema/servizio con filtraggio lato server.",
            ),
            (
                "host_network_ports",
                "host_network_ports",
                "Restituisce array JSON con le porte locali in ascolto.",
            ),
            (
                "manage_packages",
                "manage_packages",
                "Installa/aggiorna pacchetti APT/DNF restituendo JSON compatto.",
            ),
            (
                "run_sql_query",
                "run_sql_query",
                "Esegue query psql/mysql/sqlite e restituisce output strutturato.",
            ),
            (
                "session_audit_log",
                "session_audit_log",
                "Legge il log di audit di una sessione tmux specifica dall'host.",
            ),
        ]
        for name, method_name, description in specs:
            self.server.tool(name=name, description=description)(getattr(self, method_name))

    def run(self) -> None:
        self.server.run(transport="stdio")


def main() -> None:
    BravoricMcp().run()


if __name__ == "__main__":
    main()
