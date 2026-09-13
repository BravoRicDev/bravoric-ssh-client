"""Utility condivise per la costruzione di comandi shell e helper SSH_ASKPASS.

Centralizza due cose che altrimenti erano duplicate in ogni modulo:

- ``sh_quote``: quoting POSIX di un argomento.
- ``write_askpass_helper`` / ``write_askpass_helper_single`` e ``askpass_env``:
  generazione *sicura* (``tempfile.mkstemp``, permessi 0600/0700) dei piccoli
  script usati come ``SSH_ASKPASS`` per fornire le password a OpenSSH senza
  dipendere da ``sshpass``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Helper a password singola: la password arriva da $BRAVORIC_PASSWORD.
_ASKPASS_SINGLE = "#!/bin/sh\nprintf '%s' \"$BRAVORIC_PASSWORD\"\n"

# Helper multi-host: il primo argomento è il prompt di ssh; ogni riga del case
# restituisce la password dell'host che compare nel prompt.
_ASKPASS_MULTI = """#!/bin/sh
# SSH_ASKPASS multi-host per bravoric-ssh-client.
# Il primo argomento è il prompt di ssh (es. "user@host's password:").
prompt="$1"
case "$prompt" in
{case_body}
esac
# Nessuna password mappata: ssh chiederà in modo interattivo.
exit 1
"""


def sh_quote(value: str) -> str:
    """Quota un argomento per una shell POSIX (gestisce gli apici singoli)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _write_temp_script(script: str, *, prefix: str) -> Path:
    """Scrive uno script eseguibile in un file temporaneo creato in modo sicuro."""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)
        if os.name != "nt":
            os.chmod(path, 0o700)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return Path(path)


def write_askpass_helper_single() -> Path:
    """Helper che stampa la password presa da ``$BRAVORIC_PASSWORD``."""
    return _write_temp_script(_ASKPASS_SINGLE, prefix="bravoric-askpass-")


def write_askpass_helper(mapping: dict[str, str]) -> Path:
    """Helper ``SSH_ASKPASS`` che sceglie la password in base al prompt.

    ``mapping``: chiave da cercare nel prompt (es. ``host`` o ``user@host``) ->
    password. Se nessuna chiave corrisponde, esce con codice 1 così ssh ripiega
    sulla richiesta interattiva.
    """
    lines = [
        f"    *{sh_quote(key)}*) printf '%s' {sh_quote(password)}; exit 0;;"
        for key, password in mapping.items()
    ]
    script = _ASKPASS_MULTI.format(case_body="\n".join(lines))
    return _write_temp_script(script, prefix="bravoric-askpass-")


def askpass_env(password: str) -> dict[str, str]:
    """Ambiente con ``SSH_ASKPASS`` impostato per la password singola indicata.

    Il chiamante è responsabile di rimuovere il file ``SSH_ASKPASS`` quando ha
    finito (il percorso è in ``env["SSH_ASKPASS"]``).
    """
    env = dict(os.environ)
    helper = write_askpass_helper_single()
    env["SSH_ASKPASS"] = str(helper)
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.setdefault("DISPLAY", ":0")
    env["BRAVORIC_PASSWORD"] = password
    return env
