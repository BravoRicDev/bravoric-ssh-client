"""Test del server MCP (registrazione tool + chiamate con adapter mockato)."""

from __future__ import annotations

import asyncio
import gzip
from pathlib import Path

import pytest

from bravoric_ssh_client.config import Config, Host, Tunnel
from bravoric_ssh_client.mcp_server import BravoricMcp


def make_cfg(tmp_path: Path) -> Config:
    cfg = Config(
        path=tmp_path / "config.toml",
        snippets_file=str(tmp_path / "snippets.json"),
    )
    cfg.hosts = [
        Host(alias="alpha", host="192.0.2.1", user="alice", auth="key"),
        Host(alias="loc", host="localhost", local=True),
    ]
    return cfg


def _run(coro):
    return asyncio.run(coro)


async def _call(mcp: BravoricMcp, name: str, args: dict) -> str:
    res = await mcp.server.call_tool(name, args)
    return res.content[0].text


def test_tools_registered(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))

    async def run():
        tools = await mcp.server.list_tools()
        names = {t.name for t in tools}
        return names

    names = _run(run())
    expected = {
        "list_hosts",
        "get_host",
        "ping",
        "ping_all",
        "list_sessions",
        "create_session",
        "session_details",
        "rename_session",
        "kill_session",
        "kill_server",
        "detach_clients",
        "list_windows",
        "new_window",
        "rename_window",
        "kill_window",
        "capture_pane",
        "send_keys",
        "send_enter",
        "send_raw",
        "run_command",
        "run_command_many",
        "list_snippets",
        "add_snippet",
        "remove_snippet",
        "broadcast",
        "list_tunnels",
        "start_tunnel",
        "stop_tunnel",
        "stop_tunnels",
        "list_audit_logs",
        "read_audit_log",
        "sftp_download",
        "sftp_upload",
        "transfer_file",
    }
    assert expected <= names


def test_list_and_get_host(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "list_hosts", {}))
    assert "alpha" in out and "loc" in out
    out = _run(_call(mcp, "get_host", {"alias": "alpha"}))
    assert "192.0.2.1" in out and "alice" in out


def test_get_host_missing(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    with pytest.raises(Exception):
        _run(_call(mcp, "get_host", {"alias": "nonexistent"}))


def test_snippet_roundtrip(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    assert (
        _run(_call(mcp, "add_snippet", {"name": "up", "command": "uptime"}))
        == "snippet 'up' salvato"
    )
    out = _run(_call(mcp, "list_snippets", {}))
    assert '"name": "up"' in out
    assert _run(_call(mcp, "remove_snippet", {"name": "up"})) == "snippet 'up' rimosso"
    out = _run(_call(mcp, "list_snippets", {}))
    assert "up" not in out


def test_ping(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import adapter

    monkeypatch.setattr(adapter, "tcp_ping", lambda host, timeout=2.0: (True, "porta 22 aperta"))
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "ping", {"alias": "alpha"}))
    assert "OK" in out


def test_create_and_kill_session(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import adapter

    calls: list[str] = []

    def fake_run_action(host, command, cfg=None, timeout=20):
        calls.append(command)
        return type("R", (), {"ok": True, "stdout": "", "stderr": ""})

    monkeypatch.setattr(adapter, "run_tmux_action", fake_run_action)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "create_session", {"alias": "alpha", "name": "sess"}))
    assert "sess" in out
    assert any("tmux new -d -s 'sess'" in c for c in calls)
    out = _run(_call(mcp, "kill_session", {"alias": "alpha", "name": "sess"}))
    assert "terminata" in out


def test_run_command_local(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import broadcast as bcast

    def fake_run(args, **kwargs):
        return type("P", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

    monkeypatch.setattr(bcast.subprocess, "run", fake_run)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "run_command", {"alias": "loc", "command": "echo ok"}))
    assert "ok" in out and '"exit_code": 0' in out


