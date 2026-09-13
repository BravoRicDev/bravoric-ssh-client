"""Adapter SSH: esecuzione di comandi remoti non interattivi.

Per il listino sessioni tmux serve autenticazione non interattiva. Invece di
dipendere da ``sshpass`` (solo Linux), usiamo il meccanismo standard di OpenSSH
``SSH_ASKPASS`` con ``SSH_ASKPASS_REQUIRE=force``: funziona su Linux, macOS e
Windows (OpenSSH recente) con un unico helper che emette la password da ambiente.
Se l'host usa chiave/agent, si procede con ``BatchMode=yes`` senza password.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Host
from .shellutil import sh_quote, write_askpass_helper, write_askpass_helper_single

DEFAULT_CONNECT_TIMEOUT = "4"


@dataclass
class ListSessionsResult:
    sessions: list[str] = field(default_factory=list)
    ok: bool = False
    error: str = ""


@dataclass
class SshConfig:
    password_provider: Callable[[Host], str | None] | None = None
    connect_timeout: str = DEFAULT_CONNECT_TIMEOUT
    ssh_bin: str | None = None  # override per i test
    jump_resolver: Callable[[Host], Host | None] | None = None  # alias -> Host bastion


def _find_ssh(cfg: SshConfig | None) -> str:
    if cfg and cfg.ssh_bin:
        return cfg.ssh_bin
    import shutil

    return shutil.which("ssh") or "ssh"


def _run_local(command: str, *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Esegue un comando tmux LOCALMENTE (nessun SSH)."""

    # il comando arriva già pronto per essere eseguito dalla shell (es. pipe sed)
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_askpass_helper() -> Path:
    """Helper che stampa la password dalla variabile BRAVORIC_PASSWORD."""
    return write_askpass_helper_single()


def _ssh_target(host: Host) -> str:
    if host.effective_user():
        return f"{host.effective_user()}@{host.host}"
    return host.host


def _ssh_jump_args(host: Host, cfg: SshConfig) -> list[str]:
    """Argomenti ``-J`` (ProxyJump) se l'host ha un bastion configurato."""
    if not host.jump_host or not cfg.jump_resolver:
        return []
    jump = cfg.jump_resolver(host)
    if jump is None:
        return []
    target = _ssh_target(jump)
    if jump.port and jump.port != 22:
        target += f":{jump.port}"
    return ["-J", target]


