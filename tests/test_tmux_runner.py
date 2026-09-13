"""Test del runner tmux (build comandi + exec) senza lanciare davvero ssh."""

from __future__ import annotations

import pytest

from bravoric_ssh_client.config import Host
from bravoric_ssh_client.ssh import tmux_runner


@pytest.fixture
def host() -> Host:
    return Host(alias="srv", host="10.1.1.5", user="root", port=2222)


def test_build_exec_basic(host: Host):
    argv, env, helper, local = tmux_runner._build_exec(host, None, None)
    joined = " ".join(argv)
    assert argv[0].endswith("ssh")
    assert "-t" in argv
    assert "root@10.1.1.5" in joined
    assert env is None  # nessuna password -> nessun helper
    assert helper is None
    assert local is False


def test_build_exec_with_password_uses_helper(host: Host, tmp_path, monkeypatch):
    helper_file = tmp_path / "ask.sh"
    helper_file.write_text("#!/bin/sh\nprintf '%s' \"$BRAVORIC_PASSWORD\"\n", encoding="utf-8")
    monkeypatch.setattr(tmux_runner, "_write_askpass_helper", lambda pw: helper_file)
    argv, env, helper, _ = tmux_runner._build_exec(host, "tmux attach -t '0'", "pw123")
    assert env is not None
    assert env["BRAVORIC_PASSWORD"] == "pw123"
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert helper is not None
    assert helper.exists()
    # il comando remoto contiene il comando tmux
    assert any("tmux attach" in a for a in argv)
    helper.unlink(missing_ok=True)


def test_build_exec_quotes_session_name(host: Host):
    argv, _, _, _ = tmux_runner._build_exec(host, "tmux attach -t 'my'sess'", None)
    assert any("attach" in a for a in argv)


def test_tmux_new_defaults_to_alias(host: Host):
    argv, _, _, _ = tmux_runner._build_exec(host, "tmux new -A -s 'srv'", None)
    assert any("tmux new -A -s 'srv'" in a for a in argv)


def test_exec_called_posix(host: Host, monkeypatch):
    """Su POSIX tmux_attach deve execvp/execvpe (sostituzione processo)."""
    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    calls: list = []

    def fake_execvpe(a0, argv, env):
        calls.append(argv)

    def fake_execvp(a0, argv):
        calls.append(argv)

    monkeypatch.setattr(tmux_runner.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(tmux_runner.os, "execvp", fake_execvp)
    monkeypatch.setattr(tmux_runner, "_set_terminal_title", lambda t: None)
    tmux_runner.tmux_attach(host, "sess1", None)
    assert calls, "execvp dovrebbe essere chiamato"
    joined = " ".join(calls[0])
    assert "sess1" in joined
    assert "-t" in joined


def test_terminal_title_sets_host_session(host: Host, monkeypatch):
    """Il titolo della finestra deve essere 'HOST - SESSIONE'."""
    import io
    import sys

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setattr(tmux_runner.sys, "stdout", buf)
    tmux_runner._set_terminal_title("srv - main")
    assert buf.getvalue() == "\x1b]0;srv - main\x07"


def test_attach_sets_title(host: Host, monkeypatch):
    titles: list[str] = []

    def fake_title(t):
        titles.append(t)

    monkeypatch.setattr(tmux_runner, "_set_terminal_title", fake_title)
    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(tmux_runner.os, "execvp", lambda a0, argv: None)
    monkeypatch.setattr(tmux_runner.os, "execvpe", lambda a0, argv, env: None)
    tmux_runner.tmux_attach(host, "sess1", None)
    assert titles == ["srv - sess1"], titles


def test_tmux_with_title_wraps_command():
    """Il wrapper deve fissare la stringa titolo con #S (sessione per client) e ripristinarla."""
    cmd = tmux_runner._tmux_with_title("srv", "attach -t 'sess1'")
    # #S viene espanso da tmux per client: ogni finestra mostra la propria sessione
    assert "set-titles-string 'srv - #S'" in cmd
    assert "attach -t 'sess1'" in cmd
    assert 'set-titles-string "$t"' in cmd  # ripristino
    assert cmd.startswith("t=$(tmux show -gv set-titles-string")


def test_tmux_attach_uses_session_title(monkeypatch):
    """tmux_attach deve passare l'alias al wrapper (titolo per-sessione)."""

    host = Host(alias="srv", host="localhost", local=True)
    from bravoric_ssh_client.ssh import tmux_runner as tr

    calls = []
    monkeypatch.setattr(tr, "_exec_local_shell", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(tr, "_set_terminal_title", lambda t: None)
    tr.tmux_attach(host, "sess1", None)
    assert calls, "atteso exec locale"
    assert "set-titles-string 'srv - #S'" in calls[0]
    assert "attach -t 'sess1'" in calls[0]


def test_sh_quote():
    assert tmux_runner._sh_quote("a'b") == "'a'\\''b'"


def test_subprocess_path_windows(host: Host, monkeypatch):
    """Su Windows si usa subprocess.run invece di exec, con cleanup dell'helper."""
    import subprocess

    monkeypatch.setattr(tmux_runner, "_IS_POSIX", False)
    calls: list = []

    def fake_subprocess_run(argv, **kwargs):
        calls.append(argv)
        return None

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        tmux_runner.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code))
    )

    helper = tmux_runner.Path("unused-helper.sh")
    monkeypatch.setattr(tmux_runner, "_write_askpass_helper", lambda pw: helper)
    monkeypatch.setattr(tmux_runner, "_cleanup_helper", lambda h: None)

    try:
        tmux_runner.tmux_attach(host, "sess1", "pw")
    except SystemExit:
        pass
    assert calls, "subprocess.run dovrebbe essere chiamato"
    assert "sess1" in " ".join(calls[0])
