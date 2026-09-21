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


def test_tmux_send_input(monkeypatch, host: Host):
    from bravoric_ssh_client.ssh.adapter import tmux_send_input

    captured_cmds: list[str] = []

    def fake_run(args, **kwargs):
        # The command passed to ssh is the last argument in argv
        captured_cmds.append(args[-1])
        return FakeCompleted(0, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = SshConfig()

    # 1. Single line short -> keys mode with Enter
    res1 = tmux_send_input(host, "sess", "echo hi", cfg, enter=True)
    assert res1.ok
    assert res1.mode == "keys"
    assert (
        "tmux send-keys -l -t 'sess' 'echo hi' && tmux send-keys -t 'sess' Enter"
        in captured_cmds[-1]
    )

    # 2. Single line short -> keys mode WITHOUT Enter
    res2 = tmux_send_input(host, "sess", "echo hi", cfg, enter=False)
    assert res2.ok
    assert res2.mode == "keys"
    assert captured_cmds[-1] == "tmux send-keys -l -t 'sess' 'echo hi'"

    # 3. Multiline -> paste mode with base64 and Enter
    res3 = tmux_send_input(host, "sess", "line1\nline2", cfg, enter=True)
    assert res3.ok
    assert res3.mode == "paste"
    cmd3 = captured_cmds[-1]
    assert "tmux load-buffer -b" in cmd3
    assert "tmux paste-buffer -p -b" in cmd3
    assert "tmux send-keys -t 'sess' Enter" in cmd3
    assert "tmux delete-buffer -b" in cmd3

    # 4. Multiline -> paste mode WITHOUT Enter
    res4 = tmux_send_input(host, "sess", "line1\nline2", cfg, enter=False)
    assert res4.ok
    assert res4.mode == "paste"
    cmd4 = captured_cmds[-1]
    assert "send-keys -t 'sess' Enter" not in cmd4

    # 5. Empty text with enter=True
    res5 = tmux_send_input(host, "sess", "", cfg, enter=True)
    assert res5.ok
    assert res5.mode == "keys"
    assert captured_cmds[-1] == "tmux send-keys -t 'sess' Enter"

    # 6. With capture_lines
    res6 = tmux_send_input(host, "sess", "ls", cfg, enter=True, capture_lines=40)
    assert res6.ok
    assert "capture-pane -p -t 'sess' -S -40" in captured_cmds[-1]

    # 7. With settle_delay
    res7 = tmux_send_input(host, "sess", "a\nb", cfg, enter=True, settle_delay=0.25)
    assert res7.ok
    assert "sleep 0.25" in captured_cmds[-1]


# ── Test Nuove Funzionalità: Errori Semantici, Retry, Rate Limiting ─────────


def test_ssh_error_classification():
    from bravoric_ssh_client.ssh.adapter import ErrorCategory, SshError

    p_perm = FakeCompleted(255, "", "Permission denied (publickey).")
    err_perm = SshError.from_subprocess(p_perm, "host1")
    assert err_perm.category == ErrorCategory.PERMISSION_DENIED
    assert err_perm.recoverable is False
    assert err_perm.code == "permission_denied"

    p_auth = FakeCompleted(255, "", "Authentication failed.")
    err_auth = SshError.from_subprocess(p_auth, "host1")
    assert err_auth.category == ErrorCategory.AUTH_FAILED
    assert err_auth.recoverable is True

    p_unreach = FakeCompleted(255, "", "ssh: connect to host 1.2.3.4 port 22: No route to host")
    err_unreach = SshError.from_subprocess(p_unreach, "host1")
    assert err_unreach.category == ErrorCategory.HOST_UNREACHABLE
    assert err_unreach.recoverable is True

    p_reset = FakeCompleted(255, "", "Connection reset by peer")
    err_reset = SshError.from_subprocess(p_reset, "host1")
    assert err_reset.category == ErrorCategory.CONNECTION_RESET
    assert err_reset.recoverable is True

    p_timeout = FakeCompleted(255, "", "Connection timed out")
    err_timeout = SshError.from_subprocess(p_timeout, "host1")
    assert err_timeout.category == ErrorCategory.TIMEOUT
    assert err_timeout.recoverable is True

    p_cmd = FakeCompleted(127, "", "bash: mycustomcmd: command not found")
    err_cmd = SshError.from_subprocess(p_cmd, "host1")
    assert err_cmd.category == ErrorCategory.COMMAND_NOT_FOUND
    assert err_cmd.recoverable is False


def test_is_transient_error():
    from bravoric_ssh_client.ssh.adapter import _is_transient_error

    assert _is_transient_error(FakeCompleted(255, "", "Connection timed out")) is True
    assert _is_transient_error(FakeCompleted(255, "", "Connection reset by peer")) is True
    assert _is_transient_error(FakeCompleted(255, "", "Broken pipe")) is True
    assert _is_transient_error(FakeCompleted(255, "", "No route to host")) is True
    assert _is_transient_error(FakeCompleted(255, "", "Network is unreachable")) is True
    assert _is_transient_error(FakeCompleted(255, "", "Permission denied (publickey)")) is False
    assert _is_transient_error(FakeCompleted(0, "success", "")) is False


def test_retry_on_transient_error(monkeypatch, host: Host):
    import time

    from bravoric_ssh_client.ssh.adapter import run_remote_command

    attempts = 0

    def fake_run(args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return FakeCompleted(255, "", "Connection timed out")
        return FakeCompleted(0, "success\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    cfg = SshConfig()
    res = run_remote_command(host, "echo hi", cfg, batch=True, max_retries=3)
    assert res.returncode == 0
    assert res.stdout == "success\n"
    assert attempts == 3


def test_no_retry_when_max_retries_zero(monkeypatch, host: Host):
    import time

    from bravoric_ssh_client.ssh.adapter import run_remote_command

    attempts = 0

    def fake_run(args, **kwargs):
        nonlocal attempts
        attempts += 1
        return FakeCompleted(255, "", "Connection timed out")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    cfg = SshConfig()
    res = run_remote_command(host, "echo hi", cfg, batch=True, max_retries=0)
    assert res.returncode == 255
    assert attempts == 1


def test_auth_rate_limiting():
    from bravoric_ssh_client.ssh.adapter import (
        MAX_AUTH_FAILURES,
        _auth_failures,
        _is_auth_locked,
        _record_auth_failure,
        _reset_auth_failures,
    )

    _auth_failures.clear()
    alias = "test-host-lockout"

    # Initially not locked
    locked, rem = _is_auth_locked(alias)
    assert not locked
    assert rem == 0

    # Record failures below threshold
    for _ in range(MAX_AUTH_FAILURES - 1):
        _record_auth_failure(alias)
    locked, _ = _is_auth_locked(alias)
    assert not locked

    # Reach threshold
    _record_auth_failure(alias)
    locked, rem = _is_auth_locked(alias)
    assert locked
    assert rem > 0

    # Reset
    _reset_auth_failures(alias)
    locked, rem = _is_auth_locked(alias)
    assert not locked
    assert rem == 0
