"""Provider ``plain``: file di testo locale con permessi 0600.

Formato righe: ``<cred_key>\t<password>``.
Non è cifrato: adatto come fallback dove non c'è un keyring di sistema,
o per chi monta una home/partizione cifrata. Le password restano lì finché
l'utente non le sostituisce con un provider keyring.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..config import Config, Host, default_config_dir
from . import CredentialError, CredentialProvider


class PlainProvider(CredentialProvider):
    def __init__(self, filepath: Path | None = None, *, config: Config | None = None):
        if filepath is None:
            base = config.path.parent if config and config.path else default_config_dir()
            name = config.plain_file if config and config.plain_file else "secrets.tsv"
            filepath = Path(name)
            if not filepath.is_absolute():
                filepath = base / filepath
        self.filepath = filepath
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_mode()

    def _ensure_mode(self) -> None:
        if self.filepath.exists() and os.name != "nt":
            try:
                os.chmod(self.filepath, 0o600)
            except OSError:
                pass

    def _read(self) -> dict[str, str]:
        if not self.filepath.exists():
            return {}
        data: dict[str, str] = {}
        try:
            text = self.filepath.read_text(encoding="utf-8")
        except OSError as exc:
            raise CredentialError(f"Impossibile leggere {self.filepath}: {exc}") from exc
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                k, _, v = line.partition("\t")
            elif "=" in line:
                k, _, v = line.partition("=")
            else:
                continue
            data[k.strip()] = v.strip()
        return data

    def _write(self, data: dict[str, str]) -> None:
        lines = ["# bravoric-ssh-client secrets — non condividere questo file", ""]
        for k in sorted(data):
            lines.append(f"{k}\t{data[k]}")
        payload = "\n".join(lines) + "\n"
        # scrittura atomica con permessi 0600
        fd, tmp = tempfile.mkstemp(dir=str(self.filepath.parent), prefix=".secrets.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.filepath)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, host: Host) -> str | None:
        return self._read().get(host.effective_cred_key())

    def set(self, host: Host, secret: str) -> None:
        data = self._read()
        data[host.effective_cred_key()] = secret
        self._write(data)

    def delete(self, host: Host) -> None:
        data = self._read()
        data.pop(host.effective_cred_key(), None)
        self._write(data)
