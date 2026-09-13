"""Gestione dei tunnel SSH (port forwarding) in background.

Avvia ``ssh -N -T`` in subprocess per forward locali (-L), remoti (-R) e
dinamici/SOCKS (-D), e ne traccia lo stato (PID vivo, porta locale). Le
password arrivano via SSH_ASKPASS (helper semplice o multi-host con bastion).

Lo stato viene **persistito su file** (``tunnels.json``, di default nella
directory di configurazione): i tunnel attivi sopravvivono al riavvio del
processo che li ha avviati (MCP o TUI) e sono condivisi tra i due frontend.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Host, Tunnel, default_config_dir
from .commander import write_askpass_helper

DEFAULT_CONNECT_TIMEOUT = "4"

_STATE_VERSION = 1


@dataclass
class TunnelProcess:
    """Un tunnel in esecuzione: spec originale + processo + porta locale.

    ``proc`` è il Popen quando il tunnel è stato avviato da questo processo;
    per i tunnel ripristinati da stato persistito (riavvio) può essere ``None``
    e si usa ``pid`` per verificarne la vivacità.
    """

    spec: Tunnel
    port: int
    proc: subprocess.Popen | None = None
    helper: Path | None = None
    pid: int | None = None
    started_at: float = 0.0

    @property
    def running(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self.pid is None:
            return False
        return _pid_alive(self.pid)

    @property
    def exit_code(self) -> int | None:
        if self.proc is not None:
            return self.proc.poll()
        return None

    def terminate(self) -> None:
        """Termina il processo del tunnel (Popen o PID)."""
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            except (OSError, ProcessLookupError):
                pass
            return
        if self.pid is not None:
            try:
                os.kill(self.pid, 15)  # SIGTERM
                import time as _t

                for _ in range(15):
                    if not _pid_alive(self.pid):
                        break
                    _t.sleep(0.2)
                else:
                    os.kill(self.pid, 9)  # SIGKILL
            except (OSError, ProcessLookupError):
                pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def default_tunnels_path() -> Path:
    """Percorso del file di stato dei tunnel (default: config_dir/tunnels.json)."""
    return default_config_dir() / "tunnels.json"


class TunnelManager:
    """Registro dei tunnel attivi per host (chiave = alias).

    Le modifiche vengono salvate su ``state_path`` (JSON) così lo stato è
    condiviso tra MCP e TUI e sopravvive ai riavvii.
    """

    def __init__(self, state_path: Path | None = None):
        self._tunnels: dict[str, list[TunnelProcess]] = {}
        self._state_path = state_path
        self._loaded = state_path is None  # niente file -> non serve caricare

    @property
    def state_path(self) -> Path | None:
        return self._state_path

    @state_path.setter
    def state_path(self, path: Path | None) -> None:
        self._state_path = path
        self._loaded = path is None
        if path is not None:
            self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        """Carica lo stato persistito (una sola volta) e riconcilia i PID."""
        if self._loaded or self._state_path is None:
            return
        self._loaded = True
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        changed = False
        for item in data.get("tunnels") or []:
            alias = str(item.get("alias") or "")
            pid = item.get("pid")
            if not alias or not pid:
                continue
            if not _pid_alive(int(pid)):
                changed = True
                continue
            try:
                spec = Tunnel(
                    **{
                        k: item.get(k)
                        for k in (
                            "name",
                            "kind",
                            "local_port",
                            "remote_host",
                            "remote_port",
                            "bind",
                        )
                        if k in item
                    }
                )
            except Exception:
                continue
            self._tunnels.setdefault(alias, []).append(
                TunnelProcess(
                    spec=spec,
                    port=int(spec.local_port),
                    proc=None,
                    pid=int(pid),
                    started_at=float(item.get("started_at") or 0),
                )
            )
        if changed:
            self._persist()

    def _persist(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": _STATE_VERSION,
            "tunnels": [
                {
                    "alias": alias,
                    "name": t.spec.name,
                    "kind": t.spec.kind,
                    "local_port": t.spec.local_port,
                    "remote_host": t.spec.remote_host,
                    "remote_port": t.spec.remote_port,
                    "bind": t.spec.bind,
                    "pid": t.pid,
                    "started_at": t.started_at,
                }
                for alias, procs in self._tunnels.items()
                for t in procs
                if t.running and t.pid is not None
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def start(
        self,
        host: Host,
        spec: Tunnel,
        password: str | None,
        *,
        password_resolver: Callable[[Host], str | None] | None = None,
        jump_host: Host | None = None,
        connect_timeout: str = DEFAULT_CONNECT_TIMEOUT,
        ssh_bin: str | None = None,
    ) -> tuple[bool, str]:
        """Avvia il tunnel ``spec`` verso ``host`` in background.

        Ritorna (ok, messaggio). Se la porta è già in uso da un altro tunnel
        attivo dello stesso host, non duplica (ritorna ok=False).
        """
        self._ensure_loaded()
        existing = self._tunnels.get(host.alias) or []
        for t in existing:
            if t.running and t.port == spec.local_port:
                return False, f"porta {spec.local_port} già attiva su {host.alias}"
        args, env, helper = self._build_args(
            host, spec, password, password_resolver, jump_host, connect_timeout, ssh_bin
        )
        try:
            proc = subprocess.Popen(
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            if helper is not None:
                try:
                    helper.unlink(missing_ok=True)
                except OSError:
                    pass
            return False, str(exc)
        if helper is not None:
            threading.Thread(target=self._cleanup_on_exit, args=(proc, helper), daemon=True).start()
        self._tunnels.setdefault(host.alias, []).append(
            TunnelProcess(
                spec=spec,
                port=spec.local_port,
                proc=proc,
                pid=proc.pid,
                started_at=time.time(),
                helper=helper,
            )
        )
        self._persist()
        return True, f"tunnel {spec.local_addr()} attivo ({proc.pid})"

    def _cleanup_on_exit(self, proc: subprocess.Popen, helper: Path) -> None:
        proc.wait()
        try:
            helper.unlink(missing_ok=True)
        except OSError:
            pass
        self._persist()

    def _build_args(
        self,
        host: Host,
        spec: Tunnel,
        password: str | None,
        password_resolver: Callable[[Host], str | None] | None,
        jump_host: Host | None,
        connect_timeout: str,
        ssh_bin: str | None,
    ) -> tuple[list[str], dict[str, str], Path | None]:
        ssh = ssh_bin or shutil.which("ssh") or "ssh"
        args = [
            ssh,
            "-N",
            "-T",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={connect_timeout}",
        ]
        if host.port and host.port != 22:
            args += ["-p", str(host.port)]
        if jump_host is not None:
            jtarget = jump_host.host
            if jump_host.effective_user():
                jtarget = f"{jump_host.effective_user()}@{jump_host.host}"
            if jump_host.port and jump_host.port != 22:
                jtarget += f":{jump_host.port}"
            args += ["-J", jtarget]
        args += [_forward_arg(spec)]
        target = host.host
        if host.effective_user():
            target = f"{host.effective_user()}@{host.host}"
        args.append(target)

        env = dict(os.environ)
        mapping: dict[str, str] = {}
        if password_resolver and jump_host is not None:
            jpw = password_resolver(jump_host)
            if jpw:
                mapping[jump_host.host] = jpw
            hpw = password_resolver(host)
            if hpw:
                mapping[host.host] = hpw
        elif password:
            mapping[host.host] = password
        helper: Path | None = None
        if mapping:
            helper = write_askpass_helper(mapping)
            env["SSH_ASKPASS"] = str(helper)
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")
        return args, env, helper

    def stop(self, host_alias: str, port: int) -> bool:
        """Termina il tunnel sulla ``port`` per l'host (se attivo)."""
        self._ensure_loaded()
        procs = self._tunnels.get(host_alias) or []
        for t in list(procs):
            if t.port == port and t.running:
                t.terminate()
                procs.remove(t)
                if t.helper is not None:
                    try:
                        t.helper.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._persist()
                return True
        return False

    def stop_all(self, host_alias: str) -> int:
        """Termina tutti i tunnel attivi dell'host; ritorna il numero terminato."""
        self._ensure_loaded()
        count = 0
        procs = self._tunnels.get(host_alias) or []
        for t in list(procs):
            if t.running:
                t.terminate()
                count += 1
            if t.helper is not None:
                try:
                    t.helper.unlink(missing_ok=True)
                except OSError:
                    pass
        if procs:
            self._tunnels[host_alias] = []
        if count:
            self._persist()
        return count

    def active(self, host_alias: str) -> list[TunnelProcess]:
        """Tunnel attivi dell'host (solo quelli con processo vivo)."""
        self._ensure_loaded()
        procs = self._tunnels.get(host_alias) or []
        alive = [t for t in procs if t.running]
        if len(alive) != len(procs):
            self._tunnels[host_alias] = alive
            self._persist()
        return alive

    def any_active(self, host_alias: str) -> bool:
        return bool(self.active(host_alias))


def _forward_arg(spec: Tunnel) -> str:
    """Costruisce l'argomento -L/-R/-D per la definizione del tunnel."""
    if spec.kind == "D":
        return f"-D {spec.local_addr()}"
    flag = "-L" if spec.kind == "L" else "-R"
    return f"{flag} {spec.local_addr()}:{spec.remote_host}:{spec.remote_port}"
