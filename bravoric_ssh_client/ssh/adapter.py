"""Adapter SSH: esecuzione di comandi remoti non interattivi.

Per il listino sessioni tmux serve autenticazione non interattiva. Invece di
dipendere da ``sshpass`` (solo Linux), usiamo il meccanismo standard di OpenSSH
``SSH_ASKPASS`` con ``SSH_ASKPASS_REQUIRE=force``: funziona su Linux, macOS e
Windows (OpenSSH recente) con un unico helper che emette la password da ambiente.
Se l'host usa chiave/agent, si procede con ``BatchMode=yes`` senza password.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Host
from .shellutil import sh_quote, write_askpass_helper, write_askpass_helper_single

logger = logging.getLogger("bravoric.ssh")


DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_MAX_DELAY = 30.0

MAX_AUTH_FAILURES = 5
AUTH_LOCKOUT_SECONDS = 300

CONTROLMASTER_PATH = "~/.ssh/bravoric-socket-%r@%h-%p"
SERVER_ALIVE_INTERVAL = "30"
SERVER_ALIVE_COUNT_MAX = "3"


class ErrorCategory(str, enum.Enum):
    AUTH_FAILED = "auth_failed"
    HOST_UNREACHABLE = "host_unreachable"
    TIMEOUT = "timeout"
    COMMAND_NOT_FOUND = "command_not_found"
    CONNECTION_RESET = "connection_reset"
    PERMISSION_DENIED = "permission_denied"
    FILE_TOO_LARGE = "file_too_large"
    CONFIG = "config"
    UNKNOWN = "unknown"


class SshError(Exception):
    def __init__(
        self,
        message: str,
        code: str,
        category: ErrorCategory,
        recoverable: bool = False,
        detail: str = "",
        cause: Exception | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.category = category
        self.recoverable = recoverable
        self.detail = detail
        self.cause = cause

    @staticmethod
    def from_subprocess(proc: subprocess.CompletedProcess[str], context: str = "") -> SshError:
        stderr = (proc.stderr or "").lower()
        stdout = (proc.stdout or "").lower()
        combined = stderr + " " + stdout

        if "permission denied" in combined or "keyboard-interactive" in combined:
            return SshError(
                f"Permesso negato su {context}",
                code="permission_denied",
                category=ErrorCategory.PERMISSION_DENIED,
                recoverable=False,
                detail=(proc.stderr or "").strip(),
            )
        if "authentication failed" in combined or "auth fail" in combined:
            return SshError(
                f"Autenticazione fallita su {context}",
                code="auth_failed",
                category=ErrorCategory.AUTH_FAILED,
                recoverable=True,
                detail=(proc.stderr or "").strip(),
            )
        if (
            "no route to host" in combined
            or "could not resolve" in combined
            or "name or service not known" in combined
            or "network is unreachable" in combined
        ):
            return SshError(
                f"Host non raggiungibile: {context}",
                code="host_unreachable",
                category=ErrorCategory.HOST_UNREACHABLE,
                recoverable=True,
                detail=(proc.stderr or "").strip(),
            )
        if "connection reset" in combined or "broken pipe" in combined:
            return SshError(
                f"Connessione interrotta su {context}",
                code="connection_reset",
                category=ErrorCategory.CONNECTION_RESET,
                recoverable=True,
                detail=(proc.stderr or "").strip(),
            )
        if "timed out" in combined or "timeout" in combined:
            return SshError(
                f"Timeout connessione su {context}",
                code="timeout",
                category=ErrorCategory.TIMEOUT,
                recoverable=True,
                detail=(proc.stderr or "").strip(),
            )
        if "command not found" in combined or "not found" in combined:
            return SshError(
                f"Comando non trovato su {context}",
                code="command_not_found",
                category=ErrorCategory.COMMAND_NOT_FOUND,
                recoverable=False,
                detail=(proc.stderr or "").strip(),
            )
        return SshError(
            f"Errore generico su {context}",
            code="unknown",
            category=ErrorCategory.UNKNOWN,
            recoverable=False,
            detail=(proc.stderr or "").strip(),
        )


_auth_failures: dict[str, list[float]] = {}


def _record_auth_failure(host_alias: str) -> None:
    now = time.time()
    if host_alias not in _auth_failures:
        _auth_failures[host_alias] = []
    _auth_failures[host_alias].append(now)
    cutoff = now - 600
    _auth_failures[host_alias] = [t for t in _auth_failures[host_alias] if t > cutoff]


def _is_auth_locked(host_alias: str) -> tuple[bool, int]:
    now = time.time()
    failures = _auth_failures.get(host_alias, [])
    recent = [t for t in failures if now - t < AUTH_LOCKOUT_SECONDS]
    if len(recent) >= MAX_AUTH_FAILURES:
        oldest = min(recent)
        remaining = int(AUTH_LOCKOUT_SECONDS - (now - oldest))
        return True, max(0, remaining)
    return False, 0


def _reset_auth_failures(host_alias: str) -> None:
    _auth_failures.pop(host_alias, None)


DEFAULT_CONNECT_TIMEOUT = "4"

# Shell/processi che indicano "nessun processo in primo piano" (pane idle).
# NB: 'node' NON è incluso: molte TUI (es. opencode) girano proprio come 'node'.
SHELL_COMMANDS = {
    "bash",
    "zsh",
    "sh",
    "dash",
    "fish",
    "ksh",
    "tcsh",
    "csh",
    "ash",
    "login",
    "tmux",
    "nu",
    "xonsh",
    "elvish",
    "oil",
    "osh",
    "pwsh",
}

# Delimitatore "impossibile": separa i campi di una formattazione tmux senza
# collidere con path/titoli che possono contenere spazi, pipe o altri separatori.
PANE_DELIM = "|||__BRAVORIC_DELIM__|||"
WINDOW_DELIM = "|||__BRAVORIC_WIN__|||"


def clean_cmd(raw: str) -> str:
    """Nome base del processo in primo piano (senza path né '-' iniziale)."""
    lines = (raw or "").strip().splitlines()
    if not lines:
        return ""
    return lines[0].strip().lstrip("-").split("/")[-1]


def _safe_int(value: str, default: int = 0) -> int:
    """Converte in int tollerando valori vuoti o sporchi di tmux."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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
    use_controlmaster: bool = True


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


