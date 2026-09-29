"""Test sui tetti dell'audit: la cartella dei log non deve poter riempire il disco.

Sul campo si era arrivati a ~14 GB con un singolo file da 5 GB: il writer era uno
stream gzip mai chiuso e non esisteva alcuna retention. Questi test bloccano la
regressione su due fronti:
  - il writer tmux e' limitato a MAX_LOG_BYTES;
  - la cartella viene potata (per eta' e per budget) prima di ogni nuova scrittura.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from bravoric_ssh_client.ssh import audit

# --- cap del writer ---------------------------------------------------------


def test_pipe_pane_limita_la_dimensione(tmp_path: Path):
    cmd = audit.tmux_pipe_pane_enable("sess", tmp_path / "a.log.gz")
    assert "head -c" in cmd
    assert str(audit.MAX_LOG_BYTES) in cmd
    assert "gzip -c" in cmd
    # -o: non aprire un secondo pipe se ce n'e' gia' uno
    assert "pipe-pane -o" in cmd


def test_pipe_pane_quota_correttamente_percorsi_strani(tmp_path: Path):
    """Il percorso finisce dentro due livelli di quoting: spazi e apici non devono
    rompere il comando (prima veniva composto a mano e si rompeva)."""
    weird = tmp_path / "dir con spazio" / "o'brien.log.gz"
    weird.parent.mkdir(parents=True, exist_ok=True)
    cmd = audit.tmux_pipe_pane_enable("sess", weird)

    # il comando deve essere eseguibile da sh ed eseguire davvero gzip
    import shlex
    import subprocess

    inner = shlex.split(cmd)[-1]
    res = subprocess.run(
        ["/bin/sh", "-c", inner],
        input=b"A" * 500,
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr.decode()
    assert weird.exists(), "il log non e' stato creato: quoting sbagliato"
    assert weird.stat().st_size > 0


def test_writer_si_ferma_al_tetto(tmp_path: Path):
    """Con un tetto piccolo il file non cresce oltre (il writer esce da solo)."""
    import shlex
    import subprocess

    target = tmp_path / "cap.log.gz"
    inner = f"gzip -c | head -c 1024 >> {shlex.quote(str(target))}"
    # 100 KiB di input, tetto 1 KiB: head deve troncare
    res = subprocess.run(
        ["/bin/sh", "-c", inner], input=b"B" * (100 * 1024), capture_output=True, check=False
    )
    assert res.returncode == 0, res.stderr.decode()
    assert target.stat().st_size <= 1024


# --- retention --------------------------------------------------------------


def _mk(path: Path, *, age_days: float, size: int) -> Path:
    path.write_bytes(b"x" * size)
    ts = time.time() - age_days * 86400
    os.utime(path, (ts, ts))
    return path


def test_prune_rimuove_per_eta(tmp_path: Path):
    vecchio = _mk(tmp_path / "vecchio.log.gz", age_days=40, size=10)
    recente = _mk(tmp_path / "recente.log.gz", age_days=2, size=10)
    removed = audit.prune_logs(tmp_path, max_age_days=30)
    assert vecchio in removed
    assert not vecchio.exists()
    assert recente.exists()


def test_prune_rispetta_il_budget_dal_piu_vecchio(tmp_path: Path):
    a = _mk(tmp_path / "a.log.gz", age_days=5, size=1000)
    b = _mk(tmp_path / "b.log.gz", age_days=4, size=1000)
    c = _mk(tmp_path / "c.log.gz", age_days=1, size=1000)
    removed = audit.prune_logs(tmp_path, max_total_bytes=2500, max_age_days=30)
    # 3000 byte > 2500: si elimina il piu' vecchio (a) e ci si ferma
    assert removed == [a]
    assert not a.exists() and b.exists() and c.exists()
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 2500


def test_prune_non_tocca_il_file_in_scrittura(tmp_path: Path):
    keep = _mk(tmp_path / "corrente.log.gz", age_days=99, size=10)
    altro = _mk(tmp_path / "altro.log.gz", age_days=99, size=10)
    removed = audit.prune_logs(tmp_path, max_age_days=1, keep=keep)
    assert keep.exists(), "il file che stiamo per scrivere non va mai rimosso"
    assert altro in removed
    assert not altro.exists()


def test_prune_su_dir_inesistente_non_esplode(tmp_path: Path):
    assert audit.prune_logs(tmp_path / "non-esiste") == []


def test_audit_log_path_applica_la_retention(tmp_path: Path, monkeypatch):
    """Chiamare audit_log_path deve anche fare pulizia della cartella."""
    from bravoric_ssh_client.config import Config

    logs = tmp_path / "logs"
    logs.mkdir()
    vecchio = _mk(logs / "20200101_host_sess.log.gz", age_days=999, size=10)
    monkeypatch.setattr(audit, "cfg_path_logs_dir", lambda cfg: logs)

    out = audit.audit_log_path(Config(), "host", "sess")
    assert out.parent == logs
    assert not vecchio.exists(), "il log fuori eta' doveva essere rimosso"
