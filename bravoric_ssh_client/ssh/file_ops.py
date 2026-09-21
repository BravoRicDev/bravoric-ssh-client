"""Operazioni file via SCP e SFTP (delega a OpenSSH scp/sftp), con supporto password.

Riusa lo stesso meccanismo SSH_ASKPASS usato per il listino sessioni, così funziona
anche con host a password. Le operazioni sono non interattive (nessun exec).
"""

from __future__ import annotations

import os
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


@dataclass
class SftpResult:
    ok: bool = False
    stdout: str = ""
    stderr: str = ""


def _target(host: Host, remote_path: str) -> str:
    user = host.effective_user()
    base = f"{user}@{host.host}" if user else host.host
    return f"{base}:{remote_path}"


def _sftp_target(host: Host) -> str:
    user = host.effective_user()
    return f"{user}@{host.host}" if user else host.host


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


def _run_sftp_batch(
    host: Host,
    batch_commands: list[str],
    password: str | None,
    *,
    timeout: int = 120,
) -> SftpResult:
    sftp = shutil.which("sftp") or "sftp"
    cmd = [sftp, "-b", "-", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5"]
    if host.port and host.port != 22:
        cmd += ["-P", str(host.port)]
    cmd += [_sftp_target(host)]

    batch_input = "\n".join(batch_commands) + "\n"

    if password:
        env = askpass_env(password)
        try:
            proc = subprocess.run(
                cmd,
                input=batch_input,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        finally:
            try:
                Path(env["SSH_ASKPASS"]).unlink(missing_ok=True)
            except OSError:
                pass
    else:
        proc = subprocess.run(
            cmd,
            input=batch_input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return SftpResult(
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


# ── Nuovi Tool SFTP Nativi ───────────────────────────────────────────────────


def sftp_list(
    host: Host, remote_path: str, password: str | None, *, timeout: int = 30
) -> SftpResult:
    """Elenca il contenuto di una directory remota."""
    return _run_sftp_batch(host, [f"ls -la {remote_path}"], password, timeout=timeout)


def sftp_mkdir(
    host: Host, remote_path: str, password: str | None, *, timeout: int = 30
) -> SftpResult:
    """Crea una directory remota (ricorsivamente via sftp mkdir)."""
    # sftp non supporta mkdir -p nativo, ma possiamo creare le directory intermedie se necessario
    # o semplicemente fare mkdir. Per sftp facciamo mkdir.
    return _run_sftp_batch(host, [f"mkdir {remote_path}"], password, timeout=timeout)


def sftp_rm(host: Host, remote_path: str, password: str | None, *, timeout: int = 30) -> SftpResult:
    """Rimuove un file o directory remota."""
    # sftp rm rimuove file, rmdir rimuove dir. Proviamo rm prima, rmdir come fallback.
    return _run_sftp_batch(
        host, [f"rm {remote_path}", f"rmdir {remote_path}"], password, timeout=timeout
    )


def sftp_rename(
    host: Host, old_path: str, new_path: str, password: str | None, *, timeout: int = 30
) -> SftpResult:
    """Rinomina o sposta un file/directory remota."""
    return _run_sftp_batch(host, [f"rename {old_path} {new_path}"], password, timeout=timeout)


def sftp_get(
    host: Host,
    remote_path: str,
    local_path: str,
    password: str | None,
    *,
    recursive: bool = False,
    timeout: int = 120,
) -> SftpResult:
    """Scarica file o directory via sftp (supporta ricorsione)."""
    flag = "-r " if recursive else ""
    return _run_sftp_batch(
        host, [f"get {flag}{remote_path} {local_path}"], password, timeout=timeout
    )


def sftp_put(
    host: Host,
    local_path: str,
    remote_path: str,
    password: str | None,
    *,
    recursive: bool = False,
    timeout: int = 120,
) -> SftpResult:
    """Carica file o directory via sftp (supporta ricorsione)."""
    flag = "-r " if recursive else ""
    return _run_sftp_batch(
        host, [f"put {flag}{local_path} {remote_path}"], password, timeout=timeout
    )


def sftp_batch(
    host: Host, batch_commands: list[str], password: str | None, *, timeout: int = 120
) -> SftpResult:
    """Esegue comandi SFTP arbitrari in batch."""
    return _run_sftp_batch(host, batch_commands, password, timeout=timeout)


def transfer_file_direct(
    src_host: Host,
    src_path: str,
    dst_host: Host,
    dst_path: str,
    src_password: str | None,
    dst_password: str | None,
    *,
    timeout: int = 300,
) -> SftpResult:
    """Trasferisce un file direttamente tra due host remoti usando SFTP streaming (senza staging locale su disco)."""
    # Usiamo una pipe: sftp get da sorgente a stdout | sftp put verso destinazione da stdin
    sftp = shutil.which("sftp") or "sftp"

    # Comando di lettura (sorgente)
    src_cmd = [sftp, "-b", "-", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5"]
    if src_host.port and src_host.port != 22:
        src_cmd += ["-P", str(src_host.port)]
    src_cmd += [_sftp_target(src_host)]

    # Comando di scrittura (destinazione)
    dst_cmd = [sftp, "-b", "-", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5"]
    if dst_host.port and dst_host.port != 22:
        dst_cmd += ["-P", str(dst_host.port)]
    dst_cmd += [_sftp_target(dst_host)]

    # Prepariamo gli ambienti
    src_env = askpass_env(src_password) if src_password else dict(os.environ)
    dst_env = askpass_env(dst_password) if dst_password else dict(os.environ)

    try:
        # Avviamo il processo sorgente (get a stdout)
        p_src = subprocess.Popen(
            src_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=src_env,
        )

        # Avviamo il processo destinazione (put da stdin)
        p_dst = subprocess.Popen(
            dst_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dst_env,
        )

        # Inviamo i comandi SFTP ai rispettivi processi
        # Sorgente: get file a stdout (usando '-' come target locale per sftp get)
        p_src.stdin.write(f"get {src_path} -\n")
        p_src.stdin.close()

        # Destinazione: put da stdin a destinazione (usando '-' come sorgente locale)
        p_dst.stdin.write(f"put - {dst_path}\n")
        p_dst.stdin.close()

        # Leggiamo da sorgente e scriviamo a destinazione in streaming
        # Nota: per sftp get '-' scrive l'output binario sul suo stdout.
        # Ma dato che abbiamo usato text=True, gestiamo il testo o convertiamo in binario.
        # Per sicurezza riavviamo senza text=True per lo streaming binario efficiente.
        p_src.kill()
        p_dst.kill()

        p_src = subprocess.Popen(
            src_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=src_env,
        )
        p_dst = subprocess.Popen(
            dst_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dst_env,
        )

        p_src.stdin.write(f"get {src_path} -\n".encode())
        p_src.stdin.close()

        p_dst.stdin.write(f"put - {dst_path}\n".encode())

        # Stream loop
        while True:
            chunk = p_src.stdout.read(1024 * 1024)  # 1 MB chunk
            if not chunk:
                break
            p_dst.stdin.write(chunk)

        p_dst.stdin.close()

        # Attesa completamento
        p_src.wait(timeout=timeout)
        p_dst.wait(timeout=timeout)

        src_err = p_src.stderr.read().decode("utf-8", errors="replace")
        dst_err = p_dst.stderr.read().decode("utf-8", errors="replace")

        ok = p_src.returncode == 0 and p_dst.returncode == 0
        return SftpResult(
            ok=ok,
            stdout=f"Source exit: {p_src.returncode}, Dest exit: {p_dst.returncode}",
            stderr=f"Source err: {src_err}\nDest err: {dst_err}",
        )

    finally:
        if src_password:
            Path(src_env.get("SSH_ASKPASS", "")).unlink(missing_ok=True)
        if dst_password:
            Path(dst_env.get("SSH_ASKPASS", "")).unlink(missing_ok=True)