def _expand_control_path(host: Host) -> str:
    path = os.path.expanduser(CONTROLMASTER_PATH)
    # Assicurati che la directory ~/.ssh esista
    os.makedirs(os.path.expanduser("~/.ssh"), exist_ok=True)
    return path


def _ssh_base_args(host: Host, cfg: SshConfig, *, batch: bool) -> list[str]:
    args = []
    if batch:
        args += ["-o", "BatchMode=yes"]

    # Connect timeout per-host o globale
    timeout = getattr(host, "connect_timeout", None) or cfg.connect_timeout
    if timeout is not None:
        args += ["-o", f"ConnectTimeout={timeout}"]
    else:
        args += ["-o", f"ConnectTimeout={DEFAULT_CONNECT_TIMEOUT}"]

    args += [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
        "-o",
        f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
    ]

    if cfg.use_controlmaster and not host.is_local():
        args += [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={_expand_control_path(host)}",
            "-o",
            "ControlPersist=10m",
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


def _is_transient_error(proc: subprocess.CompletedProcess[str]) -> bool:
    stderr = (proc.stderr or "").lower()
    stdout = (proc.stdout or "").lower()
    combined = stderr + " " + stdout
    return any(
        kw in combined
        for kw in [
            "timed out",
            "timeout",
            "connection reset",
            "broken pipe",
            "connection refused",
            "no route to host",
            "network is unreachable",
            "host key verification failed",
        ]
    )


def _classify_ssh_error(
    proc: subprocess.CompletedProcess[str], host_alias: str, command: str
) -> SshError:
    stderr = (proc.stderr or "").lower()
    stdout = (proc.stdout or "").lower()
    combined = stderr + " " + stdout
    if "permission denied" in combined or "keyboard-interactive" in combined:
        return SshError(
            f"Autenticazione fallita su {host_alias}",
            code="auth_failed",
            category=ErrorCategory.AUTH_FAILED,
            detail=(proc.stderr or "").strip(),
        )
    if "timed out" in combined or "timeout" in combined:
        return SshError(
            f"Timeout connessione su {host_alias}",
            code="timeout",
            category=ErrorCategory.TIMEOUT,
            recoverable=True,
            detail=(proc.stderr or "").strip(),
        )
    if "could not resolve" in combined or "name or service not known" in combined:
        return SshError(
            f"Host non raggiungibile: {host_alias}",
            code="host_unreachable",
            category=ErrorCategory.HOST_UNREACHABLE,
            detail=(proc.stderr or "").strip(),
        )
    if "connection reset" in combined or "broken pipe" in combined:
        return SshError(
            f"Connessione interrotta su {host_alias}",
            code="connection_reset",
            category=ErrorCategory.CONNECTION_RESET,
            recoverable=True,
            detail=(proc.stderr or "").strip(),
        )
    if "command not found" in combined or "not found" in combined:
        return SshError(
            f"Comando non trovato su {host_alias}: {command}",
            code="command_not_found",
            category=ErrorCategory.COMMAND_NOT_FOUND,
            detail=(proc.stderr or "").strip(),
        )
    return SshError(
        f"Errore SSH generico su {host_alias}: exit {proc.returncode}",
        code="unknown",
        category=ErrorCategory.UNKNOWN,
        detail=(proc.stderr or "").strip(),
    )


def run_remote_command(
    host: Host,
    command: str,
    cfg: SshConfig | None = None,
    *,
    batch: bool = False,
    with_password: bool = False,
    timeout: int = 15,
    max_retries: int = DEFAULT_MAX_RETRIES,
    stream_callback: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Esegue ``command`` sul server.

    - ``batch``: rifiuta prompt interattivi (utile per capire se serve password).
    - ``with_password``: fornisce le password via SSH_ASKPASS helper (multi-host
      per il caso bastion + host finale).
    - ``max_retries``: retry per errori transienti (default 3, 0 = nessun retry).
    - ``stream_callback``: callback opzionale per ricevere l'output in tempo reale.
    """
    cfg = cfg or SshConfig()

    # Rate limiting autenticazione
    locked, remaining = _is_auth_locked(host.alias)
    if locked:
        raise SshError(
            f"Host {host.alias} temporaneamente bloccato per troppi fallimenti autenticazione. Riprova tra {remaining}s.",
            code="auth_locked",
            category=ErrorCategory.AUTH_FAILED,
            detail=f"Lockout attivo per {remaining} secondi.",
        )

    ssh = _find_ssh(cfg)
    args = [ssh, *_ssh_base_args(host, cfg, batch=batch), command]
    env = dict(os.environ)

    def _run_with_retry(env_dict: dict[str, str]) -> subprocess.CompletedProcess[str]:
        proc = None
        for attempt in range(max_retries + 1):
            try:
                if stream_callback:
                    # Streaming output con subprocess.Popen
                    p = subprocess.Popen(
                        args,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env_dict,
                    )
                    try:
                        stdout_lines = []
                        while True:
                            line = p.stdout.readline()
                            if not line:
                                break
                            stdout_lines.append(line)
                            stream_callback(line)
                        p.wait(timeout=timeout)
                        stderr = p.stderr.read()
                        proc = subprocess.CompletedProcess(
                            args, p.returncode, "".join(stdout_lines), stderr
                        )
                    finally:
                        if p.poll() is None:
                            p.kill()
                            p.wait()
                else:
                    proc = subprocess.run(
                        args, capture_output=True, text=True, timeout=timeout, env=env_dict
                    )

                if proc.returncode == 0:
                    _reset_auth_failures(host.alias)
                    return proc

                if not _is_transient_error(proc):
                    break
            except subprocess.TimeoutExpired as e:
                if attempt == max_retries:
                    raise SshError(
                        f"Timeout connessione su {host.alias}",
                        code="timeout",
                        category=ErrorCategory.TIMEOUT,
                        recoverable=True,
                        cause=e,
                    ) from e
            except Exception as e:
                if attempt == max_retries:
                    raise SshError(
                        f"Errore di connessione su {host.alias}: {e}",
                        code="connection_failed",
                        category=ErrorCategory.HOST_UNREACHABLE,
                        cause=e,
                    ) from e

            # Exponential backoff
            delay = min(DEFAULT_RETRY_BASE_DELAY * (2**attempt), DEFAULT_RETRY_MAX_DELAY)
            logger.warning(
                "ssh retry attempt=%d host=%s delay=%.2f", attempt + 1, host.alias, delay
            )
            time.sleep(delay)

        if proc and proc.returncode != 0:
            err = SshError.from_subprocess(proc, host.alias)
            if err.category in (ErrorCategory.AUTH_FAILED, ErrorCategory.PERMISSION_DENIED):
                _record_auth_failure(host.alias)
        return proc or subprocess.CompletedProcess(args, -1, "", "Errore sconosciuto")

    if with_password:
        mapping = _password_mapping(host, cfg)
        if not mapping:
            raise ValueError("with_password=True ma nessuna password dal provider")
        has_jump = bool(host.jump_host and cfg.jump_resolver and cfg.jump_resolver(host))
        if not has_jump:
            password = next(iter(mapping.values()))
            helper = _write_askpass_helper()
            try:
                env["SSH_ASKPASS"] = str(helper)
                env["SSH_ASKPASS_REQUIRE"] = "force"
                env.setdefault("DISPLAY", ":0")
                env["BRAVORIC_PASSWORD"] = password
                return _run_with_retry(env)
            finally:
                helper.unlink(missing_ok=True)
        helper = write_askpass_helper(mapping)
        try:
            env["SSH_ASKPASS"] = str(helper)
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")
            return _run_with_retry(env)
        finally:
            helper.unlink(missing_ok=True)
    return _run_with_retry(env)


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
    mode: str = ""


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


@dataclass
class TmuxWindowInfo:
    """Descrizione strutturata di una finestra tmux."""

    index: int = 0
    name: str = ""
    active: bool = False
    pane_count: int = 1
    layout: str = ""


@dataclass
class WindowListResult:
    """Esito della lettura strutturata delle finestre di una sessione."""

    ok: bool = False
    error: str = ""
    windows: list[TmuxWindowInfo] = field(default_factory=list)


def tmux_list_windows_parsed(
    host: Host, session: str, cfg: SshConfig | None = None
) -> WindowListResult:
    """Elenca le finestre di una sessione in forma strutturata (indice, nome, …)."""
    fmt = WINDOW_DELIM.join(
        [
            "#{window_index}",
            "#{window_name}",
            "#{window_active}",
            "#{window_panes}",
            "#{window_layout}",
        ]
    )
    res = run_tmux_action(
        host, f"tmux list-windows -t {_sh_quote(session)} -F {_sh_quote(fmt)}", cfg
    )
    if not res.ok:
        return WindowListResult(
            ok=False, error=(res.stderr or "").strip() or "list-windows fallito"
        )
    windows: list[TmuxWindowInfo] = []
    for line in (res.stdout or "").strip().splitlines():
        parts = line.split(WINDOW_DELIM)
        if len(parts) < 4:
            continue
        windows.append(
            TmuxWindowInfo(
                index=_safe_int(parts[0]),
                name=parts[1],
                active=parts[2].strip() == "1",
                pane_count=_safe_int(parts[3], default=1),
                layout=parts[4] if len(parts) > 4 else "",
            )
        )
    return WindowListResult(ok=True, windows=windows)


def tmux_select_window(
    host: Host, session: str, window_index: int | str, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Rende attiva (visibile) la finestra ``window_index`` della sessione."""
    target = f"{session}:{window_index}"
    return run_tmux_action(host, f"tmux select-window -t {_sh_quote(target)}", cfg)


def tmux_capture_pane(
    host: Host,
    session: str,
    cfg: SshConfig | None = None,
    *,
    lines: int = 200,
    window: str | None = None,
    escape: bool = False,
) -> TmuxActionResult:
    """Cattura il contenuto della sessione tmux remota (ultime ``lines`` righe).

    ``window`` opzionale: ``session:index`` per catturare una finestra specifica.
    ``escape`` opzionale: se True, include i codici escape di colore e stile (-e).
    """
    target = session if window is None else f"{session}:{window}"
    esc_flag = "-e " if escape else ""
    return run_tmux_action(
        host,
        f"tmux capture-pane {esc_flag}-p -t {_sh_quote(target)} -S -{int(lines)}",
        cfg,
    )


def tmux_send_keys(
    host: Host,
    session: str,
    text: str,
    cfg: SshConfig | None = None,
    *,
    enter: bool = False,
) -> TmuxActionResult:
    """Invia ``text`` come testo letterale alla sessione tmux (send-keys -l).

    Con ``enter=True`` concatena atomicamente l'invio del tasto Enter.
    """
    if enter:
        cmd = f"tmux send-keys -t {_sh_quote(session)} -l {_sh_quote(text)} \\; send-keys -t {_sh_quote(session)} Enter"
    else:
        cmd = f"tmux send-keys -t {_sh_quote(session)} -l {_sh_quote(text)}"
    return run_tmux_action(host, cmd, cfg)


def tmux_send_input(
    host: Host,
    session: str,
    text: str,
    cfg: SshConfig | None = None,
    *,
    enter: bool = True,
    mode: str = "auto",
    bracketed: bool = True,
    settle_delay: float = 0.0,
    capture_lines: int = 0,
) -> TmuxActionResult:
    """Invia input a una sessione tmux con gestione atomica di invio e multiriga.

    Modalità (``mode``):
    - ``auto``: usa bracketed paste se il testo ha newline, tabulazioni, lunghezza > 100
      o caratteri di controllo; altrimenti usa ``keys`` atomico.
    - ``paste``: forza bracketed paste via buffer tmux dedicato caricato in base64.
    - ``keys``: forza ``send-keys -l`` atomico.

    Parametri avanzati:
    - ``enter``: se True (default), invia il tasto Enter al termine.
    - ``settle_delay``: secondi di attesa (es. 0.2) prima di Enter (utile per TUI lente).
    - ``capture_lines``: se > 0, concatena la cattura delle ultime N righe della pane
      nello stesso comando atomico, restituendole in ``res.stdout``.
    """
    import base64
    import time
    import uuid

    cfg = cfg or SshConfig()
    mode_val = (mode or "auto").strip().lower()
    if mode_val not in ("auto", "paste", "keys"):
        mode_val = "auto"

    if not text:
        if enter:
            res = tmux_send_enter(host, session, cfg)
            res.mode = "keys"
            return res
        return TmuxActionResult(ok=True, stdout="", stderr="", mode="noop")

    use_paste = mode_val == "paste" or (
        mode_val == "auto"
        and ("\n" in text or "\t" in text or len(text) > 100 or any(ord(c) < 32 for c in text))
    )

    capture_part = (
        f" && tmux capture-pane -p -t {_sh_quote(session)} -S -{int(capture_lines)}"
        if capture_lines > 0
        else ""
    )

    if use_paste:
        name = f"bravoric_input_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        flag = "-p " if bracketed else ""
        delay_part = f" && sleep {settle_delay:.2f}" if settle_delay > 0 and enter else ""
        enter_part = (
            f"{delay_part} && tmux send-keys -t {_sh_quote(session)} Enter" if enter else ""
        )
        cmd = (
            f"printf %s {_sh_quote(b64)} | base64 -d | tmux load-buffer -b {_sh_quote(name)} - && "
            f"{{ tmux paste-buffer {flag}-b {_sh_quote(name)} -t {_sh_quote(session)}"
            f"{enter_part}{capture_part}; rc=$?; "
            f"tmux delete-buffer -b {_sh_quote(name)} 2>/dev/null; exit $rc; }}"
        )
        res = run_tmux_action(host, cmd, cfg)
        res.mode = "paste"
        return res

    delay_part = f" && sleep {settle_delay:.2f}" if settle_delay > 0 and enter else ""
    enter_part = f"{delay_part} && tmux send-keys -t {_sh_quote(session)} Enter" if enter else ""
    cmd = f"tmux send-keys -l -t {_sh_quote(session)} {_sh_quote(text)}{enter_part}{capture_part}"
    res = run_tmux_action(host, cmd, cfg)
    res.mode = "keys"
    return res


def tmux_send_enter(host: Host, session: str, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Invia Enter alla sessione tmux."""
    return run_tmux_action(host, f"tmux send-keys -t {_sh_quote(session)} Enter", cfg)


def tmux_send_raw(
    host: Host, session: str, keys: str, cfg: SshConfig | None = None
) -> TmuxActionResult:
    """Invia tasti tmux non letterali (es. 'C-c', 'Up', 'BSpace')."""
    return run_tmux_action(host, f"tmux send-keys -t {_sh_quote(session)} {keys}", cfg)


def tmux_pane_command(host: Host, session: str, cfg: SshConfig | None = None) -> TmuxActionResult:
    """Restituisce il comando in primo piano nella pane attiva (es. 'node', 'bash').

    Serve a capire se nella sessione gira una TUI/processo o solo la shell.
    """
    return run_tmux_action(
        host,
        f"tmux display-message -p -t {_sh_quote(session)} '#{{pane_current_command}}'",
        cfg,
    )


@dataclass
class PaneInfoResult:
    """Ispazione atomica della pane attiva (processo, CWD, PID, titolo, geometria)."""

    ok: bool = False
    error: str = ""
    command: str = ""
    cwd: str = ""
    pid: int = 0
    title: str = ""
    width: int = 0
    height: int = 0
    is_shell: bool = False


def tmux_pane_info(host: Host, session: str, cfg: SshConfig | None = None) -> PaneInfoResult:
    """Legge in UNA sola chiamata processo, CWD, PID, titolo e dimensioni della pane.

    Ritorna anche ``is_shell``: True se il processo in primo piano è una shell
    (pane idle, nessun comando/TUI attivo).
    """
    fmt = PANE_DELIM.join(
        [
            "#{pane_current_command}",
            "#{pane_current_path}",
            "#{pane_pid}",
            "#{pane_title}",
            "#{pane_width}",
            "#{pane_height}",
        ]
    )
    res = run_tmux_action(
        host,
        f"tmux display-message -p -t {_sh_quote(session)} {_sh_quote(fmt)}",
        cfg,
    )
    if not res.ok:
        return PaneInfoResult(ok=False, error=(res.stderr or "").strip() or f"exit {res.ok}")
    parts = (res.stdout or "").strip().split(PANE_DELIM)
    if len(parts) < 6:
        return PaneInfoResult(ok=False, error="Formato output tmux non valido")
    cmd = clean_cmd(parts[0])
    return PaneInfoResult(
        ok=True,
        command=cmd,
        cwd=parts[1],
        pid=_safe_int(parts[2]),
        title=parts[3],
        width=_safe_int(parts[4]),
        height=_safe_int(parts[5]),
        is_shell=cmd in SHELL_COMMANDS,
    )


def tmux_paste_buffer(
    host: Host,
    session: str,
    content: str,
    cfg: SshConfig | None = None,
    *,
    bracketed: bool = True,
    buffer_name: str | None = None,
) -> TmuxActionResult:
    """Incolla ``content`` nella sessione usando un buffer tmux (bracketed paste).

    Il testo viene caricato in un buffer tmux dedicato passando per base64: niente
    quoting ambiguo, niente file temporanei e nessuna differenza tra host locale e
    remoto. Con ``bracketed=True`` l'incolla usa ``paste-buffer -p`` così le TUI
    (editor, agenti) ricevono i marcatori di bracketed paste e non corrompono
    l'indentazione né scatenano auto-completamenti a metà riga.
    """
    import base64
    import time

    cfg = cfg or SshConfig()
    name = buffer_name or f"bravoric_transfer_{int(time.time() * 1000)}"
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    load_cmd = f"printf %s {_sh_quote(b64)} | base64 -d | tmux load-buffer -b {_sh_quote(name)} -"
    load = run_tmux_action(host, load_cmd, cfg)
    if not load.ok:
        return TmuxActionResult(
            ok=False,
            stdout=load.stdout,
            stderr=(load.stderr or "").strip() or "load-buffer fallito",
        )
    flag = "-p " if bracketed else ""
    res = run_tmux_action(
        host,
        f"tmux paste-buffer {flag}-b {_sh_quote(name)} -t {_sh_quote(session)}",
        cfg,
    )
    # Pulizia del buffer dedicato (best-effort: non deve influenzare l'esito).
    run_tmux_action(host, f"tmux delete-buffer -b {_sh_quote(name)} 2>/dev/null", cfg)
    return res


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
