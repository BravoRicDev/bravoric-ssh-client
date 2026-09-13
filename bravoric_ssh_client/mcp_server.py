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
from pathlib import Path
from typing import Any

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


class BravoricMcp:
    """Wrapper dell'MCPServer che registra i tool e detiene lo stato."""

    def __init__(self, config: Config | None = None):
        from mcp.server.mcpserver import MCPServer

        self.config = config
        self.tunnels = TunnelManager()
        self.server = MCPServer(
            "bravoric-ssh",
            title="bravoric-ssh-client",
            version=__version__,
            instructions=(
                "Controllo di host SSH e sessioni tmux: comandi, broadcast, tunnel, audit."
            ),
        )
        self._register()

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

    # ---------- sessioni tmux ----------

    def list_sessions(self, alias: str) -> str:
        res = adapter.list_tmux_sessions(self._host(alias), self._ssh_cfg())
        if not res.ok:
            return f"errore: {res.error}"
        return self._dump(res.sessions)

    def create_session(
        self, alias: str, name: str | None = None, command: str | None = None
    ) -> str:
        host = self._host(alias)
        name = name or session_slug("mcp", alias)
        q = adapter._sh_quote
        tmux_cmd = f"tmux new -d -s {q(name)}"
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

    def send_keys(self, alias: str, session: str, text: str) -> str:
        res = adapter.tmux_send_keys(self._host(alias), session, text, self._ssh_cfg())
        return "inviato" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def send_enter(self, alias: str, session: str) -> str:
        res = adapter.tmux_send_enter(self._host(alias), session, self._ssh_cfg())
        return "Invio inviato" if res.ok else f"errore: {(res.stderr or '').strip()}"

    def send_raw(self, alias: str, session: str, keys: str) -> str:
        res = adapter.tmux_send_raw(self._host(alias), session, keys, self._ssh_cfg())
        return "tasti inviati" if res.ok else f"errore: {(res.stderr or '').strip()}"

    # ---------- comandi batch ----------

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

    def session_history(self) -> str:
        """Cronologia delle sessioni recenti (history.json)."""
        from .history import load_history

        entries = load_history(self._load_config())
        return self._dump([{"host": e.host, "session": e.session} for e in entries[:50]])

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
            ("new_window", "new_window", "Crea una finestra in una sessione."),
            ("rename_window", "rename_window", "Rinomina una finestra."),
            ("kill_window", "kill_window", "Chiude una finestra."),
            (
                "capture_pane",
                "capture_pane",
                "Cattura l'output di una sessione (ultime lines righe).",
            ),
            ("send_keys", "send_keys", "Invia testo letterale alla sessione."),
            ("send_enter", "send_enter", "Invia Invio alla sessione."),
            ("send_raw", "send_raw", "Invia tasti tmux non letterali (es. C-c, Up)."),
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
        ]
        for name, method_name, description in specs:
            self.server.tool(name=name, description=description)(getattr(self, method_name))

    def run(self) -> None:
        self.server.run(transport="stdio")


def main() -> None:
    BravoricMcp().run()


if __name__ == "__main__":
    main()
