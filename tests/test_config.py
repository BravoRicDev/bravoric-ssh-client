"""Test del parsing config e dei provider credenziali (senza rete)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bravoric_ssh_client.config import (
    ConfigError,
    Host,
    _parse_host,
    load_config,
)
from bravoric_ssh_client.credentials.factory import resolve_password
from bravoric_ssh_client.credentials.plain import PlainProvider


@pytest.fixture
def sample_toml(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(
        """
[credentials]
provider = "plain"
plain_file = "secrets.tsv"

[defaults]
port = 2222

[[hosts]]
alias = "alpha"
host = "10.0.0.1"
user = "alice"

[[hosts]]
alias = "beta"
host = "10.0.0.2"
auth = "key"
""",
        encoding="utf-8",
    )
    return p


def test_load_basic(sample_toml: Path):
    cfg = load_config(sample_toml)
    assert cfg.credential_provider == "plain"
    assert cfg.plain_file == "secrets.tsv"
    assert len(cfg.hosts) == 2
    alpha = cfg.host("alpha")
    assert alpha is not None
    assert alpha.host == "10.0.0.1"
    assert alpha.port == 2222  # da defaults
    assert alpha.effective_cred_key() == "alice@10.0.0.1"
    beta = cfg.host("beta")
    assert beta is not None
    assert beta.auth == "key"


def test_invalid_auth(tmp_path: Path):
    p = tmp_path / "bad.toml"
    p.write_text('[[hosts]]\nalias="x"\nhost="y"\nauth="nope"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_plain_provider_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "bravoric_ssh_client.credentials.plain.default_config_dir", lambda: tmp_path
    )
    secrets = tmp_path / "bravoric-ssh-client" / "secrets.tsv"
    provider = PlainProvider(secrets)
    host = Host(alias="alpha", host="10.0.0.1", user="alice")
    assert provider.get(host) is None
    provider.set(host, "s3cret!")
    assert provider.get(host) == "s3cret!"
    assert secrets.exists()
    if hasattr(secrets, "stat"):
        assert (secrets.stat().st_mode & 0o777) == 0o600
    provider.delete(host)
    assert provider.get(host) is None


def test_resolve_password_plain(sample_toml: Path, tmp_path: Path):
    cfg = load_config(sample_toml)
    # il provider 'plain' di default punta sotto config dir; scriviamo direttamente il file
    secrets = tmp_path / "bravoric-ssh-client" / "secrets.tsv"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    secrets.write_text("alice@10.0.0.1\tpwalpha\n", encoding="utf-8")
    # per il test forziamo cfg.plain_file sul path assoluto
    cfg.plain_file = str(secrets)
    alpha = cfg.host("alpha")
    assert alpha is not None
    assert resolve_password(cfg, alpha) == "pwalpha"
    beta = cfg.host("beta")
    assert beta is not None  # auth=key -> nessuna password
    assert resolve_password(cfg, beta) is None


def test_parse_errors():
    with pytest.raises(ConfigError):
        _parse_host("  ", {}, {})
    with pytest.raises(ConfigError):
        _parse_host("x", {"host": "1.2.3.4"}, {"port": "abc"})