def test_broadcast_tmux_mocked(monkeypatch, tmp_path):
    from bravoric_ssh_client import mcp_server

    class FakeResult:
        def __init__(self, alias):
            self.host_alias = alias
            self.ok = True
            self.exit_code = 0
            self.stdout = ""
            self.stderr = ""
            self.error = ""
            self.session_name = f"bcast-x-{alias}-ts"

    monkeypatch.setattr(
        mcp_server,
        "run_snippet_on_hosts_tmux",
        lambda hosts, cmd, snippet, cfg, timeout=60, max_workers=8: [
            FakeResult(h.alias) for h in hosts
        ],
    )
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(mcp, "broadcast", {"aliases": ["alpha", "loc"], "command": "uptime", "mode": "tmux"})
    )
    assert '"session_name": "bcast-x-alpha-ts"' in out


def test_read_audit_log_ok(tmp_path):
    cfg = make_cfg(tmp_path)
    logs_dir = cfg.path.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    gz = logs_dir / "20260910_alpha_sess.log.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write("riga1\nriga2\n")
    mcp = BravoricMcp(config=cfg)
    out = _run(_call(mcp, "list_audit_logs", {}))
    assert "20260910_alpha_sess.log.gz" in out
    out = _run(
        _call(mcp, "read_audit_log", {"filename": "20260910_alpha_sess.log.gz", "max_lines": 1})
    )
    assert "riga2" in out and "riga1" not in out


def test_read_audit_log_rejects_traversal(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "read_audit_log", {"filename": "../../etc/passwd"}))
    assert "non consentito" in out


def test_get_status(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "get_status", {}))
    assert '"transport": "stdio"' in out
    assert '"hosts": 2' in out
    assert "config_path" in out


def test_tmux_present(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import adapter

    monkeypatch.setattr(adapter, "tmux_present", lambda host, cfg=None: True)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "tmux_present", {"alias": "loc"}))
    assert "presente" in out


def test_list_remote_audit_logs(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import broadcast as bcast

    class Res:
        host_alias = "alpha"
        ok = True
        exit_code = 0
        stdout = "20260910_alpha_sess.log.gz\n20260910_alpha_sh.log.gz\n"
        stderr = ""
        error = ""

    monkeypatch.setattr(bcast, "run_snippet_on_host", lambda h, cmd, cfg, timeout=60: Res())
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "list_remote_audit_logs", {"alias": "alpha"}))
    assert "20260910_alpha_sess.log.gz" in out


def test_read_remote_audit_log(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import broadcast as bcast

    class Res:
        host_alias = "alpha"
        ok = True
        exit_code = 0
        stdout = "riga1\nriga2\n"
        stderr = ""
        error = ""

    calls: list[str] = []

    def fake(host, cmd, cfg, timeout=60):
        calls.append(cmd)
        return Res()

    monkeypatch.setattr(bcast, "run_snippet_on_host", fake)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(mcp, "read_remote_audit_log", {"alias": "alpha", "filename": "20260910_a.log.gz"})
    )
    assert "riga2" in out
    assert "gzip -cd" in calls[0]
    # max_lines aggiunge il tail
    _run(
        _call(
            mcp,
            "read_remote_audit_log",
            {"alias": "alpha", "filename": "20260910_a.log.gz", "max_lines": 5},
        )
    )
    assert "tail -n 5" in calls[1]


def test_read_remote_audit_log_rejects_bad_name(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(mcp, "read_remote_audit_log", {"alias": "alpha", "filename": "../../etc/passwd"})
    )
    assert "non consentito" in out


# ---------- dashboard / panoramica ----------


class FakeSessions:
    def __init__(self, sessions, ok=True, error=""):
        self.sessions = sessions
        self.ok = ok
        self.error = error


def test_hosts_summary(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import adapter

    monkeypatch.setattr(adapter, "tcp_ping", lambda host, timeout=2.0: (True, "ok"))
    monkeypatch.setattr(adapter, "tmux_present", lambda host, cfg=None: True)
    monkeypatch.setattr(
        adapter, "list_tmux_sessions", lambda host, cfg=None: FakeSessions(["s1", "s2"])
    )
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "hosts_summary", {}))
    assert '"reachable": true' in out
    assert '"session_count": 2' in out


