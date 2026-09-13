"""Import di host da ``~/.ssh/config`` in Config/TOML (riusabile da TUI e script)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, Host


def remmina_dirs() -> list[Path]:
    """Directory dove Remmina salva i profili .remmina (flatpak e legacy)."""
    home = Path.home()
    dirs = [
        home / ".var" / "app" / "org.remmina.Remmina" / "data" / "remmina",
        home / ".local" / "share" / "remmina",
        home / ".config" / "remmina",
    ]
    return [d for d in dirs if d.is_dir()]


def find_remmina_files() -> list[Path]:
    """Tutti i file *.remmina disponibili (flatpak prima)."""
    out: list[Path] = []
    seen: set[str] = set()
    for d in remmina_dirs():
        for f in sorted(d.glob("*.remmina")):
            key = f.stem
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def parse_remmina_file(path: Path) -> SshEntry | None:
    """Estrae un host da un profilo Remmina SSH (protocol=SSH)."""
    data: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    if data.get("protocol", "").lower() != "ssh":
        return None
    name = data.get("name", "").strip()
    server = data.get("server", "").strip()
    if not server or server.startswith("."):  # server vuoto/placeholder
        return None
    user = data.get("username", "").strip()
    try:
        port = int(data.get("port", "") or "22")
    except ValueError:
        port = 22
    auth = ""
    # password="." -> nel keyring; ssh_privatekey -> chiave
    if data.get("ssh_privatekey"):
        auth = "key"
    elif data.get("password") == ".":
        auth = "keyring"
    elif data.get("password"):
        auth = "keyring"
    alias = name.lower().replace(" ", "-").replace("_", "-") or server
    return SshEntry(alias=alias, host=server, user=user, port=port, auth=auth, raw=data)


def import_from_remmina(
    out_path: Path,
    *,
    provider: str = "keyring",
    default_auth: str = "keyring",
    merge: bool = True,
) -> tuple[int, int]:
    """Importa gli host da profili Remmina (SSH) nel file config.toml.

    Ritorna (totale, nuovi_aggiunti).
    """
    entries: list[SshEntry] = []
    for f in find_remmina_files():
        e = parse_remmina_file(f)
        if e:
            entries.append(e)
    if not entries:
        return 0, 0
    return _write_entries(
        entries, out_path, provider=provider, default_auth=default_auth, merge=merge
    )


@dataclass
class SshEntry:
    alias: str
    host: str
    user: str = ""
    port: int = 22
    auth: str = ""
    raw: dict[str, str] = field(default_factory=dict)


def parse_ssh_config(path: Path) -> list[SshEntry]:
    """Parser minimale di un ssh_config: gestisce Host con blocchi annidati.

    Non copre la semantica completa (wildcard, match, Include, ProxyJump...)
    ma i casi comuni: alias, User, HostName, Port.
    """
    entries: list[SshEntry] = []
    current: dict[str, str] | None = None

    def close():
        nonlocal current
        if current is None:
            return
        alias = current.get("host", "").strip()
        hostname = current.get("hostname") or alias
        hostname = hostname.strip()
        if alias and "*" not in alias and "?" not in alias and alias not in ("localhost",):
            entries.append(
                SshEntry(
                    alias=alias,
                    host=hostname,
                    user=current.get("user", "").strip(),
                    port=int(current.get("port", "22") or "22"),
                    raw=dict(current),
                )
            )
        current = None

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        key = key.strip().lower()
        value = value.strip()
        if key == "host":
            close()
            current = {"host": value}
        elif current is not None and value:
            current[key.lower()] = value
    close()
    return entries


def entries_to_hosts(entries: list[SshEntry], default_auth: str = "keyring") -> list[Host]:
    return [
        Host(
            alias=e.alias,
            host=e.host,
            user=e.user or None,
            port=e.port or 22,
            auth=e.auth or default_auth,
        )
        for e in entries
    ]


def _write_entries(
    entries: list[SshEntry],
    out_path: Path,
    *,
    provider: str = "keyring",
    default_auth: str = "keyring",
    merge: bool = True,
) -> tuple[int, int]:
    """Scrive gli host nel file config.toml. Ritorna (totale, nuovi_aggiunti)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if merge and out_path.exists():
        from .config import load_config, save_config

        existing = load_config(out_path)
        existing_names = {h.alias for h in existing.hosts}
        new_entries = [e for e in entries if e.alias not in existing_names]
        total_new = len(new_entries)
        cfg = existing
        for e in new_entries:
            cfg.hosts.append(
                Host(
                    alias=e.alias,
                    host=e.host,
                    user=e.user or None,
                    port=e.port or 22,
                    auth=e.auth or default_auth,
                )
            )
        save_config(cfg, out_path)
        return len(entries), total_new

    from .config import save_config

    cfg = Config(
        credential_provider=provider,
        hosts=entries_to_hosts(entries, default_auth),
        path=out_path,
    )
    save_config(cfg, out_path)
    return len(entries), len(entries)


def import_from_ssh_config(
    ssh_config: Path,
    out_path: Path,
    *,
    provider: str = "keyring",
    default_auth: str = "keyring",
    merge: bool = True,
    exclude: set[str] | None = None,
) -> tuple[int, int]:
    """Importa host da un ssh_config nel file config.toml.

    Ritorna (totale, nuovi_aggiunti). Con ``merge=True`` gli host già presenti
    nel config.toml non vengono duplicati; altrimenti il file viene riscritto
    con soli host importati.
    """
    entries = parse_ssh_config(ssh_config)
    if exclude:
        entries = [e for e in entries if e.alias not in exclude]
    if not entries:
        return 0, 0
    return _write_entries(
        entries, out_path, provider=provider, default_auth=default_auth, merge=merge
    )
