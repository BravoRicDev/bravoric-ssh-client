"""Test del supporto tmux LOCALE (host localhost, nessun SSH)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from bravoric_ssh_client.config import Host
from bravoric_ssh_client.ssh import adapter as ssh_adapter
from bravoric_ssh_client.ssh import tmux_runner

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shell/exec paths")


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def local_host() -> Host:
    return Host(alias="localhost", host="localhost", auth="key")


def test_is_local_detection():
    assert Host(alias="l", host="localhost").is_local()
    assert Host(alias="l", host="127.0.0.1").is_local()
    assert Host(alias="l", host="::1").is_local()
    assert not Host(alias="l", host="192.168.1.5").is_local()
    # override esplicito
    assert Host(alias="l", host="1.2.3.4", local=True).is_local()
    assert not Host(alias="l", host="localhost", local=False).is_local()


def test_list_local_uses_no_ssh(monkeypatch, local_host):
    calls: list = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return FakeCompleted(0, "main\nwork\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = ssh_adapter.list_tmux_sessions(local_host)
    assert res.ok
    assert res.sessions == ["main", "work"]
    # nessuna chiamata a 'ssh': deve aver eseguito sh -c con tmux locale
    assert all("/bin/sh" in a or "sh" in a[0] for a in calls)


def test_run_tmux_action_local(monkeypatch, local_host):
    def fake_run(args, **kwargs):
        assert "ssh" not in args[0]
        return FakeCompleted(0, "done\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = ssh_adapter.tmux_kill_session(local_host, "sess")
    assert res.ok
    assert res.stdout.strip() == "done"


def test_tmux_present_local(monkeypatch, local_host):
    def fake_run(args, **kwargs):
        return FakeCompleted(0, "yes\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh_adapter.tmux_present(local_host) is True


def test_runner_local_builds_tmux_argv(local_host, monkeypatch):
    """Per host locali l'attach esegue /bin/sh -c con tmux, senza ssh."""
    calls: list = []

    def fake_execvp(a0, argv):
        calls.append(argv)

    monkeypatch.setattr(tmux_runner.os, "execvp", fake_execvp)
    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(tmux_runner, "_set_terminal_title", lambda t: None)
    tmux_runner.tmux_attach(local_host, "sess1", None)
    assert calls
    cmd = calls[0][-1] if calls[0][0].endswith("sh") else " ".join(calls[0])
    assert "tmux attach -t 'sess1'" in cmd
    assert "set-titles-string 'localhost - #S'" in cmd  # titolo per-sessione
    assert "ssh" not in cmd
    # _build_exec resta solo per ssh
    argv, _, _, local = tmux_runner._build_exec(local_host, "tmux attach -t '0'", None)
    assert local is False


def test_runner_local_new_uses_tmux(local_host, monkeypatch):
    calls: list = []

    def fake_execvp(a0, argv):
        calls.append(argv)

    monkeypatch.setattr(tmux_runner.os, "execvp", fake_execvp)
    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(tmux_runner, "_set_terminal_title", lambda t: None)
    tmux_runner.tmux_new(local_host, "prova", None)
    assert calls
    cmd = calls[0][-1] if calls[0][0].endswith("sh") else " ".join(calls[0])
    assert "tmux new -A -s 'prova'" in cmd
    assert "ssh" not in cmd
