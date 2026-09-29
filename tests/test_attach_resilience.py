"""Test di regressione sui fix di resilienza dell'attach.

Contesto del bug: `tmux attach -t 'X'` esce all'istante quando la sessione X non
esiste; il comando ssh termina in poche centinaia di ms e la finestra del terminale
si chiude subito (sintomo segnalato: "la finestra si apre e si chiude").

Questi test bloccano la regressione su:
  1. attach semplice -> attach-or-create (`new -A -s`), mai attach puro;
  2. attach in sola lettura invariato (non deve creare nulla);
  3. nessun `-s` duplicato nel ramo con audit;
  4. kitty lanciato con `--hold` (la finestra non sparisce);
  5. cleanup dell'helper SSH_ASKPASS senza `os.fork()` nudo;
  6. `restart_after_ssh` eseguito in una shell che sopravvive all'exec.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bravoric_ssh_client.app import terminal_argv
from bravoric_ssh_client.config import Host
from bravoric_ssh_client.ssh import tmux_runner


@pytest.fixture
def host() -> Host:
    return Host(alias="srv", host="10.1.1.5", user="root")


# --- 1. attach semplice: attach-or-create ----------------------------------


def test_attach_aggancia_o_crea(host: Host):
    """L'attach deve agganciare se la sessione esiste e crearla se manca."""
    cmd = tmux_runner._attach_cmd(host, "mysess", "attach", None)
    assert "tmux new -A -s 'mysess'" in cmd
    # l'attach puro morirebbe se la sessione non esiste -> finestra che si chiude
    assert "tmux attach -t 'mysess'" not in cmd


def test_attach_senza_audit_non_perde_il_titolo(host: Host):
    cmd = tmux_runner._attach_cmd(host, "mysess", "attach", None)
    assert "set-titles-string 'srv - #S'" in cmd
    assert "refresh-client" in cmd


# --- 2. attach in sola lettura: invariato ----------------------------------


def test_attach_ro_resta_sola_lettura(host: Host):
    cmd = tmux_runner._attach_cmd(host, "mysess", "attach -r", None)
    assert "tmux attach -r -t 'mysess'" in cmd
    # in sola lettura non si deve creare nulla
    assert "new -A" not in cmd


def test_attach_ro_con_audit_non_crea(host: Host, tmp_path: Path):
    cmd = tmux_runner._attach_cmd(host, "mysess", "attach -r", tmp_path / "a.log.gz")
    assert "tmux attach -r -t 'mysess'" in cmd
    assert "new -A" not in cmd


# --- 3. nessun flag `-s` duplicato -----------------------------------------


@pytest.mark.parametrize("base", ["attach", "new -A -s"])
def test_audit_non_duplica_flag_s(host: Host, tmp_path: Path, base: str):
    """Regressione: prima si generava `tmux new -A -s -d -s 'x'` (due volte -s)."""
    cmd = tmux_runner._attach_cmd(host, "mysess", base, tmp_path / "a.log.gz", create=True)
    assert cmd.count("-s 'mysess'") == 1, cmd


def test_attach_con_audit_assicura_poi_aggancia(host: Host, tmp_path: Path):
    cmd = tmux_runner._attach_cmd(host, "mysess", "attach", tmp_path / "a.log.gz")
    assert "tmux new -A -d -s 'mysess'" in cmd  # assicura la sessione, detached
    assert "tmux attach -t 'mysess'" in cmd  # poi aggancia davvero
    assert "pipe-pane" in cmd  # audit attivo


def test_nome_sessione_con_apostrofo_e_quotato(host: Host):
    cmd = tmux_runner._attach_cmd(host, "my'sess", "attach", None)
    # il nome grezzo non deve comparire: verrebbe interpretato dalla shell
    assert "'my'sess'" not in cmd
    assert "my'\\''sess" in cmd


# --- 4. kitty con --hold ---------------------------------------------------


def test_kitty_usa_hold():
    argv = terminal_argv("/usr/bin/kitty", "exec 'x' --attach 'h' 's'")
    assert argv[:4] == ["/usr/bin/kitty", "--hold", "sh", "-c"]
    assert argv[-1] == "exec 'x' --attach 'h' 's'"


def test_ptyxis_invariato():
    argv = terminal_argv("/usr/bin/ptyxis", "CMD")
    assert argv == ["/usr/bin/ptyxis", "-x", "sh -c 'CMD'"]


def test_gnome_terminal_invariato():
    argv = terminal_argv("/usr/bin/gnome-terminal", "CMD")
    assert argv == ["/usr/bin/gnome-terminal", "--", "sh", "-c", "CMD"]


# --- 5. cleanup dell'helper senza os.fork() --------------------------------


