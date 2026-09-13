"""Esecuzione asincrona distribuita di snippet su più host.

Esegue un comando shell su N host in parallelo (thread pool) e raccoglie
stdout/stderr/exit code per host, così la TUI può mostrarli in una griglia
di risultati. Per host locali esegue il comando direttamente sulla macchina.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..config import Host
from . import adapter


@dataclass
class BroadcastResult:
    """Esito dell'esecuzione di uno snippet su un singolo host."""

    host_alias: str
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    session_name: str = ""  # se presente: eseguito in una sessione tmux con questo nome

    def summary(self) -> str:
        if self.error:
            return f"{self.host_alias}: ERRORE {self.error}"
        if self.session_name:
            return f"{self.host_alias}: sessione '{self.session_name}'"
        status = "OK" if self.ok else f"EXIT {self.exit_code}"
        return f"{self.host_alias}: {status}"


def session_slug(snippet: str, host_alias: str) -> str:
    """Slug del nome sessione tmux per un broadcast.

    Forma: ``bcast-<snippet>-<host>-<YYYYMMDD-HHMMSS>``. Il nome resta pulito
    (solo [A-Za-z0-9._-], max ~60 char) così è filtrabile con ``tmux ls``.
    """
    import datetime
    import re

    def clean(value: str) -> str:
        out = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
        out = re.sub(r"-{2,}", "-", out).strip(".-_") or "snippet"
        return out[:24]

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"bcast-{clean(snippet)}-{clean(host_alias)}-{ts}"[:60]


def run_snippet_in_tmux(
    host: Host,
    command: str,
    session_name: str,
    cfg: adapter.SshConfig | None = None,
    *,
    timeout: int = 60,
) -> BroadcastResult:
    """Lancia ``command`` in una NUOVA sessione tmux detached sull'host.

    La sessione resta viva sul server: per vederne l'output basta attacharsi.
    Se tmux non è installato sull'host, ripiega sull'esecuzione diretta
    (ssh batch), segnalando la modalità nel risultato via ``session_name`` vuoto.
    """
    cfg = cfg or adapter.SshConfig()
    if not adapter.tmux_present(host, cfg):
        res = run_snippet_on_host(host, command, cfg, timeout=timeout)
        res.session_name = ""
        return res
    q = adapter._sh_quote
    tmux_cmd = f"tmux new -d -s {q(session_name)} {q(command)}"
    result = adapter.run_tmux_action(host, tmux_cmd, cfg, timeout=timeout)
    if result.ok:
        return BroadcastResult(
            host_alias=host.alias,
            ok=True,
            exit_code=0,
            session_name=session_name,
            stdout=f"lanciata in sessione tmux '{session_name}'",
        )
    # errore: tentiamo il fallback diretto, mantenendo il messaggio di errore
    res = run_snippet_on_host(host, command, cfg, timeout=timeout)
    if res.ok:
        res.session_name = ""
        return res
    res.ok = False
    res.error = result.stderr.strip() or res.error or f"exit {result.ok}"
    return res


def run_snippet_on_hosts_tmux(
    hosts: list[Host],
    command: str,
    snippet_name: str,
    cfg: adapter.SshConfig | None = None,
    *,
    timeout: int = 60,
    max_workers: int = 8,
) -> list[BroadcastResult]:
    """Lancia lo snippet in una sessione tmux nuova per ogni host, in parallelo."""
    results: list[BroadcastResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                run_snippet_in_tmux,
                h,
                command,
                session_slug(snippet_name, h.alias),
                cfg,
                timeout=timeout,
            ): h
            for h in hosts
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - mai far fallire il broadcast
                h = futures[fut]
                results.append(
                    BroadcastResult(host_alias=h.alias, ok=False, exit_code=-1, error=str(exc))
                )
    results.sort(key=lambda r: r.host_alias)
    return results


def run_snippet_on_host(
    host: Host,
    command: str,
    cfg: adapter.SshConfig | None = None,
    *,
    timeout: int = 60,
) -> BroadcastResult:
    """Esegue ``command`` su un singolo host (locale o remoto)."""
    cfg = cfg or adapter.SshConfig()
    if host.is_local():
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return BroadcastResult(
                host_alias=host.alias,
                ok=proc.returncode == 0,
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        except subprocess.TimeoutExpired:
            return BroadcastResult(host_alias=host.alias, ok=False, exit_code=-1, error="timeout")
        except OSError as exc:
            return BroadcastResult(host_alias=host.alias, ok=False, exit_code=-1, error=str(exc))

    # remoto: prima tentativo pulito (chiave/agent), poi con password se serve.
    proc = adapter.run_remote_command(host, command, cfg, batch=True, timeout=timeout)
    if proc.returncode != 0 and cfg.password_provider and cfg.password_provider(host):
        proc = adapter.run_remote_command(host, command, cfg, with_password=True, timeout=timeout)
    return BroadcastResult(
        host_alias=host.alias,
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def run_snippet_on_hosts(
    hosts: list[Host],
    command: str,
    cfg: adapter.SshConfig | None = None,
    *,
    timeout: int = 60,
    max_workers: int = 8,
) -> list[BroadcastResult]:
    """Esegue lo snippet su tutti gli host in parallelo; ritorna i risultati."""
    results: list[BroadcastResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(run_snippet_on_host, h, command, cfg, timeout=timeout): h for h in hosts
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - mai far fallire il broadcast
                h = futures[fut]
                results.append(
                    BroadcastResult(host_alias=h.alias, ok=False, exit_code=-1, error=str(exc))
                )
    # ordina per alias per una griglia stabile
    results.sort(key=lambda r: r.host_alias)
    return results