def test_list_sessions_all(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import adapter

    def fake_list(host, cfg=None):
        return (
            FakeSessions([host.alias + "-s"])
            if host.is_local()
            else FakeSessions([], ok=False, error="down")
        )

    monkeypatch.setattr(adapter, "list_tmux_sessions", fake_list)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "list_sessions_all", {}))
    assert '"alias": "loc"' in out
    assert '"ok": true' in out
    assert '"alias": "alpha"' in out


def test_find_in_sessions(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import adapter

    monkeypatch.setattr(adapter, "list_tmux_sessions", lambda host, cfg=None: FakeSessions(["s1"]))
    monkeypatch.setattr(
        adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200: type(
            "R", (), {"ok": True, "stdout": "riga1\nciao mondo\nriga3"}
        ),  # type: ignore[call-arg]
    )
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "find_in_sessions", {"alias": "loc", "pattern": "ciao"}))
    assert '"session": "s1"' in out
    assert "ciao mondo" in out
    # pattern invalido
    out = _run(_call(mcp, "find_in_sessions", {"alias": "loc", "pattern": "(["}))
    assert "non valido" in out


def test_run_command_all(monkeypatch, tmp_path):
    from bravoric_ssh_client import mcp_server

    class FakeRes:
        def __init__(self, alias):
            self.host_alias = alias
            self.ok = True
            self.exit_code = 0
            self.stdout = "out\n"
            self.stderr = ""
            self.error = ""

    monkeypatch.setattr(
        mcp_server,
        "run_snippet_on_hosts",
        lambda h, c, cfg, timeout=60: [FakeRes(x.alias) for x in h],
    )
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "run_command_all", {"command": "uptime"}))
    assert '"ok": true' in out


def test_run_and_wait(monkeypatch, tmp_path):
    from bravoric_ssh_client import mcp_server

    monkeypatch.setattr(
        mcp_server, "_wait_run_and_read", lambda self, h, cmd, t, p: (True, "C1\nC2", "")
    )
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(mcp, "run_and_wait", {"alias": "loc", "command": "echo C1; echo C2", "timeout": 30})
    )
    assert "C1\nC2" in out


def test_broadcast_wait(monkeypatch, tmp_path):
    from bravoric_ssh_client import mcp_server

    monkeypatch.setattr(
        mcp_server, "_wait_run_and_read", lambda self, h, cmd, t, p: (True, "OK", "")
    )
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(mcp, "broadcast_wait", {"aliases": ["loc"], "command": "uptime", "timeout": 30})
    )
    assert '"ok": true' in out
    assert '"output": "OK"' in out


def test_list_tunnels_all(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.hosts.append(
        Host(
            alias="beta2",
            host="192.0.2.9",
            user="b",
            auth="key",
            tunnels=[Tunnel(kind="L", local_port=5433, remote_host="db", remote_port=5433)],
        )
    )
    mcp = BravoricMcp(config=cfg)
    out = _run(_call(mcp, "list_tunnels_all", {}))
    assert '"alias": "beta2"' in out  # solo host con tunnel configurati


def test_tunnel_health(monkeypatch, tmp_path):
    from bravoric_ssh_client.config import Tunnel
    from bravoric_ssh_client.ssh.tunnels import TunnelProcess

    fake = TunnelProcess(spec=Tunnel(kind="D", local_port=1080), port=1080, pid=1)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    monkeypatch.setattr(mcp.tunnels, "active", lambda alias: [fake])

    # porta non in ascolto -> listening false
    out = _run(_call(mcp, "tunnel_health", {"alias": "loc"}))
    assert '"listening": false' in out
    assert '"kind": "D"' in out
    # tunnel R -> n/a
    fake_r = TunnelProcess(
        spec=Tunnel(kind="R", local_port=8080, remote_host="w", remote_port=80), port=8080, pid=2
    )
    monkeypatch.setattr(mcp.tunnels, "active", lambda alias: [fake_r])
    out = _run(_call(mcp, "tunnel_health", {"alias": "loc"}))
    assert "porta remota" in out


def test_session_history(monkeypatch, tmp_path):
    from bravoric_ssh_client import history

    class FakeEntry:
        host = "alpha"
        session = "sess"

    monkeypatch.setattr(history, "load_history", lambda cfg: [FakeEntry()])
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "session_history", {}))
    assert '"host": "alpha"' in out
    assert '"session": "sess"' in out


