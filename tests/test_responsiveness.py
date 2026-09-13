"""Test della reattività della TUI: le operazioni I/O non bloccano l'event loop."""

from __future__ import annotations

import asyncio
import time

from bravoric_ssh_client.app import BravoricApp, HostScreen, SessionScreen
from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.ssh import adapter as ssh_adapter


def make_cfg() -> Config:
    cfg = Config()
    cfg.hosts = [Host(alias="alpha", host="10.0.0.1", user="a", auth="key")]
    return cfg


class SlowRes:
    ok = True
    sessions = ["s1", "s2"]
    error = ""


def test_listing_does_not_block_navigation(monkeypatch):
    """Con un listing lento (3s), si deve poter tornare indietro prima che finisca."""
    monkeypatch.setattr(
        ssh_adapter, "list_tmux_sessions", lambda host, cfg=None: time.sleep(3) or SlowRes()
    )

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HostScreen)
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            t0 = time.monotonic()
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, SessionScreen)
            # il listing gira in background: si può tornare indietro
            await pilot.press("escape")
            await pilot.pause(0.3)
            elapsed = time.monotonic() - t0
            assert isinstance(app.screen, HostScreen)
            assert elapsed < 2.5, f"TUI bloccata per {elapsed:.1f}s"
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_ping_all_does_not_block(monkeypatch):
    """Il ping di tutti gli host non deve congelare la lista."""

    def slow_ping(host, timeout=2.0):
        time.sleep(1.0)
        return True, "ok"

    monkeypatch.setattr(ssh_adapter, "tcp_ping", slow_ping)

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HostScreen)
            t0 = time.monotonic()
            await pilot.press("T")
            await pilot.pause(0.2)
            # durante il ping (1s per host) si può uscire
            await pilot.press("q")
            await pilot.pause(0.3)
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"ping ha bloccato per {elapsed:.1f}s"

    asyncio.run(run())


def test_keyring_timeout_is_bounded(monkeypatch):
    """Un backend keyring che non risponde deve fallire, non restare appeso."""
    from bravoric_ssh_client.credentials import keyring as kr

    monkeypatch.setattr(kr, "TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(kr.keyring, "get_password", lambda service, key: time.sleep(5))
    kr._unresponsive.clear()
    provider = kr.KeyringProvider()
    host = Host(alias="alpha", host="10.0.0.1", user="a", auth="keyring")

    t0 = time.monotonic()
    raised = False
    try:
        provider.get(host)
    except kr.CredentialError:
        raised = True
    finally:
        kr._unresponsive.clear()
    assert raised
    assert time.monotonic() - t0 < 2.0


def test_keyring_hang_does_not_block_ui(monkeypatch):
    """Se il keyring si blocca all'avvio, la lista host resta navigabile."""
    from bravoric_ssh_client.credentials import keyring as kr

    monkeypatch.setattr(kr, "TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(kr.keyring, "get_password", lambda service, key: time.sleep(5))
    kr._unresponsive.clear()

    cfg = Config()
    cfg.hosts = [Host(alias="alpha", host="10.0.0.1", user="a", auth="keyring")]

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HostScreen)
            t0 = time.monotonic()
            await pilot.press("q")
            await pilot.pause(0.3)
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"keyring ha bloccato per {elapsed:.1f}s"
            kr._unresponsive.clear()

    asyncio.run(run())
