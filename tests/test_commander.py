"""Test del lancio di Midnight Commander per lo scambio file (sftp/sftp+locale)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.ssh import commander

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shell/exec paths")


def _host(alias="alpha", hostname="10.0.0.1", user="a", auth="key", port=22):
    return Host(alias=alias, host=hostname, user=user, auth=auth, port=port)


def test_build_url_remote():
    h = _host(port=2222)
    assert commander.build_url(h) == "sh://a@10.0.0.1:2222/"
    assert commander.build_url(h, path="/var/log") == "sh://a@10.0.0.1:2222/var/log"


def test_build_url_default_port():
    h = _host(port=22)
    assert commander.build_url(h) == "sh://a@10.0.0.1/"


def test_build_url_local():
    h = Host(alias="locale", host="localhost", local=True)
    assert commander.build_url(h) == str(Path.home())


def test_build_url_local_path():
    h = Host(alias="locale", host="localhost", local=True)
    assert commander.build_url(h, path="/tmp") == "/tmp"


def test_askpass_helper_matches_by_host():
    helper = commander.write_askpass_helper({"10.0.0.1": "pw1", "10.0.0.2": "pw2"})
    try:
        r = subprocess.run([str(helper), "a@10.0.0.1's password:"], capture_output=True, text=True)
        assert r.returncode == 0
        assert r.stdout == "pw1"
        r = subprocess.run([str(helper), "b@10.0.0.2's password:"], capture_output=True, text=True)
        assert r.returncode == 0
        assert r.stdout == "pw2"
        r = subprocess.run([str(helper), "x@unknown's password:"], capture_output=True, text=True)
        assert r.returncode != 0
    finally:
        helper.unlink(missing_ok=True)


def test_build_env_all_password_resolved(tmp_path: Path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    h1 = _host(hostname="10.0.0.1", auth="keyring")
    h2 = _host(alias="beta", hostname="10.0.0.2", user="b", auth="keyring")

    def resolver(host):
        return f"pw-{host.host}"

    env, helper = commander.build_env(cfg, h1, h2, password_resolver=resolver)
    assert helper is not None
    assert env.get("SSH_ASKPASS") == str(helper)
    assert env.get("SSH_ASKPASS_REQUIRE") == "force"
    assert env.get("DISPLAY")
    helper.unlink(missing_ok=True)


def test_build_env_missing_password_falls_back(tmp_path: Path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    h1 = _host(hostname="10.0.0.1", auth="keyring")
    h2 = _host(alias="beta", hostname="10.0.0.2", user="b", auth="key")

    def resolver(host):
        return "pw" if host.host == "10.0.0.1" else None

    # un host a password senza credenziale -> niente helper (prompt interattivo)
    env, helper = commander.build_env(
        cfg, h1, _host(alias="c", hostname="10.0.0.9", auth="keyring"), password_resolver=resolver
    )
    assert helper is None
    # host a chiave + password risolta -> helper con solo la password risolta
    env, helper = commander.build_env(cfg, h1, h2, password_resolver=resolver)
    assert helper is not None
    helper.unlink(missing_ok=True)


def test_build_env_key_only_no_helper(tmp_path: Path):
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    h1 = _host(auth="key")
    h2 = _host(alias="beta", hostname="10.0.0.2", user="b", auth="key")
    env, helper = commander.build_env(cfg, h1, h2, password_resolver=lambda h: None)
    assert helper is None
    assert "SSH_ASKPASS" not in env


def test_launch_commander_calls_exec(monkeypatch):
    calls = []

    def fake_execvpe(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(commander.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(commander, "mc_path", lambda: "/usr/bin/mc")
    commander.launch_commander(_host(), Host(alias="locale", host="localhost", local=True), {})
    assert len(calls) == 1
    file, argv, env = calls[0]
    assert file == "/usr/bin/mc"
    assert argv[1].startswith("sh://a@10.0.0.1")
    assert argv[2] == str(Path.home())


def test_main_sftp_cli_builds_env_and_execs(monkeypatch, tmp_path: Path):
    import bravoric_ssh_client.app as app_module

    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    cfg.credential_provider = "plain"
    cfg.plain_file = "plain.txt"
    cfg.hosts = [_host(auth=""), _host(alias="beta", hostname="10.0.0.2", user="b", auth="key")]
    (tmp_path / "plain.txt").write_text("a@10.0.0.1\tPWSECRET\n", encoding="utf-8")

    calls = []
    helper_content = {}

    def fake_launch(host_a, host_b, env):
        h = Path(env.get("SSH_ASKPASS", ""))
        helper_content["exists"] = h.exists()
        helper_content["body"] = h.read_text() if h.exists() else ""
        helper_content["force"] = env.get("SSH_ASKPASS_REQUIRE")
        calls.append((host_a, host_b, env))

    monkeypatch.setattr(commander, "launch_commander", fake_launch)
    monkeypatch.setattr(commander, "mc_available", lambda: True)
    app_module._sftp_cli(cfg, "alpha", "local")
    assert len(calls) == 1
    host_a, host_b, env = calls[0]
    assert host_a.alias == "alpha"
    assert host_b.is_local()
    assert helper_content["exists"] is True
    assert helper_content["force"] == "force"
    assert "PWSECRET" in helper_content["body"]
    h = Path(env["SSH_ASKPASS"])
    h.unlink(missing_ok=True)


def test_sftp_target_screen_launches_ptyxis(monkeypatch):
    import subprocess as sp

    from bravoric_ssh_client.app import BravoricApp, SftpTargetScreen

    monkeypatch.setattr(commander, "mc_available", lambda: True)
    cfg = Config()
    cfg.hosts = [_host(), _host(alias="beta", hostname="10.0.0.2", user="b")]
    popen_calls = []

    class FakeProc:
        pass

    def fake_popen(args, **kw):
        popen_calls.append(args)
        return FakeProc()

    monkeypatch.setattr(sp, "Popen", fake_popen)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("F")
            await pilot.pause(0.3)
            assert isinstance(app.screen, SftpTargetScreen)
            await pilot.press("enter")  # seleziona "Locale"
            await pilot.pause(0.4)
            assert popen_calls, "nessuna finestra ptyxis aperta"
            args = popen_calls[0]
            assert args[0] == "ptyxis" or "ptyxis" in args[0]
            assert args[1] == "-x"
            # la stringa comando deve essere eseguibile da ptyxis: primo token sh
            assert args[-1].startswith("sh -c "), args[-1]
            assert "exec " in args[-1]
            assert "--sftp" in args[-1]
            assert "alpha" in args[-1] and "locale" in args[-1]
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_sftp_target_screen_remote_second(monkeypatch):
    import subprocess as sp

    from bravoric_ssh_client.app import BravoricApp

    monkeypatch.setattr(commander, "mc_available", lambda: True)
    cfg = Config()
    cfg.hosts = [_host(), _host(alias="beta", hostname="10.0.0.2", user="b")]
    popen_calls = []

    class FakeProc:
        pass

    def fake_popen(args, **kw):
        popen_calls.append(args)
        return FakeProc()

    monkeypatch.setattr(sp, "Popen", fake_popen)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("F")
            await pilot.pause(0.3)
            await pilot.press("down")  # beta (secondo host remoto)
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.4)
            args = popen_calls[0]
            assert args[-1].startswith("sh -c "), args[-1]
            assert "alpha" in args[-1] and "beta" in args[-1]
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())
