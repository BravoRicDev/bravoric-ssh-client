"""Profili di rotazione: insiemi di (host, sessione) da monitorare in auto-cycle.

Un profilo è un gruppo di coppie (host_alias, session) — es. "tutti i server",
"claude code", "produzione" — che l'utente crea col launcher e può riavviare
in un colpo (finestre separate) o aprire in modalità observe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, default_config_dir


@dataclass
class Rotation:
    """Profilo di rotazione: lista di (host_alias, session)."""

    name: str
    entries: list[tuple[str, str]] = field(default_factory=list)

    def unique_entries(self) -> list[tuple[str, str]]:
        """Senza doppioni (stessa coppia host/sessione)."""
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for host, session in self.entries:
            key = f"{host}/{session}"
            if key not in seen:
                seen.add(key)
                out.append((host, session))
        return out


def _rotations_path(cfg: Config) -> Path:
    base = cfg.path.parent if cfg.path else default_config_dir()
    return base / "rotations.json"


def load_rotations(cfg: Config) -> list[Rotation]:
    path = _rotations_path(cfg)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Rotation] = []
    for item in data:
        if isinstance(item, dict) and item.get("name"):
            entries = item.get("entries") or []
            pairs = []
            for e in entries:
                if isinstance(e, dict) and e.get("host") and e.get("session"):
                    pairs.append((e["host"], e["session"]))
            if pairs:
                out.append(Rotation(name=str(item["name"]), entries=pairs))
    return out


def save_rotations(cfg: Config, rotations: list[Rotation]) -> None:
    path = _rotations_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {"name": r.name, "entries": [{"host": h, "session": s} for h, s in r.unique_entries()]}
        for r in rotations
    ]
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def add_rotation(cfg: Config, rotation: Rotation) -> None:
    """Aggiunge (o sostituisce per nome) un profilo nel catalogo."""
    rotations = load_rotations(cfg)
    rotations = [r for r in rotations if r.name != rotation.name]
    rotations.insert(0, rotation)
    save_rotations(cfg, rotations)


def remove_rotation(cfg: Config, name: str) -> None:
    rotations = [r for r in load_rotations(cfg) if r.name != name]
    save_rotations(cfg, rotations)
