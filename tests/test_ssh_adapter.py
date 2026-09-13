"""Test dell'adapter SSH con mock di subprocess (nessuna rete reale)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bravoric_ssh_client.config import Host
from bravoric_ssh_client.ssh.adapter import (
    SshConfig,
    list_tmux_sessions,
    run_remote_command,
)


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def host() -> Host:
    return Host(alias="srv", host="192.0.2.10", user="root", auth="keyring")


def test_list_ok_without_password(monkeypatch, host: Host):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return FakeCompleted(0, "main\nwork\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = SshConfig()
    res = list_tmux_sessions(host, cfg)
    assert res.ok
    assert res.sessions == ["main", "work"]


def test_list_fallback_to_password(monkeypatch, host: Host):
    # primo tentativo (BatchMode, no password) fallisce -> poi con password ok
    attempts: list[str] = []

    def fake_run(args, **kwargs):
        attempts.append("askpass" if kwargs.get("env", {}).get("BRAVORIC_PASSWORD") else "batch")
        if "BRAVORIC_PASSWORD" in kwargs.get("env", {}):
            return FakeCompleted(0, "sess1\n")
        return FakeCompleted(255, "", "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = SshConfig(password_provider=lambda h: "pw123")
    res = list_tmux_sessions(host, cfg)
    assert res.ok
    assert res.sessions == ["sess1"]
    assert attempts == ["batch", "askpass"]


def test_list_fails_without_any_credential(monkeypatch, host: Host):
    def fake_run(args, **kwargs):
        return FakeCompleted(255, "", "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = SshConfig(password_provider=lambda h: None)
    res = list_tmux_sessions(host, cfg)
    assert not res.ok
    assert "Permission denied" in res.error


def test_run_remote_uses_correct_ssh_and_timeout(monkeypatch, host: Host):
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        return FakeCompleted(0, "yes\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = SshConfig(ssh_bin="/usr/bin/ssh")
    run_remote_command(host, "echo hi", cfg, batch=True, timeout=9)
    assert captured["args"][0] == "/usr/bin/ssh"
    assert "-o" in captured["args"]
    assert captured["timeout"] == 9
    joined = " ".join(captured["args"])
    assert "root@192.0.2.10" in joined


def test_askpass_helper_contains_password_only_in_env(monkeypatch, host: Host, tmp_path: Path):
    """La password non deve mai comparire in argv."""

    captured_args: list[str] = []
    calls = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        env = kwargs.get("env", {})
        assert "pw-super-segreta" not in " ".join(args)
        calls.append(env)
        if "BRAVORIC_PASSWORD" in env:
            assert env.get("BRAVORIC_PASSWORD") == "pw-super-segreta"
            assert "SSH_ASKPASS_REQUIRE" in env
            return FakeCompleted(0, "x\n")
        return FakeCompleted(255, "", "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = SshConfig(password_provider=lambda h: "pw-super-segreta")
    list_tmux_sessions(host, cfg)
    assert "pw-super-segreta" not in " ".join(captured_args)
    # almeno una chiamata è passata con la password solo in env
    assert any(
        "BRAVORIC_PASSWORD" in e and e["BRAVORIC_PASSWORD"] == "pw-super-segreta" for e in calls
    )
