"""Logging locale strutturato (audit trail) delle sessioni interattive.

Quando ``audit_log = true`` in config, le sessioni interattive (attach, nuova,
shell) vengono registrate localmente in file compressi:
``~/.local/share/bravoric-ssh-client/logs/YYYYMMDD_ALIAS_SESSION.log.gz``.

Due tecniche, complementari:

- **script wrap** (shell interattiva locale/remota): wrappa il comando esterno
  con ``script -q -f -c`` e comprime l'output in gz al termine.
- **tmux pipe-pane** (attach): inietta ``tmux pipe-pane -o 'gzip -c >> file'``
  durante l'attach e lo chiude al detach, così l'I/O della sessione viene
  registrato lato server anche senza tenere un processo locale.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

from ..config import Config
from .shellutil import sh_quote


def default_logs_dir() -> Path:
    """~/.local/share/bravoric-ssh-client/logs (XDG data dir)."""
    xdg = __import__("os").environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "bravoric-ssh-client" / "logs"


def _sanitize(name: str) -> str:
    """Rende un alias/sessione adatto a un nome file (solo [A-Za-z0-9._-])."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return cleaned or "anon"


def audit_log_path(cfg: Config, host_alias: str, session: str | None = None) -> Path:
    """Percorso del file di log compresso per la sessione."""
    import datetime

    logs_dir = cfg_path_logs_dir(cfg)
    date = datetime.datetime.now().strftime("%Y%m%d")
    tail = _sanitize(session) if session else "shell"
    return logs_dir / f"{date}_{_sanitize(host_alias)}_{tail}.log.gz"


def cfg_path_logs_dir(cfg: Config) -> Path:
    """Directory dei log: logs/ sotto la dir config se possibile, altrimenti default."""
    if cfg.path and cfg.path.parent:
        d = cfg.path.parent / "logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            pass
    d = default_logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def script_available() -> bool:
    """True se il comando ``script`` (util-linux-script) è installato.

    Serve per registrare l'I/O della shell interattiva locale/remota senza tmux.
    Se assente, l'audit si limita alle sessioni tmux (pipe-pane).
    """
    import shutil

    return shutil.which("script") is not None


def script_wrap(command: str, out_gz: Path) -> str:
    """Wrappa ``command`` con ``script`` scrivendo su file e comprimendo in gz.

    Il file temporaneo (plain) viene creato accanto al .gz e rimosso da gzip.
    Ritorna una stringa di shell pronta per ``sh -c``.
    """
    plain = out_gz.with_name(out_gz.name.removesuffix(".gz") + ".raw")
    cmd = sh_quote(command)
    return f"script -q -f {sh_quote(str(plain))} -c {cmd}; gzip -f {sh_quote(str(plain))}"


def tmux_pipe_pane_enable(session: str, out_gz: Path) -> str:
    """Comando tmux che inizia a registrare la sessione su un gz via gzip."""
    target = sh_quote(session)
    log = sh_quote(str(out_gz))
    return f"tmux pipe-pane -o -t {target} 'gzip -c >> {log}'"


def tmux_pipe_pane_disable(session: str) -> str:
    """Comando tmux che chiude il pipe (pipe-pane senza comando)."""
    return f"tmux pipe-pane -t {sh_quote(session)}"


def read_log_gz(path: Path, *, max_lines: int = 0) -> str:
    """Legge un file di log compresso (test/utilità). max_lines=0 = tutto."""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if max_lines > 0:
        lines = lines[-max_lines:]
    return "".join(lines)