def test_rotations_roundtrip(tmp_path):
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(_call(mcp, "add_rotation", {"name": "prod", "entries": ["alpha/s1", "alpha/s2"]}))
    assert "salvata" in out
    out = _run(_call(mcp, "list_rotations", {}))
    assert '"name": "prod"' in out
    assert '"host": "alpha"' in out
    out = _run(_call(mcp, "remove_rotation", {"name": "prod"}))
    assert "rimossa" in out
    out = _run(_call(mcp, "list_rotations", {}))
    assert "prod" not in out


# ---------- trasferimento file ----------


def test_sftp_download(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import file_ops

    def fake_download(host, remote, local, password, **kw):
        Path(local).write_text("hello\n")
        return file_ops.ScpResult(ok=True)

    monkeypatch.setattr(file_ops, "download", fake_download)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(
            mcp,
            "sftp_download",
            {"alias": "alpha", "remote": "/r/x.zip", "local": str(tmp_path / "x.zip")},
        )
    )
    assert '"ok": true' in out
    assert '"host": "alpha"' in out
    assert (tmp_path / "x.zip").read_text() == "hello\n"


def test_sftp_upload(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import file_ops

    local = tmp_path / "x.zip"
    local.write_bytes(b"data")
    seen: dict[str, str] = {}

    def fake_upload(host, local, remote, password, **kw):
        seen["remote"] = remote
        return file_ops.ScpResult(ok=True)

    monkeypatch.setattr(file_ops, "upload", fake_upload)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(
            mcp,
            "sftp_upload",
            {"alias": "alpha", "local": str(local), "remote": "/r/x.zip"},
        )
    )
    assert '"ok": true' in out
    assert seen["remote"] == "/r/x.zip"


def test_transfer_file(monkeypatch, tmp_path):
    import hashlib

    from bravoric_ssh_client.ssh import file_ops

    payload = b"archive-payload-test"
    uploaded: dict[str, object] = {}

    def fake_download(host, remote, local, password, **kw):
        Path(local).write_bytes(payload)
        return file_ops.ScpResult(ok=True)

    def fake_upload(host, local, remote, password, **kw):
        uploaded["data"] = Path(local).read_bytes()
        uploaded["remote"] = remote
        return file_ops.ScpResult(ok=True)

    monkeypatch.setattr(file_ops, "download", fake_download)
    monkeypatch.setattr(file_ops, "upload", fake_upload)
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(
            mcp,
            "transfer_file",
            {
                "src_alias": "alpha",
                "src_remote": "/r/src.zip",
                "dst_alias": "alpha",
                "dst_remote": "/r/dst.zip",
            },
        )
    )
    assert uploaded["data"] == payload
    assert uploaded["remote"] == "/r/dst.zip"
    assert '"ok": true' in out
    assert f'"md5": "{hashlib.md5(payload).hexdigest()}"' in out


def test_transfer_file_download_failure(monkeypatch, tmp_path):
    from bravoric_ssh_client.ssh import file_ops

    monkeypatch.setattr(
        file_ops,
        "download",
        lambda host, remote, local, password, **kw: file_ops.ScpResult(ok=False, stderr="no route"),
    )
    mcp = BravoricMcp(config=make_cfg(tmp_path))
    out = _run(
        _call(
            mcp,
            "transfer_file",
            {
                "src_alias": "alpha",
                "src_remote": "/r/src.zip",
                "dst_alias": "alpha",
                "dst_remote": "/r/dst.zip",
            },
        )
    )
    assert '"ok": false' in out
    assert '"stage": "download"' in out