def test_cleanup_non_usa_fork(monkeypatch, tmp_path: Path):
    """Il guardiano non deve usare os.fork() nudo: fork senza exec in un processo
    multi-threaded (Textual) eredita i lock degli altri thread e lascia una copia
    integrale del processo in memoria a ogni connessione."""
    calls: list = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    def no_fork():
        raise AssertionError("os.fork() nudo non deve essere usato dal cleanup")

    monkeypatch.setattr(os, "fork", no_fork)
    monkeypatch.setattr(tmux_runner.subprocess, "Popen", FakePopen)

    helper = tmp_path / "bravoric-askpass-x.sh"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    tmux_runner._schedule_cleanup_posix(helper)

    assert calls, "deve lanciare un processo guardiano separato"
    argv, kwargs = calls[0]
    assert argv[0] == "/bin/sh"
    assert str(helper) in argv
    assert kwargs.get("start_new_session") is True
    # il guardiano attende la fine del padre e poi rimuove il file
    assert "kill -0" in argv[2]
    assert "rm -f" in argv[2]


# --- 6. restart_after_ssh --------------------------------------------------


def test_restart_avvolge_in_shell(monkeypatch):
    """Con BRAVORIC_RESTART_COMMAND il comando gira in una shell che, al termine
    della sessione ssh, rilancia la TUI (l'exec ha sostituito il processo)."""
    seen: dict = {}

    def fake_execvpe(a0, argv, env):
        seen["argv"] = argv
        seen["env"] = env

    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    monkeypatch.setattr(os, "execvp", lambda *a: None)
    monkeypatch.setenv("BRAVORIC_RESTART_COMMAND", "bravoric-tui --x")
    # In produzione _build_exec costruisce env a partire da os.environ, quindi la
    # variabile arriva dentro env: replichiamo lo stesso scenario.
    env = {"PATH": "/bin", "BRAVORIC_RESTART_COMMAND": "bravoric-tui --x"}

    tmux_runner._run_or_exec(["ssh", "-t", "h"], env, None)

    argv = seen["argv"]
    assert argv[0] == "/bin/sh"
    assert argv[1] == "-c"
    assert "ssh -t h" in argv[2]
    assert "exec bravoric-tui --x" in argv[2]
    # fallback: se il rilancio fallisce si apre una shell, la finestra non muore
    assert "exec ${SHELL:-/bin/sh}" in argv[2]
    # la variabile non deve propagarsi all'ambiente eseguito
    assert "BRAVORIC_RESTART_COMMAND" not in (seen["env"] or {})


def test_senza_restart_esegue_comando_invariato(monkeypatch):
    """Senza restart_after_ssh l'argv passato a exec non deve cambiare."""
    seen: dict = {}

    def fake_execvpe(a0, argv, env):
        seen["argv"] = argv

    def fake_execvp(a0, argv):
        seen["argv"] = argv

    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    monkeypatch.setattr(os, "execvp", fake_execvp)
    monkeypatch.delenv("BRAVORIC_RESTART_COMMAND", raising=False)

    tmux_runner._run_or_exec(["ssh", "-t", "h"], None, None)
    assert seen["argv"] == ["ssh", "-t", "h"]


# --- 7. adattamento del TERM (agnostico al terminale del server) -------------


def _term_dopo_il_guard(term: str) -> str:
    """Esegue davvero il preambolo in /bin/sh e ritorna il TERM risultante."""
    script = f'{tmux_runner._TERM_GUARD}; printf %s "$TERM"'
    res = subprocess.run(
        ["/bin/sh", "-c", script],
        env={**os.environ, "TERM": term},
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


def test_term_guard_sostituisce_term_sconosciuto():
    """Un TERM che il server non conosce (es. xterm-kitty senza terminfo) deve
    essere rimpiazzato con uno utilizzabile: senza questo tmux muore con
    'missing or unsuitable terminal' e la finestra non mostra la sessione."""
    out = _term_dopo_il_guard("term-inesistente-xyz")
    assert out in (
        "xterm-256color",
        "xterm",
        "screen",
        "tmux-256color",
        "tmux",
        "vt100",
    ), out
    assert out != "term-inesistente-xyz"


def test_term_guard_lascia_intatto_term_valido():
    """Se il TERM corrente e' utilizzabile non va toccato: nessun downgrade inutile."""
    assert _term_dopo_il_guard("xterm-256color") == "xterm-256color"


def test_term_guard_e_esportato():
    """Il TERM scelto deve essere esportato, altrimenti tmux (processo figlio)
    continuerebbe a vedere quello sbagliato."""
    script = f"{tmux_runner._TERM_GUARD}; sh -c 'printf %s \"$TERM\"'"
    res = subprocess.run(
        ["/bin/sh", "-c", script],
        env={**os.environ, "TERM": "term-inesistente-xyz"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout != "term-inesistente-xyz"
    assert res.stdout in ("xterm-256color", "xterm", "screen", "tmux-256color", "tmux", "vt100")


def test_attach_cmd_include_il_guard(host: Host):
    """Il comando di attach deve portarsi dietro il preambolo di adattamento."""
    for base in ("attach", "attach -r"):
        cmd = tmux_runner._attach_cmd(host, "sess", base, None)
        assert "for _bt in" in cmd, base
        assert "export TERM" in cmd, base
