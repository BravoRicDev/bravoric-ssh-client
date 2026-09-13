"""Test per jump host, audit trail e config (tunnels/jump_host/audit_log)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bravoric_ssh_client.config import Config, Host, Tunnel, load_config, save_config
from bravoric_ssh_client.ssh import audit, tmux_runner
from bravoric_ssh_client.ssh.adapter import (
    SshConfig,
    _ssh_base_args,
    list_tmux_sessions,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shell/exec paths")

# ---------- Config: tunnels / jump_host / audit_log ----------


def test_config_roundtrip_tunnels_jump_audit(tmp_path: Path):
    cfg = Config(
        audit_log=True,
        path=tmp_path / "config.toml",
        hosts=[
            Host(
                alias="app",
                host="10.0.0.9",
                user="deploy",
                jump_host="bastion",
                tunnels=[
                    Tunnel(
                        name="db",
                        kind="L",
                        local_port=5432,
                        remote_host="localhost",
                        remote_port=5432,
                    ),
                    Tunnel(kind="D", local_port=1080),
                ],
            ),
            Host(alias="bastion", host="1.2.3.4", user="adm"),
        ],
    )
    save_config(cfg)
    loaded = load_config(tmp_path / "config.toml")
    assert loaded.audit_log is True
    app = loaded.host("app")
    assert app is not None
    assert app.jump_host == "bastion"
    assert len(app.tunnels) == 2
    assert app.tunnels[0].kind == "L"
    assert app.tunnels[0].local_port == 5432
    assert app.tunnels[0].remote_host == "localhost"
    assert app.tunnels[1].kind == "D"
    assert app.tunnels[1].local_port == 1080


def test_config_invalid_tunnel_kind(tmp_path: Path):
    cfg = Config(path=tmp_path / "c.toml", hosts=[Host(alias="a", host="h")])
    save_config(cfg)
    (tmp_path / "c.toml").write_text(
        (tmp_path / "c.toml")
        .read_text()
        .replace(
            '[[hosts]]\nalias = "a"',
            '[[hosts]]\nalias = "a"\n[[hosts.tunnels]]\nkind = "X"\nlocal_port = 1',
        )
    )
    from bravoric_ssh_client.config import ConfigError

    with pytest.raises(ConfigError):
        load_config(tmp_path / "c.toml")


# ---------- Jump host: adapter ----------


def test_ssh_base_args_adds_jump(host_jump):
    host, cfg = host_jump
    args = _ssh_base_args(host, cfg, batch=True)
    joined = " ".join(args)
    assert "-J" in joined
    assert "adm@1.2.3.4" in joined


def test_list_tmux_sessions_uses_jump(monkeypatch, host_jump):
    host, cfg = host_jump
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "s1\n", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = list_tmux_sessions(host, cfg)
    assert res.ok
    assert "-J" in " ".join(calls[0])


def test_run_remote_with_jump_password_multihost(monkeypatch, host_jump):
    host, cfg = host_jump
    # serve password: primo batch fallisce, poi multi-host askpass con entrambe
    attempts: list[dict] = []

    def fake_run(args, **kwargs):
        env = kwargs.get("env", {})
        attempts.append({"env": env, "args": args})
        if env.get("SSH_ASKPASS_REQUIRE") == "force":
            return type("R", (), {"returncode": 0, "stdout": "s1\n", "stderr": ""})()
        return type("R", (), {"returncode": 255, "stdout": "", "stderr": "denied"})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = list_tmux_sessions(host, cfg)
    assert res.ok
    ask = [a for a in attempts if a["env"].get("SSH_ASKPASS_REQUIRE") == "force"]
    assert ask, "atteso tentativo con password"


@pytest.fixture
def host_jump():
    host = Host(alias="app", host="10.0.0.9", user="deploy", jump_host="bastion")
    jump = Host(alias="bastion", host="1.2.3.4", user="adm")
    cfg = SshConfig(
        password_provider=lambda h: "pw-app" if h.alias == "app" else "pw-bastion",
        jump_resolver=lambda h: jump if h.jump_host else None,
    )
    return host, cfg


# ---------- Jump host: tmux_runner ----------


def test_build_exec_jump_host():
    host = Host(alias="app", host="10.0.0.9", user="deploy", jump_host="bastion")
    jump = Host(alias="bastion", host="1.2.3.4", user="adm")
    argv, env, helper, _ = tmux_runner._build_exec(
        host, "tmux attach -t '0'", None, jump_host=jump, jump_password=None
    )
    joined = " ".join(argv)
    assert "-J adm@1.2.3.4" in joined
    assert "deploy@10.0.0.9" in joined
    assert env is None  # niente password -> niente helper
    assert helper is None


def test_build_exec_jump_with_passwords(monkeypatch, tmp_path: Path):
    from bravoric_ssh_client.ssh import tmux_runner as tr

    monkeypatch.setattr(tr, "write_askpass_helper", lambda mapping: tmp_path / "ask.sh")
    (tmp_path / "ask.sh").write_text("#!/bin/sh\n")
    host = Host(alias="app", host="10.0.0.9", user="deploy", jump_host="bastion")
    jump = Host(alias="bastion", host="1.2.3.4", user="adm")
    argv, env, helper, _ = tr._build_exec(
        host, "tmux attach -t '0'", "pw-app", jump_host=jump, jump_password="pw-bastion"
    )
    assert env is not None
    assert env["SSH_ASKPASS"] == str(tmp_path / "ask.sh")
    assert helper is not None
    assert "-J" in " ".join(argv)


def test_tmux_attach_passes_jump(monkeypatch):
    host = Host(alias="app", host="10.0.0.9", user="deploy")
    jump = Host(alias="bastion", host="1.2.3.4", user="adm")
    calls: list[list[str]] = []

    def fake_execvpe(a0, argv, env):
        calls.append(argv)

    def fake_execvp(a0, argv):
        calls.append(argv)

    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(tmux_runner.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(tmux_runner.os, "execvp", fake_execvp)
    monkeypatch.setattr(tmux_runner, "_set_terminal_title", lambda t: None)
    tmux_runner.tmux_attach(host, "sess1", None, jump_host=jump)
    assert calls
    assert "-J adm@1.2.3.4" in " ".join(calls[0])


# ---------- Audit trail ----------


def test_audit_log_path(tmp_path: Path):
    cfg = Config(path=tmp_path / "config.toml")
    p = audit.audit_log_path(cfg, "srv", "Claude - CMS")
    assert p.suffix == ".gz"
    assert "srv" in p.name
    assert p.parent == tmp_path / "logs"


def test_audit_script_wrap(tmp_path: Path):
    out = tmp_path / "20260909_srv_sess.log.gz"
    cmd = audit.script_wrap("ssh -t host", out)
    assert cmd.startswith("script -q -f ")
    assert "ssh -t host" in cmd
    assert "gzip -f" in cmd


def test_audit_pipe_pane():
    on = audit.tmux_pipe_pane_enable("sess", Path("/tmp/x.log.gz"))
    assert "pipe-pane" in on and "gzip -c >>" in on
    off = audit.tmux_pipe_pane_disable("sess")
    assert "pipe-pane -t 'sess'" in off


def test_audit_read_log_gz(tmp_path: Path):
    import gzip

    p = tmp_path / "t.log.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("riga1\nriga2\n")
    content = audit.read_log_gz(p)
    assert "riga1" in content and "riga2" in content


def test_tmux_attach_audit_local(monkeypatch, tmp_path: Path):
    """Con audit, un attach locale inietta il pipe-pane che registra la sessione."""
    host = Host(alias="srv", host="localhost", local=True)
    calls: list[list[str]] = []

    def fake_execvpe(a0, argv, env):
        calls.append(argv)

    def fake_execvp(a0, argv):
        calls.append(argv)

    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(tmux_runner.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(tmux_runner.os, "execvp", fake_execvp)
    monkeypatch.setattr(tmux_runner, "_set_terminal_title", lambda t: None)
    out = tmp_path / "x.log.gz"
    tmux_runner.tmux_attach(host, "sess1", None, audit=out)
    assert calls
    joined = " ".join(calls[0])
    assert "pipe-pane" in joined  # pipe-pane registra l'I/O della sessione
    assert "gzip -c >>" in joined
    assert out.name in joined  # il file target è il .gz locale


def test_tmux_attach_audit_remote_uses_home_logs(monkeypatch, tmp_path: Path):
    """Attach remoto con audit: pipe-pane su ~/.bravoric-ssh-client/logs del server."""
    host = Host(alias="srv", host="10.0.0.9", user="deploy")
    calls: list[list[str]] = []

    def fake_execvpe(a0, argv, env):
        calls.append(argv)

    def fake_execvp(a0, argv):
        calls.append(argv)

    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(tmux_runner.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(tmux_runner.os, "execvp", fake_execvp)
    monkeypatch.setattr(tmux_runner, "_set_terminal_title", lambda t: None)
    out = tmp_path / "x.log.gz"
    tmux_runner.tmux_attach(host, "sess1", None, audit=out)
    assert calls
    joined = " ".join(calls[0])
    assert "$HOME/.bravoric-ssh-client/logs/" in joined  # log sul server
    assert "sess1" in joined


def test_ssh_shell_audit_skipped_without_script(monkeypatch, tmp_path: Path):
    """Senza 'script', la shell parte comunque (audit ignorato)."""
    monkeypatch.setattr("bravoric_ssh_client.ssh.audit.script_available", lambda: False)
    host = Host(alias="srv", host="10.0.0.9", user="deploy")
    calls: list[list[str]] = []

    def fake_execvpe(a0, argv, env):
        calls.append(argv)

    def fake_execvp(a0, argv):
        calls.append(argv)

    monkeypatch.setattr(tmux_runner, "_IS_POSIX", True)
    monkeypatch.setattr(tmux_runner.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(tmux_runner.os, "execvp", fake_execvp)
    monkeypatch.setattr(tmux_runner, "_set_terminal_title", lambda t: None)
    out = tmp_path / "x.log.gz"
    tmux_runner.ssh_shell(host, None, audit=out)
    assert calls
    joined = " ".join(calls[0])
    assert "script -q -f" not in joined  # audit disattivato
    assert "deploy@10.0.0.9" in joined
