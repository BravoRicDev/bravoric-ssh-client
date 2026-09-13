"""Layer credenziali astratto.

L'app non deve dipendere dal keyring di GNOME: fornisce un'interfaccia unica
``CredentialProvider`` con implementazioni per keyring di sistema (GNOME, KWallet,
Keychain, Windows Credential Manager) e per un file locale ``plain`` con permessi 0600.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Host


class CredentialError(Exception):
    """Errore di accesso alle credenziali."""


class CredentialProvider(ABC):
    """Interfaccia comune per leggere/scrivere/eliminare una password di un host."""

    service: str = "bravoric-ssh-client"

    @abstractmethod
    def get(self, host: Host) -> str | None:
        """Password per l'host, oppure None se assente."""

    @abstractmethod
    def set(self, host: Host, secret: str) -> None:
        """Memorizza la password per l'host."""

    @abstractmethod
    def delete(self, host: Host) -> None:
        """Elimina la password per l'host."""
