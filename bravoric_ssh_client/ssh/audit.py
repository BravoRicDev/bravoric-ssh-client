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

# Tetti dell'audit. Senza di essi il log di una sessione lunga cresce senza fine:
# sul campo si e' visto un singolo file da 5 GB e ~14 GB complessivi nella cartella,
# perche' il writer e' uno stream gzip mai chiuso e non c'era alcuna pulizia.
MAX_LOG_BYTES = 64 * 1024 * 1024  # tetto per singolo file (compresso)
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # budget complessivo della cartella
MAX_AGE_DAYS = 30  # eta' massima di un log


def _unlink(path: Path) -> bool:
    """Rimuove un file; True se e' stato rimosso davvero."""
    try:
        path.unlink()
        return True
    except OSError:
        return False


def prune_logs(
    logs_dir: Path,
    *,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_age_days: int = MAX_AGE_DAYS,
    keep: Path | None = None,
) -> list[Path]:
    """Tiene la cartella dei log entro un budget, rimuovendo i file piu' vecchi.

    Due criteri, in ordine: prima i file piu' vecchi di ``max_age_days``, poi —
    se il totale supera ancora ``max_total_bytes`` — si continua a eliminare dal
    piu' vecchio finche' il budget e' rispettato. ``keep`` (il file che si sta per
    scrivere) non viene mai toccato. Ritorna i file rimossi.
    """
    import time

    try:
        candidates = [p for p in logs_dir.iterdir() if p.is_file() and p != keep]
    except OSError:
        return []

    entries: list[tuple[float, int, Path]] = []
    for p in candidates:
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
    entries.sort()  # i piu' vecchi per primi

    removed: list[Path] = []
    cutoff = time.time() - max_age_days * 86400
    for mtime, _size, p in list(entries):
        if mtime < cutoff and _unlink(p):
            removed.append(p)
            entries.remove((mtime, _size, p))

    total = sum(size for _mtime, size, _p in entries)
    for _mtime, size, p in entries:
        if total <= max_total_bytes:
            break
        if _unlink(p):
            removed.append(p)
            total -= size
    return removed


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
    """Percorso del file di log compresso per la sessione.

    Prima di restituirlo applica la retention: la cartella dei log non deve poter
    riempire il disco, quindi a ogni nuova registrazione i file piu' vecchi (o
    eccedenti il budget) vengono rimossi. Il file che stiamo per scrivere e' protetto.
    """
    import datetime

    logs_dir = cfg_path_logs_dir(cfg)
    date = datetime.datetime.now().strftime("%Y%m%d")
    tail = _sanitize(session) if session else "shell"
    out = logs_dir / f"{date}_{_sanitize(host_alias)}_{tail}.log.gz"
    prune_logs(logs_dir, keep=out)
    return out


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
    """Comando tmux che inizia a registrare la sessione su un gz via gzip.

    Il writer e' limitato a ``MAX_LOG_BYTES``: ``head -c`` chiude il flusso quando
    il tetto e' raggiunto, cosi' un singolo file non puo' piu' crescere senza limite.
    L'intero comando interno passa da ``sh_quote``: prima veniva composto a mano
    infilando un percorso gia' quotato dentro altri apici, cosa che si rompe se il
    percorso contiene un apice.
    """
    target = sh_quote(session)
    inner = f"gzip -c | head -c {MAX_LOG_BYTES} >> {sh_quote(str(out_gz))}"
    return f"tmux pipe-pane -o -t {target} {sh_quote(inner)}"


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
