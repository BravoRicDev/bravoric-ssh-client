"""Cronologia delle sessioni tmux attachate di recente.

Salvata in JSON nella directory di config (history.json). Le voci sono coppie
(host_alias, session_name) in ordine di utilizzo (più recente in testa).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config, default_config_dir


@dataclass
class HistoryEntry:
    host: str
    session: str

    def key(self) -> str:
        return f"{self.host}/{self.session}"


def _history_path(cfg: Config) -> Path:
    base = cfg.path.parent if cfg.path else default_config_dir()
    return base / "history.json"


def load_history(cfg: Config) -> list[HistoryEntry]:
    path = _history_path(cfg)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[HistoryEntry] = []
    for item in data:
        if isinstance(item, dict) and item.get("host") and item.get("session"):
            out.append(HistoryEntry(item["host"], item["session"]))
    return out


def record_history(cfg: Config, host: str, session: str) -> None:
    """Registra l'utilizzo di una sessione (in testa, senza duplicati)."""
    if not session:
        return
    path = _history_path(cfg)
    entries = load_history(cfg)
    new = HistoryEntry(host, session)
    entries = [e for e in entries if e.key() != new.key()]
    entries.insert(0, new)
    entries = entries[: max(1, cfg.history_size or 10)]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([{"host": e.host, "session": e.session} for e in entries], indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
