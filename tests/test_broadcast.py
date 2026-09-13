"""Test snippet catalog e broadcast (esecuzione in parallelo su più host)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.snippets import Snippet, add_snippet, load_snippets, remove_snippet
from bravoric_ssh_client.ssh import broadcast
from bravoric_ssh_client.ssh.adapter import SshConfig

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shell/exec paths")

# ---------- Catalogo snippet ----------


def test_snippet_roundtrip(tmp_path: Path):
    cfg = Config(path=tmp_path / "config.toml", snippets_file=str(tmp_path / "snip.json"))
    add_snippet(cfg, Snippet("uptime", "uptime", "tempo acceso"))
    add_snippet(cfg, Snippet("df", "df -h"))
    items = load_snippets(cfg)
    assert len(items) == 2
    assert items[0].name == "df"  # più recente in testa
    assert items[0].command == "df -h"
    remove_snippet(cfg, "df")
    items = load_snippets(cfg)
    assert [s.name for s in items] == ["uptime"]


def test_load_snippets_missing(tmp_path: Path):
    cfg = Config(path=tmp_path / "config.toml")
    assert load_snippets(cfg) == []


def test_add_snippet_overwrites(tmp_path: Path):
    cfg = Config(path=tmp_path / "config.toml", snippets_file=str(tmp_path / "s.json"))
    add_snippet(cfg, Snippet("x", "old"))
    add_snippet(cfg, Snippet("x", "new"))
    items = load_snippets(cfg)
    assert len(items) == 1
    assert items[0].command == "new"


# ---------- Broadcast ----------


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def local_host() -> Host:
    return Host(alias="loc", host="localhost", local=True)


@pytest.fixture
def remote_host() -> Host:
    return Host(alias="srv", host="192.0.2.20", user="root", auth="keyring")


def test_run_snippet_local(monkeypatch, local_host):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return FakeCompleted(0, "ok\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = broadcast.run_snippet_on_host(local_host, "echo ok")
    assert res.ok
    assert res.host_alias == "loc"
    assert res.stdout.strip() == "ok"
    assert "/bin/sh" in calls[0][0]


def test_run_snippet_remote(monkeypatch, remote_host):
    attempts: list[str] = []

    def fake_run(args, **kwargs):
        env = kwargs.get("env", {})
        attempts.append("askpass" if env.get("SSH_ASKPASS_REQUIRE") == "force" else "batch")
        if env.get("SSH_ASKPASS_REQUIRE") == "force":
            return FakeCompleted(0, "out\n")
        return FakeCompleted(255, "", "denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = SshConfig(password_provider=lambda h: "pw")
    res = broadcast.run_snippet_on_host(remote_host, "uptime", cfg)
    assert res.ok
    assert res.stdout.strip() == "out"
    assert attempts == ["batch", "askpass"]


def test_run_snippet_on_hosts_parallel(monkeypatch):
    """Esegue su più host in parallelo e ordina i risultati per alias."""
    h1 = Host(alias="b", host="192.0.2.1", user="root", auth="key")
    h2 = Host(alias="a", host="192.0.2.2", user="root", auth="key")
    h3 = Host(alias="loc", host="localhost", local=True)
    calls: list[str] = []

    def fake_run(args, **kwargs):
        calls.append(" ".join(args))
        return FakeCompleted(0, "ok\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = broadcast.run_snippet_on_hosts([h1, h2, h3], "uptime")
    assert len(results) == 3
    aliases = [r.host_alias for r in results]
    assert aliases == ["a", "b", "loc"]  # ordinati


def test_run_snippet_on_hosts_tolerates_error(monkeypatch):
    h = Host(alias="a", host="192.0.2.1", user="root", auth="key")

    def fake_run(args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = broadcast.run_snippet_on_hosts([h], "x")
    assert len(results) == 1
    assert not results[0].ok
    assert "boom" in results[0].error


# ---------- Broadcast in sessione tmux ----------


def test_session_slug_shape():
    slug = broadcast.session_slug("df -h", "srv")
    assert slug.startswith("bcast-df-h-srv-")
    # caratteri pericolosi ripuliti
    slug2 = broadcast.session_slug("a:b c", "h:o")
    assert ":" not in slug2
    assert " " not in slug2
    assert len(slug2) <= 60


def test_session_slug_empty_fallback():
    slug = broadcast.session_slug("", "")
    assert slug.startswith("bcast-")
    assert slug.count("-") >= 3


def test_run_snippet_in_tmux_launches(monkeypatch, local_host):
    """Con tmux presente lancia una sessione detached e ritorna il nome."""
    calls: list[str] = []

    def fake_tmux_present(host, cfg=None):
        return True

    def fake_run_action(host, command, cfg=None, timeout=20):
        calls.append(command)
        return type("R", (), {"ok": True, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(broadcast.adapter, "tmux_present", fake_tmux_present)
    monkeypatch.setattr(broadcast.adapter, "run_tmux_action", fake_run_action)
    res = broadcast.run_snippet_in_tmux(local_host, "uptime", "bcast-up-loc-ts")
    assert res.ok
    assert res.session_name == "bcast-up-loc-ts"
    assert "tmux new -d -s 'bcast-up-loc-ts' 'uptime'" in calls[0]


def test_run_snippet_in_tmux_fallback_without_tmux(monkeypatch, local_host):
    """Senza tmux ripiega sulla modalità diretta (session_name vuoto)."""
    calls: list[list[str]] = []

    def fake_tmux_present(host, cfg=None):
        return False

    def fake_run(args, **kwargs):
        calls.append(args)
        return FakeCompleted(0, "out\n")

    monkeypatch.setattr(broadcast.adapter, "tmux_present", fake_tmux_present)
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = broadcast.run_snippet_in_tmux(local_host, "echo hi", "bcast-x")
    assert res.ok
    assert res.session_name == ""
    assert res.stdout.strip() == "out"


def test_run_snippet_in_tmux_error_falls_back(monkeypatch, local_host):
    """Errore nella creazione tmux → fallback diretto con l'errore."""

    def fake_tmux_present(host, cfg=None):
        return True

    def fake_run_action(host, command, cfg=None, timeout=20):
        return type("R", (), {"ok": False, "stdout": "", "stderr": "tmux: no server"})

    def fake_run(args, **kwargs):
        return FakeCompleted(0, "fallback\n")

    monkeypatch.setattr(broadcast.adapter, "tmux_present", fake_tmux_present)
    monkeypatch.setattr(broadcast.adapter, "run_tmux_action", fake_run_action)
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = broadcast.run_snippet_in_tmux(local_host, "echo hi", "bcast-x")
    assert res.ok
    assert res.session_name == ""
    assert res.stdout.strip() == "fallback"


def test_run_snippet_on_hosts_tmux_parallel(monkeypatch):
    h1 = Host(alias="b", host="192.0.2.1", user="root", auth="key")
    h2 = Host(alias="a", host="192.0.2.2", user="root", auth="key")

    def fake_tmux_present(host, cfg=None):
        return True

    def fake_run_action(host, command, cfg=None, timeout=20):
        return type("R", (), {"ok": True, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(broadcast.adapter, "tmux_present", fake_tmux_present)
    monkeypatch.setattr(broadcast.adapter, "run_tmux_action", fake_run_action)
    results = broadcast.run_snippet_on_hosts_tmux([h1, h2], "uptime", "up")
    assert [r.host_alias for r in results] == ["a", "b"]
    for r in results:
        assert r.ok
        assert r.session_name.startswith("bcast-up-")
        assert r.session_name.count("-") >= 4  # bcast-snippet-host-ts
