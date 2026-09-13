"""Operazioni file via SCP (delega a OpenSSH scp), con supporto password.

Riusa lo stesso meccanismo SSH_ASKPASS usato per il listino sessioni, così funziona
anche con host a password. Le operazioni sono non interattive (nessun exec).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Host
from .shellutil import askpass_env


@dataclass
class ScpResult:
    ok: bool = False
    stdout: str = ""
    stderr: str = ""


def _target(host: Host, remote_path: str) -> str:
    user = host.effective_user()
    base = f"{user}@{host.host}" if user else host.host
    return f"{base}:{remote_path}"


def _run_scp(
    host: Host,
    args: list[str],
    password: str | None,
    *,
    timeout: int = 120,
) -> ScpResult:
    scp = shutil.which("scp") or "scp"
    cmd = [scp, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5"]
    if host.port and host.port != 22:
        cmd += ["-P", str(host.port)]
    cmd += args
    if password:
        env = askpass_env(password)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        finally:
            try:
                Path(env["SSH_ASKPASS"]).unlink(missing_ok=True)
            except OSError:
                pass
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return ScpResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def upload(
    host: Host, local: str, remote: str, password: str | None, *, timeout: int = 120
) -> ScpResult:
    """Copia un file locale verso il server (scp local user@host:remote)."""
    return _run_scp(host, [local, _target(host, remote)], password, timeout=timeout)


def download(
    host: Host, remote: str, local: str, password: str | None, *, timeout: int = 120
) -> ScpResult:
    """Copia un file dal server in locale (scp user@host:remote local)."""
    return _run_scp(host, [_target(host, remote), local], password, timeout=timeout)
