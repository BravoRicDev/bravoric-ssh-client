"""Lancio di un file manager a doppio pannello (Midnight Commander) per SFTP.

Apre ``mc`` già configurato su due pannelli: un host remoto e uno locale, oppure
due host remoti (scambio file server↔server). Usa il VFS ``sh://`` di mc
(ssh/scp, equivalente SFTP; ``sftp://`` è accettato da mc solo via ``cd`` interno)
e riusa il meccanismo ``SSH_ASKPASS`` per fornire le password salvate nel
keyring in modo automatico.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from ..config import Host
from .shellutil import write_askpass_helper
from .tmux_runner import _schedule_cleanup_posix


def mc_available() -> bool:
    """True se Midnight Commander (mc) è installato."""
    return shutil.which("mc") is not None


def mc_path() -> str | None:
    return shutil.which("mc")


def build_url(host: Host, *, path: str = "/") -> str:
    """URL VFS di mc per l'host.

    - host locali: percorso del filesystem locale.
    - remoti: ``sh://user@host:port/path`` (VFS shell di mc, basato su ssh/scp).
      mc accetta ``sh://`` come argomento dei pannelli; ``sftp://`` è usabile solo
      con ``cd`` all'interno di mc.
    """
    if host.is_local():
        return str(Path(path).expanduser()) if path not in ("", "/") else str(Path.home())
    user = host.effective_user()
    base = f"{user}@{host.host}" if user else host.host
    if host.port and host.port != 22:
        base += f":{host.port}"
    p = path or "/"
    if not p.startswith("/"):
        p = "/" + p
    return f"sh://{base}{p}"


def build_env(
    config,
    host_a: Host,
    host_b: Host,
    *,
    password_resolver: Callable[[Host], str | None],
) -> tuple[dict[str, str], Path | None]:
    """Prepara l'ambiente per mc con le password del keyring.

    Ritorna (env, helper_path). L'helper va rimosso dopo l'exit di mc.

    Usa l'helper ``SSH_ASKPASS`` con ``SSH_ASKPASS_REQUIRE=force`` SOLO se ogni
    host coinvolto che richiede una password (auth keyring/plain) ha una password
    risolta dal resolver: in tal caso la connessione è completamente automatica.
    Se invece c'è un host a password senza credenziale salvata (o auth=prompt),
    non forza l'helper e OpenSSH chiederà in modo interattivo (agent per i key).
    """
    env = dict(os.environ)
    mapping: dict[str, str] = {}
    needs_password = False
    all_resolved = True
    for host in (host_a, host_b):
        if host.is_local():
            continue
        method = (host.auth or "").strip().lower()
        if method in ("key", "prompt"):
            continue
        needs_password = True
        password = password_resolver(host)
        if password:
            mapping.setdefault(host.host, password)
        else:
            all_resolved = False
    if not needs_password or not all_resolved or not mapping:
        return env, None
    helper = write_askpass_helper(mapping)
    env["SSH_ASKPASS"] = str(helper)
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.setdefault("DISPLAY", ":0")
    return env, helper


def launch_commander(
    host_a: Host,
    host_b: Host,
    env: dict[str, str],
    *,
    path_a: str = "/",
    path_b: str = "/",
) -> None:
    """Esegue mc (exec) con i due pannelli già impostati."""
    mc = mc_path()
    if not mc:
        raise FileNotFoundError("Midnight Commander (mc) non installato")
    url_a = build_url(host_a, path=path_a)
    url_b = build_url(host_b, path=path_b)
    # exec: sostituisce il processo (come per ssh). Su Windows subprocess + exit.
    if os.name == "posix":
        # Su POSIX, l'exec non torna mai. Usiamo un guardian fork per pulire l'helper askpass.
        helper = None
        if "SSH_ASKPASS" in env:
            helper = Path(env["SSH_ASKPASS"])
        _schedule_cleanup_posix(helper)
        os.execvpe(mc, [mc, url_a, url_b], env)
    else:
        import subprocess

        subprocess.run([mc, url_a, url_b], env=env, check=False)
        raise SystemExit(0)
