"""Configurazione di bravoric-ssh-client.

Formato: TOML. Percorso di default: ~/.config/bravoric-ssh-client/config.toml
(overridabile con la variabile d'ambiente BRAVORIC_CONFIG).

Esempio minimo:
    [credentials]
    provider = "keyring"

    [[hosts]]
    alias = "web-prod"
    host = "server.example.com"
    user = "deploy"
    auth = "keyring"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _toml_str(value: str) -> str:
    """Quota una stringa TOML (minimalista, gestisce i casi comuni)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return _toml_str(str(value))


def default_config_dir() -> Path:
    """Directory di configurazione per piattaforma."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "bravoric-ssh-client"
    home = Path.home()
    if os.name == "nt":  # Windows
        base = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / "bravoric-ssh-client"
    return home / ".config" / "bravoric-ssh-client"


def default_config_path() -> Path:
    env = os.environ.get("BRAVORIC_CONFIG")
    if env:
        return Path(env).expanduser()
    return default_config_dir() / "config.toml"


# Metodi di autenticazione ammessi per un host.
# "" = usa il provider globale da [credentials] (default).
AUTH_METHODS = ("", "keyring", "plain", "key", "prompt")

# Tipi di tunnel SSH supportati: L (forward locale), R (remoto), D (SOCKS).
TUNNEL_KINDS = ("L", "R", "D")

# Indirizzi di bind considerati loopback (non esposti in rete).
LOOPBACK_BINDS = ("localhost", "127.0.0.1", "::1")


def is_loopback_bind(bind: str) -> bool:
    """True se l'indirizzo di bind è loopback (localhost/127.x/::1).

    Solo gli indirizzi loopback sono ammessi per i tunnel: un bind su 0.0.0.0
    o su un IP pubblico esporrebbe il forwarding all'intera rete.
    """
    b = (bind or "").strip()
    return b in LOOPBACK_BINDS or b.startswith("127.")


@dataclass
class Tunnel:
    """Definizione di un tunnel SSH (port forwarding).

    - ``kind``: "L" (locale), "R" (remoto), "D" (dinamico/SOCKS).
    - ``local_port``: porta locale (per -L/-D) o porta sul lato remoto (-R).
    - ``remote_host``/``remote_port``: destinazione del forward (per L/R).
    - ``bind``: indirizzo di ascolto (default localhost).
    """

    name: str = ""
    kind: str = "L"
    local_port: int = 0
    remote_host: str = ""
    remote_port: int = 0
    bind: str = "localhost"

    def local_addr(self) -> str:
        return f"{self.bind}:{self.local_port}"


@dataclass
class Host:
    alias: str
    host: str
    user: str | None = None
    port: int = 22
    auth: str = ""
    cred_key: str | None = None
    local: bool | None = None  # True = forza tmux locale (niente ssh)
    group: str | None = None  # gruppo/tag per organizzare gli host
    auto_cycle: bool | None = None  # True = rotazione automatica sessioni
    cycle_interval: int = 120  # secondi di inattività prima di ruotare
    jump_host: str | None = None  # alias del bastion/ProxyJump per raggiungere l'host
    tunnels: list[Tunnel] = field(default_factory=list)  # port forwarding attivi da TUI
    connect_timeout: int | None = None  # timeout connessione per-host (secondi)
    strict_host_key_checking: str = "accept-new"  # "accept-new" | "yes" | "no" | "off"
    extra: dict[str, Any] = field(default_factory=dict)

    def effective_user(self) -> str:
        return self.user or os.environ.get("USER") or os.environ.get("USERNAME") or ""

    def effective_cred_key(self) -> str:
        """Chiave usata nel provider credenziali (default user@host)."""
        if self.cred_key:
            return self.cred_key
        return f"{self.effective_user()}@{self.host}"

    def is_local(self) -> bool:
        """True se si deve usare il tmux locale (niente SSH)."""
        if self.local is not None:
            return bool(self.local)
        return self.host in ("localhost", "127.0.0.1", "::1")


class ConfigError(ValueError):
    """Errore di configurazione (file mancante o non valido)."""


@dataclass
class Config:
    credential_provider: str = "keyring"
    plain_file: str | None = None
    hosts: list[Host] = field(default_factory=list)
    path: Path | None = None
    restart_after_ssh: bool = False  # riapre la TUI dopo un attach/shell
    history_size: int = 10  # quante sessioni recenti tenere in cronologia
    audit_log: bool = (
        True  # registra I/O interattivo (script/pipe-pane) in log gz (default: attivo)
    )
    strict_host_key_checking: str = "accept-new"  # "accept-new" | "yes" | "no" | "off"
    connect_timeout: int | None = None  # timeout connessione default (secondi)
    snippets_file: str | None = None  # percorso del catalogo snippet (JSON)
    tunnels_file: str | None = (
        None  # percorso dello stato dei tunnel (JSON, default config_dir/tunnels.json)
    )

    def host(self, alias: str) -> Host | None:
        for h in self.hosts:
            if h.alias == alias:
                return h
        return None


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def _parse_tunnel(raw: dict[str, Any]) -> Tunnel:
    kind = str(raw.get("kind") or "L").upper()
    if kind not in TUNNEL_KINDS:
        raise ConfigError(f"Tunnel: kind '{kind}' non valido (attesi {TUNNEL_KINDS})")
    try:
        local_port = int(raw.get("local_port") or 0)
    except (TypeError, ValueError):
        raise ConfigError("Tunnel: local_port non valido") from None
    if local_port <= 0:
        raise ConfigError("Tunnel: local_port obbligatorio (porta > 0)")
    if kind in ("L", "R"):
        remote_host = str(raw.get("remote_host") or "")
        if not remote_host:
            raise ConfigError(f"Tunnel {kind}: manca remote_host")
        try:
            remote_port = int(raw.get("remote_port") or 0)
        except (TypeError, ValueError):
            raise ConfigError("Tunnel: remote_port non valido") from None
        if remote_port <= 0:
            raise ConfigError("Tunnel: remote_port obbligatorio (porta > 0)")
    else:
        remote_host = ""
        remote_port = 0
    bind = str(raw.get("bind") or "localhost")
    if not is_loopback_bind(bind):
        raise ConfigError(
            f"Tunnel: bind '{bind}' non è loopback e esporrebbe il tunnel in rete. "
            "Usa 'localhost', '127.0.0.1' o '::1'."
        )
    return Tunnel(
        name=str(raw.get("name") or ""),
        kind=kind,
        local_port=local_port,
        remote_host=remote_host,
        remote_port=remote_port,
        bind=bind,
    )


def _parse_host(alias: str, raw: dict[str, Any], defaults: dict[str, Any]) -> Host:
    if not alias.strip():
        raise ConfigError("Host senza alias valido")
    host = raw.get("host") or defaults.get("host")
    if not host:
        raise ConfigError(f"Host '{alias}': manca 'host'")
    auth = str(raw.get("auth") or defaults.get("auth") or "")
    if auth not in AUTH_METHODS:
        raise ConfigError(f"Host '{alias}': auth '{auth}' non valido (attesi {AUTH_METHODS})")
    try:
        port = int(raw.get("port") or defaults.get("port") or 22)
    except (TypeError, ValueError):
        raise ConfigError(f"Host '{alias}': porta non valida") from None
    tunnels: list[Tunnel] = []
    for t in raw.get("tunnels") or []:
        if isinstance(t, dict):
            tunnels.append(_parse_tunnel(t))
    return Host(
        alias=alias,
        host=str(host),
        user=str(raw["user"])
        if raw.get("user")
        else (str(defaults["user"]) if defaults.get("user") else None),
        port=port,
        auth=auth,
        cred_key=str(raw["cred_key"]) if raw.get("cred_key") else None,
        local=_as_bool(raw.get("local")) if raw.get("local") is not None else None,
        group=str(raw["group"])
        if raw.get("group")
        else (str(defaults["group"]) if defaults.get("group") else None),
        auto_cycle=_as_bool(raw.get("auto_cycle"))
        if raw.get("auto_cycle") is not None
        else (
            _as_bool(defaults.get("auto_cycle")) if defaults.get("auto_cycle") is not None else None
        ),
        cycle_interval=int(raw.get("cycle_interval") or defaults.get("cycle_interval") or 120),
        connect_timeout=int(raw.get("connect_timeout") or defaults.get("connect_timeout") or 0)
        or None,
        strict_host_key_checking=str(
            raw.get("strict_host_key_checking")
            or defaults.get("strict_host_key_checking")
            or "accept-new"
        ),
        jump_host=str(raw["jump_host"])
        if raw.get("jump_host")
        else (str(defaults["jump_host"]) if defaults.get("jump_host") else None),
        tunnels=tunnels,
        extra=raw,
    )


def load_config(path: Path | None = None) -> Config:
    """Carica e valida la configurazione. Crea directory/seed se non esiste."""
    path = Path(path) if path else default_config_path()
    cfg: Config
    if not path.exists():
        cfg = Config(path=path)
        seed_config(path.parent)
        return cfg
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Impossibile leggere {path}: {exc}") from exc

    defaults: dict[str, Any] = dict(data.get("defaults") or {})
    cred = data.get("credentials") or {}
    general = data.get("general") or {}
    provider = str(cred.get("provider") or "keyring")
    hosts: list[Host] = []
    for raw in data.get("hosts") or []:
        if not isinstance(raw, dict):
            continue
        alias = str(raw.get("alias") or "").strip()
        if not alias:
            for k, v in raw.items():
                if isinstance(v, dict):
                    hosts.append(_parse_host(k, v, defaults))
            continue
        hosts.append(_parse_host(alias, raw, defaults))

    cfg = Config(
        credential_provider=provider,
        plain_file=str(cred["plain_file"]) if cred.get("plain_file") else None,
        hosts=hosts,
        path=path,
        restart_after_ssh=_as_bool(general.get("restart_after_ssh")),
        history_size=int(general.get("history_size") or 10),
        audit_log=_as_bool(general.get("audit_log"), default=True),
        strict_host_key_checking=str(general.get("strict_host_key_checking") or "accept-new"),
        snippets_file=str(general["snippets_file"]) if general.get("snippets_file") else None,
        tunnels_file=str(general["tunnels_file"]) if general.get("tunnels_file") else None,
    )
    return cfg


def seed_config(dirpath: Path, hosts: list[Host] | None = None) -> Path:
    """Crea una configurazione di esempio se non esiste."""
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / "config.toml"
    if path.exists():
        return path
    lines = [
        "# Configurazione bravoric-ssh-client",
        '# Provider credenziali: "keyring" | "plain"',
        "[credentials]",
        'provider = "keyring"',
        "",
        "# Valori di default per gli host sotto",
        "[defaults]",
        "port = 22",
        'auth = "keyring"',
        "",
        "[[hosts]]",
    ]
    if hosts:
        for h in hosts:
            lines.append(f"# {h.alias} — {h.host}")
    else:
        lines += [
            'alias = "esempio"',
            'host = "192.168.1.10"',
            'user = "mio-utente"',
            '# auth = "keyring"   # o "key" per chiave SSH, o "prompt" ("" = provider globale)',
        ]
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def serialize_config(cfg: Config) -> str:
    """Serializza una Config in TOML (per la scrittura su disco)."""
    lines: list[str] = []
    lines.append("# Configurazione bravoric-ssh-client")
    lines.append('# Provider credenziali: "keyring" | "plain"')
    lines.append("[credentials]")
    lines.append(f"provider = {_toml_str(cfg.credential_provider or 'keyring')}")
    if cfg.plain_file:
        lines.append(f"plain_file = {_toml_str(cfg.plain_file)}")
    lines.append("")
    lines.append("[defaults]")
    lines.append("port = 22")
    lines.append('auth = ""')
    lines.append("")
    lines.append("[general]")
    lines.append(f"restart_after_ssh = {_toml_value(cfg.restart_after_ssh)}")
    lines.append(f"history_size = {int(cfg.history_size)}")
    lines.append(f"audit_log = {_toml_value(cfg.audit_log)}")
    if cfg.strict_host_key_checking and cfg.strict_host_key_checking != "accept-new":
        lines.append(f"strict_host_key_checking = {_toml_str(cfg.strict_host_key_checking)}")
    if cfg.snippets_file:
        lines.append(f"snippets_file = {_toml_str(cfg.snippets_file)}")
    if cfg.tunnels_file:
        lines.append(f"tunnels_file = {_toml_str(cfg.tunnels_file)}")
    lines.append("")
    for h in cfg.hosts:
        lines.append(f"# {h.alias}")
        lines.append("[[hosts]]")
        lines.append(f"alias = {_toml_str(h.alias)}")
        lines.append(f"host = {_toml_str(h.host)}")
        if h.user:
            lines.append(f"user = {_toml_str(h.user)}")
        if h.port and h.port != 22:
            lines.append(f"port = {int(h.port)}")
        if h.auth:
            lines.append(f"auth = {_toml_str(h.auth)}")
        if h.cred_key:
            lines.append(f"cred_key = {_toml_str(h.cred_key)}")
        if h.local is not None:
            lines.append(f"local = {_toml_value(h.local)}")
        if h.group:
            lines.append(f"group = {_toml_str(h.group)}")
        if h.auto_cycle is not None:
            lines.append(f"auto_cycle = {_toml_value(h.auto_cycle)}")
        if h.cycle_interval and h.cycle_interval != 120:
            lines.append(f"cycle_interval = {int(h.cycle_interval)}")
        if h.jump_host:
            lines.append(f"jump_host = {_toml_str(h.jump_host)}")
        if h.connect_timeout is not None:
            lines.append(f"connect_timeout = {int(h.connect_timeout)}")
        if h.strict_host_key_checking and h.strict_host_key_checking != "accept-new":
            lines.append(f"strict_host_key_checking = {_toml_str(h.strict_host_key_checking)}")
        for t in h.tunnels:
            lines.append("[[hosts.tunnels]]")
            if t.name:
                lines.append(f"name = {_toml_str(t.name)}")
            lines.append(f"kind = {_toml_str(t.kind)}")
            lines.append(f"local_port = {int(t.local_port)}")
            if t.kind in ("L", "R"):
                lines.append(f"remote_host = {_toml_str(t.remote_host)}")
                lines.append(f"remote_port = {int(t.remote_port)}")
            if t.bind and t.bind != "localhost":
                lines.append(f"bind = {_toml_str(t.bind)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def save_config(cfg: Config, path: Path | None = None) -> Path:
    """Scrive la Config sul file TOML (di default nel suo stesso path)."""
    path = Path(path) if path else cfg.path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_config(cfg), encoding="utf-8")
    cfg.path = path
    return path


def backup_config(cfg: Config) -> Path | None:
    """Copia la config attuale in un backup con timestamp (prima di una modifica)."""
    path = cfg.path or default_config_path()
    if not path.exists():
        return None
    import time

    backup = path.with_name(f"{path.name}.bak.{int(time.time())}")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup
