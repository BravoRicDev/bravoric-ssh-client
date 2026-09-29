"""Test sui tetti dell'audit: la cartella dei log non deve riempire il disco.

Sul campo si era arrivati a ~14 GB con un singolo file da 5 GB: il writer era uno
stream gzip mai chiuso e non esisteva alcuna retention. Questi test bloccano la
regressione su tre fronti:
  - il writer tmux e' ``exec gzip``, senza pipeline: cosi' tmux parla direttamente
    col writer e non restano orfani (un orfano tiene aperto l'inode di un file
    cancellato, quindi la retention non libererebbe davvero lo spazio);
  - il writer non riapre un file che ha gia' raggiunto MAX_LOG_BYTES;
  - la cartella viene potata per eta' e budget in locale, e per eta' e numero di file
    sul server (dove ``prune_logs`` non puo' girare).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from bravoric_ssh_client.ssh import audit

# --- cap del writer ---------------------------------------------------------


def test_pipe_pane_writer_senza_pipeline(tmp_path: Path):
    """Il writer deve essere ``exec gzip``: una pipeline lascerebbe gzip orfano
    quando tmux termina la shell, e un orfano impedisce di liberare lo spazio."""
    import shlex

    cmd = audit.tmux_pipe_pane_enable("sess", tmp_path / "a.log.gz")
    inner = shlex.split(cmd)[-1]
    assert "exec gzip" in inner
    # `||` (fallback) e' ammesso: una pipe no
    assert "|" not in inner.replace("||", ""), "una pipeline lascerebbe orfani"
    assert str(audit.MAX_LOG_BYTES) in inner  # controllo di dimensione all'apertura
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


def test_writer_non_riapre_un_file_pieno(tmp_path: Path):
    """Se il log ha gia' raggiunto il tetto il writer esce senza scrivere: il file
    non puo' ricominciare a crescere a ogni attach."""
    import shlex
    import subprocess

    target = tmp_path / "pieno.log.gz"
    target.write_bytes(b"z" * 100)
    cmd = audit.tmux_pipe_pane_enable("sess", target)
    # tetto abbassato sotto la dimensione attuale, solo per la prova
    inner = shlex.split(cmd)[-1].replace(str(audit.MAX_LOG_BYTES), "50")
    before = target.stat().st_size
    res = subprocess.run(
        ["/bin/sh", "-c", inner], input=b"A" * 5000, capture_output=True, check=False
    )
    assert res.returncode != 0, "con il file pieno il writer deve uscire senza scrivere"
    assert target.stat().st_size == before


# --- writer dei log sul server (dove il client non puo' fare pulizia) --------


def test_remote_writer_usa_home_e_niente_pipeline():
    import shlex

    cmd = audit.tmux_pipe_pane_enable_remote("sess", "20260929_host_x.log.gz")
    inner = shlex.split(cmd)[-1]
    assert "$HOME/.bravoric-ssh-client/logs/20260929_host_x.log.gz" in inner
    assert "exec gzip" in inner
    assert "|" not in inner.replace("||", "")
    assert str(audit.MAX_LOG_BYTES) in inner


def test_prune_remote_pota_per_eta_e_numero():
    assert "mkdir -p" in audit.PRUNE_REMOTE
    assert "find" in audit.PRUNE_REMOTE and "-delete" in audit.PRUNE_REMOTE
    assert str(audit.MAX_AGE_DAYS) in audit.PRUNE_REMOTE
    # tiene solo i MAX_KEEP_REMOTE piu' recenti
    assert str(audit.MAX_KEEP_REMOTE + 1) in audit.PRUNE_REMOTE


def test_audit_pipe_cmds_remoto_ha_tetto_e_prune(tmp_path: Path):
    """Il ramo remoto deve usare il writer nuovo e la retention: prima lanciava un
    ``gzip -c >> $HOME/...`` senza alcun tetto e senza pulizia, e i log remoti
    crescevano senza limite."""
    from bravoric_ssh_client.config import Host
    from bravoric_ssh_client.ssh import tmux_runner

    host = Host(alias="srv", host="10.1.1.5", user="root")
    enable, disable = tmux_runner._audit_pipe_cmds(host, "sess", tmp_path / "x.log.gz")
    assert "exec gzip" in enable
    assert "$HOME/.bravoric-ssh-client/logs" in enable
    assert "find" in enable  # retention sul server
    assert "tail -n +" in enable  # tetto sul numero di file
    assert "pipe-pane -t 'sess'" in disable


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
