"""Test dei flag CLI JSON per la GUI (stdout JSON nudo, errori su stderr + exit != 0).

Coprono i comandi che non richiedono rete: --status, --snippet-list, --audit-list,
--host-info, --list-hosts, --session-history, --rotation-list, --tunnel-list e la
gestione dei flag sconosciuti.
"""

from __future__ import annotations

import json

import pytest

from bravoric_ssh_client.app import main
from bravoric_ssh_client.config import Config, Host, save_config


def _write_config(tmp_path) -> str:
    cfg = Config()
    cfg.hosts = [
        Host(alias="alpha", host="192.0.2.1", user="alice", auth="key"),
        Host(alias="beta", host="192.0.2.2", user="bob", auth="key"),
    ]
    path = tmp_path / "config.toml"
    cfg.path = path
    save_config(cfg, path)
    return str(path)


def _run(capsys, argv):
    main(argv)
    out = capsys.readouterr()
    return out


def test_status_emits_json(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--status"])
    data = json.loads(out.out)
    assert data["provider"] == "keyring"
    assert data["hosts"] == 2
    assert "config_path" in data


def test_list_hosts_emits_naked_array(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--list-hosts"])
    data = json.loads(out.out)
    assert isinstance(data, list)
    assert {h["alias"] for h in data} == {"alpha", "beta"}


def test_host_info_emits_json(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--host-info", "alpha"])
    data = json.loads(out.out)
    assert data["alias"] == "alpha"
    assert data["host"] == "192.0.2.1"
    assert data["tunnels"] == []


def test_host_info_unknown_host_exits_nonzero(tmp_path, capsys):
    path = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["-c", path, "--host-info", "nope"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "nope" in err


def test_snippet_list_emits_array(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--snippet-list"])
    data = json.loads(out.out)
    assert isinstance(data, list)


def test_audit_list_emits_array(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--audit-list"])
    data = json.loads(out.out)
    assert isinstance(data, list)


def test_session_history_emits_array(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--session-history"])
    data = json.loads(out.out)
    assert isinstance(data, list)


def test_rotation_list_emits_array(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--rotation-list"])
    data = json.loads(out.out)
    assert isinstance(data, list)


def test_tunnel_list_emits_array(tmp_path, capsys):
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--tunnel-list"])
    data = json.loads(out.out)
    assert isinstance(data, list)


def test_unknown_flag_exits_nonzero_without_tui(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--totally-unknown-flag"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "sconosciuta" in err.lower()


def test_snippet_add_and_remove(tmp_path, capsys):
    path = _write_config(tmp_path)
    main(["-c", path, "--snippet-add", "greet", "echo hi", "--description", "saluto"])
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "greet"
    assert data["added"] is True

    main(["-c", path, "--snippet-list"])
    listed = json.loads(capsys.readouterr().out)
    assert any(s["name"] == "greet" for s in listed)

    main(["-c", path, "--snippet-remove", "greet"])
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] is True


def test_ping_emits_json(tmp_path, capsys, monkeypatch):
    from bravoric_ssh_client.ssh import adapter

    monkeypatch.setattr(adapter, "tcp_ping", lambda host, timeout=2.0: (True, "ok-finto"))
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--ping", "alpha"])
    data = json.loads(out.out)
    assert data == {"ok": True, "detail": "ok-finto"}


def test_ping_failure_exits_nonzero(tmp_path, capsys, monkeypatch):
    from bravoric_ssh_client.ssh import adapter

    monkeypatch.setattr(adapter, "tcp_ping", lambda host, timeout=2.0: (False, "ko-finto"))
    path = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["-c", path, "--ping", "alpha"])
    assert exc.value.code != 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"ok": False, "detail": "ko-finto"}


def test_list_services_emits_json(tmp_path, capsys, monkeypatch):
    from bravoric_ssh_client.ssh import adapter

    def fake(host, command, cfg=None, *, timeout=20):
        return adapter.TmuxActionResult(ok=True, stdout="a.service\nb.service\n", stderr="")

    monkeypatch.setattr(adapter, "run_tmux_action", fake)
    path = _write_config(tmp_path)
    out = _run(capsys, ["-c", path, "--list-services", "alpha"])
    data = json.loads(out.out)
    assert data["services"] == ["a.service", "b.service"]
