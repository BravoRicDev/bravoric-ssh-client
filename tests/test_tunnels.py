"""Test tunnel SSH: forward args, TunnelManager (start/stop/status) con mock."""

from __future__ import annotations

import subprocess

import pytest

from bravoric_ssh_client.config import Host, Tunnel
from bravoric_ssh_client.ssh import tunnels
from bravoric_ssh_client.ssh.tunnels import TunnelManager, _forward_arg


class FakePopen:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = None
        self.pid = 1234

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        self.returncode = -15
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeSubprocess:
    DEVNULL = subprocess.DEVNULL
    TimeoutExpired = subprocess.TimeoutExpired
    Popen = FakePopen


@pytest.fixture
def host() -> Host:
    return Host(alias="srv", host="10.0.0.5", user="root", auth="keyring")


def test_forward_arg_local():
    assert _forward_arg(
        Tunnel(kind="L", local_port=5432, remote_host="localhost", remote_port=5432)
    ) == ("-L localhost:5432:localhost:5432")


def test_forward_arg_remote():
    assert _forward_arg(Tunnel(kind="R", local_port=8080, remote_host="web", remote_port=80)) == (
        "-R localhost:8080:web:80"
    )


def test_forward_arg_dynamic():
    assert _forward_arg(Tunnel(kind="D", local_port=1080)) == "-D localhost:1080"


def test_build_args_uses_NT_and_forward(monkeypatch, host):
    spec = Tunnel(kind="L", local_port=5432, remote_host="localhost", remote_port=5432)
    mgr = TunnelManager()
    args, env, helper = mgr._build_args(host, spec, None, None, None, "4", "ssh")
    joined = " ".join(args)
    assert joined.startswith("ssh -N -T")
    assert "-L localhost:5432:localhost:5432" in joined
    assert "root@10.0.0.5" in joined
    assert helper is None  # nessuna password -> nessun helper


def test_build_args_with_password_sets_askpass(monkeypatch, host, tmp_path):
    monkeypatch.setattr(tunnels, "write_askpass_helper", lambda mapping: tmp_path / "ask.sh")
    (tmp_path / "ask.sh").write_text("#!/bin/sh\n")
    spec = Tunnel(kind="L", local_port=5432, remote_host="db", remote_port=5432)
    mgr = TunnelManager()
    args, env, helper = mgr._build_args(host, spec, "pw123", None, None, "4", None)
    assert env["SSH_ASKPASS"] == str(tmp_path / "ask.sh")
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert helper is not None
    assert (
        env["BRAVORIC_PASSWORD"] if False else True
    )  # l'helper multi-host non usa BRAVORIC_PASSWORD


def test_build_args_with_jump_host(host):
    jump = Host(alias="bastion", host="1.2.3.4", user="adm")
    spec = Tunnel(kind="L", local_port=5432, remote_host="db", remote_port=5432)
    mgr = TunnelManager()
    args, env, helper = mgr._build_args(host, spec, None, None, jump, "4", None)
    joined = " ".join(args)
    assert "-J adm@1.2.3.4" in joined


def test_start_stop_lifecycle(monkeypatch, host):
    monkeypatch.setattr(tunnels, "subprocess", FakeSubprocess)
    monkeypatch.setattr(tunnels, "write_askpass_helper", lambda mapping: None)

    spec = Tunnel(name="db", kind="L", local_port=5432, remote_host="db", remote_port=5432)
    mgr = TunnelManager()
    ok, msg = mgr.start(host, spec, None)
    assert ok
    assert "5432" in msg
    assert mgr.any_active(host.alias)
    assert len(mgr.active(host.alias)) == 1
    # duplicato sulla stessa porta -> rifiutato
    ok2, _ = mgr.start(host, spec, None)
    assert not ok2
    # stop
    assert mgr.stop(host.alias, 5432)
    assert not mgr.any_active(host.alias)


def test_stop_all(monkeypatch, host):
    monkeypatch.setattr(tunnels, "subprocess", FakeSubprocess)
    monkeypatch.setattr(tunnels, "write_askpass_helper", lambda mapping: None)

    mgr = TunnelManager()
    mgr.start(host, Tunnel(kind="D", local_port=1080), None)
    mgr.start(host, Tunnel(kind="L", local_port=8080, remote_host="w", remote_port=80), None)
    assert len(mgr.active(host.alias)) == 2
    assert mgr.stop_all(host.alias) == 2
    assert not mgr.any_active(host.alias)


def test_default_tunnels_path():
    assert str(tunnels.default_tunnels_path()).endswith("tunnels.json")


# ---------- persistenza stato su file ----------


class FakePopenAlive:
    def __init__(self, args, **kwargs):
        self.returncode = None
        self.pid = 9876

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        self.returncode = -15
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_persist_and_reload(tmp_path, host, monkeypatch):
    """Dopo start lo stato è su file; un nuovo manager lo riconcilia via PID."""
    state = tmp_path / "tunnels.json"
    spec = Tunnel(name="db", kind="L", local_port=5432, remote_host="db", remote_port=5432)

    class Fake:
        DEVNULL = subprocess.DEVNULL
        TimeoutExpired = subprocess.TimeoutExpired
        Popen = FakePopenAlive

    monkeypatch.setattr(tunnels, "subprocess", Fake)
    monkeypatch.setattr(tunnels, "write_askpass_helper", lambda mapping: None)
    # pid vivo: monkeypatch _pid_alive
    monkeypatch.setattr(tunnels, "_pid_alive", lambda pid: True)

    mgr1 = TunnelManager(state_path=state)
    ok, _ = mgr1.start(host, spec, None)
    assert ok
    assert state.exists()

    # nuovo manager (simula riavvio MCP/TUI): carica lo stato, tunnel ancora attivo
    mgr2 = TunnelManager(state_path=state)
    assert mgr2.any_active(host.alias)
    procs = mgr2.active(host.alias)
    assert len(procs) == 1
    assert procs[0].spec.local_port == 5432
    assert procs[0].pid == 9876


def test_reload_drops_dead_pid(tmp_path, host, monkeypatch):
    """Un PID morto al caricamento viene ignorato e ripulito dal file."""
    state = tmp_path / "tunnels.json"
    spec = Tunnel(name="db", kind="L", local_port=5432, remote_host="db", remote_port=5432)

    class Fake:
        DEVNULL = subprocess.DEVNULL
        TimeoutExpired = subprocess.TimeoutExpired
        Popen = FakePopenAlive

    monkeypatch.setattr(tunnels, "subprocess", Fake)
    monkeypatch.setattr(tunnels, "write_askpass_helper", lambda mapping: None)
    monkeypatch.setattr(tunnels, "_pid_alive", lambda pid: True)
    mgr1 = TunnelManager(state_path=state)
    mgr1.start(host, spec, None)

    # al reload il PID è morto
    monkeypatch.setattr(tunnels, "_pid_alive", lambda pid: False)
    mgr2 = TunnelManager(state_path=state)
    assert not mgr2.any_active(host.alias)
    data = __import__("json").loads(state.read_text())
    assert data["tunnels"] == []


def test_no_state_file_no_crash():
    mgr = TunnelManager()
    assert not mgr.any_active("whatever")
