"""Catalogo locale di snippet (comandi shell predefiniti).

I snippet sono script/commandi da eseguire in broadcast su più host.
Salvati in JSON: ``snippets_file`` da config, default ``snippets.json``
nella directory di configurazione.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config, default_config_dir


@dataclass
class Snippet:
    name: str
    command: str
    description: str = ""


def _snippets_path(cfg: Config) -> Path:
    if cfg.snippets_file:
        return Path(cfg.snippets_file).expanduser()
    base = cfg.path.parent if cfg.path else default_config_dir()
    return base / "snippets.json"


def load_snippets(cfg: Config) -> list[Snippet]:
    path = _snippets_path(cfg)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Snippet] = []
    for item in data:
        if isinstance(item, dict) and item.get("name") and item.get("command"):
            out.append(
                Snippet(
                    name=str(item["name"]),
                    command=str(item["command"]),
                    description=str(item.get("description") or ""),
                )
            )
    return out


def save_snippets(cfg: Config, snippets: list[Snippet]) -> None:
    path = _snippets_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {"name": s.name, "command": s.command, "description": s.description} for s in snippets
    ]
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def add_snippet(cfg: Config, snippet: Snippet) -> None:
    snippets = load_snippets(cfg)
    snippets = [s for s in snippets if s.name != snippet.name]
    snippets.insert(0, snippet)
    save_snippets(cfg, snippets)


def remove_snippet(cfg: Config, name: str) -> None:
    snippets = [s for s in load_snippets(cfg) if s.name != name]
    save_snippets(cfg, snippets)