def _ssh_base_args(host: Host, cfg: SshConfig, *, batch: bool) -> list[str]:
    args = []
    if batch:
        args += ["-o", "BatchMode=yes"]
    args += [
        "-o",
        "ConnectTimeout=" + cfg.connect_timeout,
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if host.port and host.port != 22:
        args += ["-p", str(host.port)]
    args += _ssh_jump_args(host, cfg)
    return args + [_ssh_target(host)]


def _password_mapping(host: Host, cfg: SshConfig) -> dict[str, str]:
    """Mappa host.host -> password per l'host e il suo eventuale bastion."""
    mapping: dict[str, str] = {}
    if cfg.password_provider:
        if host.jump_host and cfg.jump_resolver:
            jump = cfg.jump_resolver(host)
            if jump is not None:
                jpw = cfg.password_provider(jump)
                if jpw:
                    mapping[jump.host] = jpw
        pw = cfg.password_provider(host)
        if pw:
            mapping[host.host] = pw
    return mapping


def run_remote_command(
    host: Host,
    command: str,
    cfg: SshConfig | None = None,
    *,
    batch: bool = False,
    with_password: bool = False,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    """Esegue ``command`` sul server.

    - ``batch``: rifiuta prompt interattivi (utile per capire se serve password).
    - ``with_password``: fornisce le password via SSH_ASKPASS helper (multi-host
      per il caso bastion + host finale).
    """
    cfg = cfg or SshConfig()
    ssh = _find_ssh(cfg)
    args = [ssh, *_ssh_base_args(host, cfg, batch=batch), command]
    env = dict(os.environ)
    if with_password:
        mapping = _password_mapping(host, cfg)
        if not mapping:
            raise ValueError("with_password=True ma nessuna password dal provider")
        # Caso semplice (un solo host, nessun bastion): helper a password singola,
        # comportamento storico con BRAVORIC_PASSWORD in env.
        has_jump = bool(host.jump_host and cfg.jump_resolver and cfg.jump_resolver(host))
        if not has_jump:
            password = next(iter(mapping.values()))
            helper = _write_askpass_helper()
            try:
                env["SSH_ASKPASS"] = str(helper)
                env["SSH_ASKPASS_REQUIRE"] = "force"
                env.setdefault("DISPLAY", ":0")
                env["BRAVORIC_PASSWORD"] = password
                return subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout, env=env
                )
            finally:
                helper.unlink(missing_ok=True)
        # Caso bastion: helper multi-host (una password per host, match sul prompt).
        helper = write_askpass_helper(mapping)
        try:
            env["SSH_ASKPASS"] = str(helper)
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        finally:
            helper.unlink(missing_ok=True)
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def list_tmux_sessions(host: Host, cfg: SshConfig | None = None) -> ListSessionsResult:
    """Elenca le sessioni tmux (locali se host.is_local(), altrimenti remote)."""
    cfg = cfg or SshConfig()
    command = "tmux ls 2>/dev/null | sed 's/:.*//'"

    if host.is_local():
        proc = _run_local(command)
        if proc.returncode == 0:
            sessions = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            return ListSessionsResult(sessions=sessions, ok=True)
        return ListSessionsResult(
            ok=False, error=(proc.stderr or "").strip() or f"exit {proc.returncode}"
        )

    # 1) Tentativo pulito: chiave/agent (BatchMode, nessun prompt).
    proc = run_remote_command(host, command, cfg, batch=True)
    if proc.returncode == 0:
        sessions = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return ListSessionsResult(sessions=sessions, ok=True)

    # 2) Serve una password? Proviamo con il provider.
    if not (cfg.password_provider and cfg.password_provider(host)):
        return ListSessionsResult(
            ok=False, error=(proc.stderr or "").strip() or f"exit {proc.returncode}"
        )

    proc = run_remote_command(host, command, cfg, with_password=True)
    if proc.returncode == 0:
        sessions = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return ListSessionsResult(sessions=sessions, ok=True)
    return ListSessionsResult(
        ok=False, error=(proc.stderr or "").strip() or f"exit {proc.returncode}"
    )


# Alias mantenuto per compatibilità: broadcast, MCP e test usano adapter._sh_quote.
_sh_quote = sh_quote


@dataclass
class TmuxActionResult:
    """Esito di un comando tmux remoto non interattivo."""

    ok: bool = False
    stdout: str = ""
    stderr: str = ""


def run_tmux_action(
    host: Host,
    command: str,
    cfg: SshConfig | None = None,
    *,
    timeout: int = 20,
) -> TmuxActionResult:
    """Esegue un comando tmux (locale se host.is_local(), altrimenti remoto)."""
    cfg = cfg or SshConfig()
    if host.is_local():
        proc = _run_local(command, timeout=timeout)
        return TmuxActionResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    proc = run_remote_command(host, command, cfg, batch=True, timeout=timeout)
    if proc.returncode == 0:
        return TmuxActionResult(ok=True, stdout=proc.stdout or "", stderr=proc.stderr or "")
    if cfg.password_provider and cfg.password_provider(host):
        proc = run_remote_command(host, command, cfg, with_password=True, timeout=timeout)
        if proc.returncode == 0:
            return TmuxActionResult(ok=True, stdout=proc.stdout or "", stderr=proc.stderr or "")
    return TmuxActionResult(ok=False, stdout=proc.stdout or "", stderr=proc.stderr or "")


def tmux_rename_session(
    host: Host, old_name: str, new_name: str, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Rinomina una sessione tmux remota."""
    return run_tmux_action(
        host,
        f"tmux rename-session -t {_sh_quote(old_name)} {_sh_quote(new_name)}",
        cfg,
    )


def tmux_kill_session(host: Host, name: str, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Termina una sessione tmux remota."""
    return run_tmux_action(host, f"tmux kill-session -t {_sh_quote(name)}", cfg)


def tmux_kill_server(host: Host, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Termina il server tmux remoto (tutte le sessioni)."""
    return run_tmux_action(host, "tmux kill-server", cfg)


def tmux_detach_clients(host: Host, name: str, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Stacca gli altri client collegati alla sessione (non chiude la sessione)."""
    return run_tmux_action(host, f"tmux detach-client -s {_sh_quote(name)}", cfg)


def tmux_list_windows(host: Host, name: str, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Elenca le finestre di una sessione remota (testo libero)."""
    return run_tmux_action(host, f"tmux list-windows -t {_sh_quote(name)}", cfg)


def tmux_capture_pane(
    host: Host,
    session: str,
    cfg: SshConfig | None = None,
    *,
    lines: int = 200,
    window: str | None = None,
) -> TmuxActionResult:
    """Cattura il contenuto della sessione tmux remota (ultime ``lines`` righe).

    ``window`` opzionale: ``session:index`` per catturare una finestra specifica.
    """
    target = session if window is None else f"{session}:{window}"
    return run_tmux_action(
        host,
        f"tmux capture-pane -p -t {_sh_quote(target)} -S -{int(lines)}",
        cfg,
    )


def tmux_send_keys(
    host: Host, session: str, text: str, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Invia ``text`` come testo letterale alla sessione tmux (send-keys -l)."""
    return run_tmux_action(
        host,
        f"tmux send-keys -t {_sh_quote(session)} -l {_sh_quote(text)}",
        cfg,
    )


def tmux_send_enter(host: Host, session: str, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Invia Enter alla sessione tmux."""
    return run_tmux_action(host, f"tmux send-keys -t {_sh_quote(session)} Enter", cfg)


def tmux_send_raw(
    host: Host, session: str, keys: str, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Invia tasti tmux non letterali (es. 'C-c', 'Up', 'BSpace')."""
    return run_tmux_action(host, f"tmux send-keys -t {_sh_quote(session)} {keys}", cfg)


def tmux_new_window(
    host: Host, session: str, name: str | None = None, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Crea una nuova finestra nella sessione remota."""
    cmd = f"tmux new-window -t {_sh_quote(session)}"
    if name:
        cmd += f" -n {_sh_quote(name)}"
    return run_tmux_action(host, cmd, cfg)


def tmux_rename_window(
    host: Host, session: str, window_id: str, new_name: str, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Rinomina una finestra della sessione remota."""
    return run_tmux_action(
        host,
        f"tmux rename-window -t {_sh_quote(f'{session}:{window_id}')} {_sh_quote(new_name)}",
        cfg,
    )


def tmux_kill_window(
    host: Host, session: str, window_id: str, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Chiude una finestra della sessione remota."""
    return run_tmux_action(host, f"tmux kill-window -t {_sh_quote(f'{session}:{window_id}')}", cfg)


def tmux_session_details(host: Host, name: str, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Dettagli di una sessione remota (nome, finestre, stato, dimensione, client)."""
    tab = "\t"
    fmt = tab.join(
        [
            "#S",
            "#W",
            "#{session_created}",
            "#{session_width}x#{session_height}",
            "#{session_attached}",
        ]
    )
    # list-sessions non accetta -t: filtro con grep ^nome<TAB>
    return run_tmux_action(
        host,
        f"tmux list-sessions -F {_sh_quote(fmt)} | grep -F {_sh_quote(name + tab)}",
        cfg,
    )


def tmux_present(host: Host, cfg: SshConfig | None = None) -> bool:
    """True se ``tmux`` è installato (localmente o sul server)."""
    cfg = cfg or SshConfig()
    command = "command -v tmux >/dev/null 2>&1 && echo yes || echo no"
    if host.is_local():
        proc = _run_local(command)
        return proc.returncode == 0 and proc.stdout.strip() == "yes"
    proc = run_remote_command(host, command, cfg, batch=True)
    if proc.returncode == 0 and proc.stdout.strip() == "yes":
        return True
    if cfg.password_provider and cfg.password_provider(host):
        proc = run_remote_command(host, command, cfg, with_password=True)
        return proc.returncode == 0 and proc.stdout.strip() == "yes"
    return False


def tcp_ping(host: Host, timeout: float = 2.0) -> tuple[bool, str]:
    """Verifica la raggiungibilità TCP sulla porta SSH dell'host.

    Per gli host locali controlla solo la presenza di tmux (niente socket remoto).
    Ritorna (ok, dettaglio).
    """
    import socket

    if host.is_local():
        import shutil

        if shutil.which("tmux"):
            return True, "tmux locale disponibile"
        return False, "tmux locale non installato"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect((host.host, host.port or 22))
        except socket.gaierror as exc:
            return False, f"host non risolvibile ({exc})"
        except OSError as exc:
            return False, f"non raggiungibile ({exc.strerror or exc})"
        return True, f"porta {host.port or 22} aperta"
    finally:
        sock.close()
