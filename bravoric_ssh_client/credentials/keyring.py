"""Implementazione keyring di sistema tramite la libreria ``keyring``.

La libreria ``keyring`` delega automaticamente al backend nativo della piattaforma:
SecretService (GNOME), KWallet (KDE), Keychain (macOS) e Credential Manager (Windows).
Su KDE si può forzare il backend kwallet selezionandolo tra quelli disponibili.

Attenzione: un backend di sistema può restare appeso in attesa di uno sblocco
interattivo (tipico del SecretService bloccato in contesti headless). Per non
congelare l'interfaccia ogni chiamata viene eseguita in un thread e interrotta
dopo ``TIMEOUT_SECONDS``; da quel momento le successive letture falliscono subito.
"""

from __future__ import annotations

import threading
import queue
import functools

import keyring
import keyring.errors

from ..config import Host
from . import CredentialError, CredentialProvider

# Oltre questa soglia il backend è considerato non responsivo.
TIMEOUT_SECONDS = 6.0

_unresponsive = threading.Event()
_unresponsive_lock = threading.Lock()

# Single persistent worker thread with queue to avoid thread-per-call leak.
_worker_queue: "queue.Queue[tuple[functools.partial, queue.Queue]]" = queue.Queue()
_worker_started = False
_worker_thread: threading.Thread | None = None


def _worker_loop() -> None:
    while True:
        task, result_queue = _worker_queue.get()
        try:
            result_queue.put(("ok", task()))
        except Exception as exc:
            result_queue.put(("error", exc))
        _worker_queue.task_done()


def _ensure_worker() -> None:
    global _worker_thread, _worker_started
    if not _worker_started:
        _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="keyring-worker")
        _worker_thread.start()
        _worker_started = True


def keyring_responsive() -> bool:
    """False se una chiamata precedente è scaduta (backend bloccato)."""
    return not _unresponsive.is_set()


class _Timeout(Exception):
    """Sollevata quando il backend supera ``TIMEOUT_SECONDS``."""


def _run_bounded(fn):
    """Esegue ``fn()`` nel worker persistente e fallisce se non risponde entro il timeout."""
    _ensure_worker()
    result_queue: queue.Queue = queue.Queue()
    _worker_queue.put((functools.partial(fn), result_queue))
    try:
        status, value = result_queue.get(timeout=TIMEOUT_SECONDS)
    except queue.Empty:
        with _unresponsive_lock:
            _unresponsive.set()
        raise _Timeout
    if status == "error":
        raise value
    return value


class KeyringProvider(CredentialProvider):
    def get(self, host: Host) -> str | None:
        key = host.effective_cred_key()
        if not keyring_responsive():
            raise CredentialError("Backend keyring non responsivo (sblocco non disponibile)")
        try:
            value = _run_bounded(lambda: keyring.get_password(self.service, key))
        except _Timeout as exc:
            raise CredentialError("Backend keyring non responsivo (timeout)") from exc
        except keyring.errors.KeyringError as exc:
            raise CredentialError(f"Keyring non disponibile: {exc}") from exc
        return value if value else None

    def set(self, host: Host, secret: str) -> None:
        key = host.effective_cred_key()
        try:
            _run_bounded(lambda: keyring.set_password(self.service, key, secret))
        except _Timeout as exc:
            raise CredentialError("Backend keyring non responsivo (timeout)") from exc
        except keyring.errors.KeyringError as exc:
            raise CredentialError(f"Keyring non disponibile: {exc}") from exc

    def delete(self, host: Host) -> None:
        key = host.effective_cred_key()

        def _delete() -> None:
            try:
                keyring.delete_password(self.service, key)
            except keyring.errors.PasswordDeleteError:
                pass

        try:
            _run_bounded(_delete)
        except _Timeout as exc:
            raise CredentialError("Backend keyring non responsivo (timeout)") from exc
        except keyring.errors.KeyringError as exc:
            raise CredentialError(f"Keyring non disponibile: {exc}") from exc
