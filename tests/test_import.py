"""Test dell'import da ~/.ssh/config e da Remmina."""

from __future__ import annotations

from pathlib import Path

from bravoric_ssh_client.config import Config, Host, load_config, save_config
from bravoric_ssh_client.importers import (
    entries_to_hosts,
    import_from_remmina,
    import_from_ssh_config,
    parse_remmina_file,
    parse_ssh_config,
)


def test_parse_simple(tmp_path: Path):
    ssh = tmp_path / "ssh_config"
    ssh.write_text(
        "Host alpha\n"
        "    HostName 10.0.0.1\n"
        "    User alice\n"
        "    Port 2222\n"
        "\n"
        "Host *\n"
        "    User ignored\n",
        encoding="utf-8",
    )
    entries = parse_ssh_config(ssh)
    assert len(entries) == 1
    e = entries[0]
    assert e.alias == "alpha"
    assert e.host == "10.0.0.1"
    assert e.user == "alice"
    assert e.port == 2222


def test_parse_defaults_and_wildcards(tmp_path: Path):
    ssh = tmp_path / "ssh_config"
    ssh.write_text(
        "Host beta\n    HostName beta.example.com\nHost gamma?\n    HostName 10.1.1.1\n",
        encoding="utf-8",
    )
    entries = parse_ssh_config(ssh)
    assert [e.alias for e in entries] == ["beta"]
    assert entries[0].host == "beta.example.com"
    assert entries[0].port == 22


def test_entries_to_hosts():
    from bravoric_ssh_client.importers import SshEntry

    hosts = entries_to_hosts([SshEntry("a", "10.0.0.1", "root", 22)], default_auth="keyring")
    assert len(hosts) == 1
    assert hosts[0].alias == "a"
    assert hosts[0].auth == "keyring"


def test_import_creates_config(tmp_path: Path):
    ssh = tmp_path / "ssh_config"
    ssh.write_text("Host alpha\n    HostName 10.0.0.1\n    User alice\n", encoding="utf-8")
    out = tmp_path / "out" / "config.toml"
    total, added = import_from_ssh_config(ssh, out, merge=False)
    assert total == 1 and added == 1
    cfg = load_config(out)
    assert len(cfg.hosts) == 1
    assert cfg.hosts[0].alias == "alpha"


def test_import_merge_no_duplicates(tmp_path: Path):
    ssh = tmp_path / "ssh_config"
    ssh.write_text(
        "Host alpha\n    HostName 10.0.0.1\n    User alice\n"
        "Host beta\n    HostName 10.0.0.2\n    User bob\n",
        encoding="utf-8",
    )
    out = tmp_path / "out" / "config.toml"
    # config iniziale con solo alpha
    from bravoric_ssh_client.config import Host, save_config

    save_config(Config(hosts=[Host(alias="alpha", host="10.0.0.1", user="alice")]), out)
    # import con merge: alpha già presente, aggiunge solo beta
    total, added = import_from_ssh_config(ssh, out, merge=True)
    cfg = load_config(out)
    assert len(cfg.hosts) == 2
    assert {h.alias for h in cfg.hosts} == {"alpha", "beta"}
    assert added == 1


def test_import_exclude(tmp_path: Path):
    ssh = tmp_path / "ssh_config"
    ssh.write_text(
        "Host alpha\n    HostName 10.0.0.1\nHost beta\n    HostName 10.0.0.2\n",
        encoding="utf-8",
    )
    out = tmp_path / "out" / "config.toml"
    total, _ = import_from_ssh_config(ssh, out, merge=False, exclude={"alpha"})
    assert total == 1
    cfg = load_config(out)
    assert cfg.hosts[0].alias == "beta"


# ---------- Import da Remmina ----------

REM_SAMPLE = """
[remmina]
name=Webprod
protocol=SSH
server=203.0.113.10
username=deploy
port=2222
password=.
ssh_privatekey=
"""

REM_RDP = """
[remmina]
name=Windows
protocol=RDP
server=192.168.1.6
username=user
password=.
"""


def test_parse_remmina_file(tmp_path: Path):
    f = tmp_path / "webprod.remmina"
    f.write_text(REM_SAMPLE, encoding="utf-8")
    e = parse_remmina_file(f)
    assert e is not None
    assert e.alias == "webprod"
    assert e.host == "203.0.113.10"
    assert e.user == "deploy"
    assert e.port == 2222
    assert e.auth == "keyring"


def test_parse_remmina_skips_non_ssh(tmp_path: Path):
    f = tmp_path / "win.remmina"
    f.write_text(REM_RDP, encoding="utf-8")
    assert parse_remmina_file(f) is None


def test_import_from_remmina_merge(tmp_path: Path, monkeypatch):
    remdir = tmp_path / "remmina"
    remdir.mkdir()
    (remdir / "webprod.remmina").write_text(REM_SAMPLE, encoding="utf-8")
    monkeypatch.setattr("bravoric_ssh_client.importers.remmina_dirs", lambda: [remdir])
    out = tmp_path / "config.toml"
    save_config(Config(hosts=[Host(alias="esistente", host="10.0.0.1")]), out)
    total, added = import_from_remmina(out, merge=True)
    assert total == 1 and added == 1
    cfg = load_config(out)
    assert cfg.host("webprod") is not None
    # secondo import: nessun duplicato
    total2, added2 = import_from_remmina(out, merge=True)
    assert added2 == 0
    cfg2 = load_config(out)
    assert len([h for h in cfg2.hosts if h.alias == "webprod"]) == 1
