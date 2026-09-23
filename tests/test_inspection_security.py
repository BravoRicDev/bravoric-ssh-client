"""Regressioni per i fix di sicurezza/robustezza delle operazioni remote.

Copre:
- remote_edit_file senza shell (nessuna injection via path/pattern);
- replace_block con MAX_FILE_SIZE definito nello script remoto;
- remote_project_tree con script valido;
- validazione del bind dei tunnel (solo loopback);
- default di audit_log attivo quando la chiave manca.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bravoric_ssh_client.config import ConfigError, Host, load_config
from bravoric_ssh_client.ssh.inspection import (
    remote_edit_file,
    remote_project_tree,
    replace_block,
)


def test_remote_edit_file_local(tmp_path: Path):
    host = Host(alias="loc", host="localhost", local=True)
    f = tmp_path / "edit.txt"
    f.write_text("hello world\nfoo hello\n")

    res = remote_edit_file(host, None, path=str(f), pattern="hello", replacement="HI")

    assert res["ok"] is True
    assert res["data"]["replacements"] == 2
    assert f.read_text() == "HI world\nfoo HI\n"


def test_remote_edit_file_count_limit(tmp_path: Path):
    host = Host(alias="loc", host="localhost", local=True)
    f = tmp_path / "edit2.txt"
    f.write_text("a a a\n")

    res = remote_edit_file(host, None, path=str(f), pattern="a", replacement="b", count=1)

    assert res["ok"] is True
    assert f.read_text() == "b a a\n"


def test_remote_edit_file_path_injection_not_executed(tmp_path: Path):
    host = Host(alias="loc", host="localhost", local=True)
    marker = tmp_path / "PWNED"
    evil_path = f"{tmp_path}/x'; touch {marker}; '"

    res = remote_edit_file(host, None, path=evil_path, pattern="a", replacement="b")

    assert res["ok"] is False
    assert not marker.exists(), "shell injection: il file marker è stato creato"


def test_replace_block_local(tmp_path: Path):
    host = Host(alias="loc", host="localhost", local=True)
    f = tmp_path / "block.txt"
    f.write_text("AAA\nBBB\nCCC\n")

    res = replace_block(host, None, path=str(f), old_text="BBB", new_text="XXX")

    assert res["ok"] is True
    assert "XXX" in f.read_text()
    assert "BBB" not in f.read_text()


def test_remote_project_tree_local(tmp_path: Path):
    host = Host(alias="loc", host="localhost", local=True)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("x")

    res = remote_project_tree(host, None, path=str(tmp_path), max_depth=2)

    assert res["ok"] is True
    tree = res["data"]["tree"]
    assert "sub" in tree
    assert tree["sub"]["a.txt"] == "file"


def test_tunnel_bind_must_be_loopback(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[hosts]]\n'
        'alias = "h1"\n'
        'host = "example.com"\n'
        '\n'
        '[[hosts.tunnels]]\n'
        'kind = "L"\n'
        'local_port = 9000\n'
        'remote_host = "127.0.0.1"\n'
        'remote_port = 80\n'
        'bind = "0.0.0.0"\n'
    )
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_tunnel_bind_loopback_accepted(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[hosts]]\n'
        'alias = "h1"\n'
        'host = "example.com"\n'
        '\n'
        '[[hosts.tunnels]]\n'
        'kind = "L"\n'
        'local_port = 9000\n'
        'remote_host = "127.0.0.1"\n'
        'remote_port = 80\n'
        'bind = "127.0.0.1"\n'
    )
    loaded = load_config(cfg)
    assert loaded.hosts[0].tunnels[0].bind == "127.0.0.1"


def test_audit_log_defaults_true_when_missing(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[hosts]]\nalias = "h1"\nhost = "example.com"\n')
    loaded = load_config(cfg)
    assert loaded.audit_log is True


def test_remote_host_network_ports_local():
    """Lo script remoto deve essere Python valido e ritornare una lista."""
    from bravoric_ssh_client.ssh.inspection import remote_host_network_ports

    host = Host(alias="loc", host="localhost", local=True)
    res = remote_host_network_ports(host, None)
    assert res["ok"] is True, res
    assert isinstance(res["data"]["ports"], list)


def test_remote_read_service_logs_local():
    """read_service_logs non deve generare script con errori di sintassi."""
    from bravoric_ssh_client.ssh.inspection import remote_read_service_logs

    host = Host(alias="loc", host="localhost", local=True)
    res = remote_read_service_logs(host, None, name="systemd-journald", lines=5)
    # Su un host locale senza journald può ritornare ok=False con errore, ma
    # NON deve mai essere un errore di sintassi/indentazione dello script.
    assert res["ok"] is True or "Syntax" not in str(res.get("error", "")), res
    assert isinstance(res.get("data"), dict), res
