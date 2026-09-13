"""Factory dei provider credenziali e risolutore per-host.

Regole:
- ``auth = ""`` (default): usa il provider globale indicato in ``[credentials].provider``.
- ``auth = "keyring"`` / ``"plain"``: forza quel backend specifico per l'host.
- ``auth = "key"``: nessuna password (chiave SSH / agent).
- ``auth = "prompt"``: nessuna password salvata (OpenSSH chiede in modo interattivo).
"""

from __future__ import annotations

from ..config import Config, Host
from . import CredentialError, CredentialProvider
from .keyring import KeyringProvider
from .plain import PlainProvider


def build_provider(config: Config) -> CredentialProvider:
    name = (config.credential_provider or "keyring").strip().lower()
    if name == "keyring":
        return KeyringProvider()
    if name == "plain":
        return PlainProvider(config=config)
    raise CredentialError(f"Provider credenziali sconosciuto: {name!r}")


def _provider_for(config: Config, host: Host) -> CredentialProvider | None:
    method = (host.auth or "").strip().lower()
    if method == "keyring":
        return KeyringProvider()
    if method == "plain":
        return PlainProvider(config=config)
    if method == "":  # provider globale
        return build_provider(config)
    return None  # key / prompt / sconosciuto


def resolve_password(config: Config, host: Host) -> str | None:
    method = (host.auth or "").strip().lower()
    if method in ("key", "prompt"):
        return None
    provider = _provider_for(config, host)
    if provider is None:
        return None
    try:
        return provider.get(host)
    except CredentialError:
        return None


def password_present(config: Config, host: Host) -> bool:
    """True se esiste una password salvata per l'host nel suo provider."""
    method = (host.auth or "").strip().lower()
    if method in ("key", "prompt"):
        return True  # non usa password: considerato "risolto"
    provider = _provider_for(config, host)
    if provider is None:
        return False
    try:
        return provider.get(host) is not None
    except CredentialError:
        return False


def store_password(config: Config, host: Host, secret: str) -> None:
    """Salva (o aggiorna) la password per l'host nel suo provider."""
    method = (host.auth or "").strip().lower()
    if method in ("key", "prompt"):
        raise CredentialError(f"L'host usa auth={method}: nessuna password da salvare")
    provider = _provider_for(config, host)
    if provider is None:
        raise CredentialError("Nessun provider credenziali per questo host")
    provider.set(host, secret)


def clear_password(config: Config, host: Host) -> None:
    """Elimina la password salvata per l'host."""
    method = (host.auth or "").strip().lower()
    if method in ("key", "prompt"):
        return
    provider = _provider_for(config, host)
    if provider is not None:
        provider.delete(host)
