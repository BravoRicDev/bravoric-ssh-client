"""Test TUI delle azioni tmux (rename, kill, dettagli, finestre) con mock dell'adapter.

Non usano rete reale: si mockano le funzioni ssh_adapter.*.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input

from bravoric_ssh_client.app import (
    BravoricApp,
    ConfirmScreen,
    InfoScreen,
    InputScreen,
    SessionScreen,
)
from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.ssh import adapter as ssh_adapter


def make_config() -> Config:
    cfg = Config()
    cfg.hosts = [Host(alias="alpha", host="192.0.2.1", user="alice", auth="key")]
    return cfg


class FakeRes:
    def __init__(self, ok=True, stdout="", stderr=""):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr


async def open_sessions(app: BravoricApp, pilot):
    await pilot.pause()
    lv = app.screen.query_one("#host-list")
    lv.index = 0
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, SessionScreen)
    app.screen._sessions = ["sess1", "sess2"]
    app.screen._loading = False
    app.screen._tmux_present = True
    app.screen._render_sessions()
    await pilot.pause()
    return app.screen


def test_rename_session_flow(monkeypatch):
    calls: list = []

    def fake_rename(host, old, new, cfg=None):
        calls.append((old, new))
        return FakeRes(ok=True)

    monkeypatch.setattr(ssh_adapter, "tmux_rename_session", fake_rename)

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(120, 35)) as pilot:
            await open_sessions(app, pilot)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, InputScreen)
            app.screen.query_one("#text-input", Input).value = "sess-rename"
            await pilot.press("ctrl+s")
            await pilot.pause(1.0)
            assert isinstance(app.screen, SessionScreen)
            assert calls == [("sess1", "sess-rename")], calls
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_kill_session_requires_confirm(monkeypatch):
    calls: list = []

    def fake_kill(host, name, cfg=None):
        calls.append(name)
        return FakeRes(ok=True)

    monkeypatch.setattr(ssh_adapter, "tmux_kill_session", fake_kill)

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(120, 35)) as pilot:
            await open_sessions(app, pilot)
            await pilot.press("k")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            # senza conferma: nessun kill
            await pilot.press("n")
            await pilot.pause(0.2)
            assert calls == []
            # riprova e conferma
            await pilot.press("k")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause(0.3)
            assert calls == ["sess1"], calls
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_details_shows_info(monkeypatch):
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_session_details",
        lambda host, name, cfg=None: FakeRes(ok=True, stdout="sess1\t2\t123\t80x24\t1"),
    )

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(120, 35)) as pilot:
            await open_sessions(app, pilot)
            await pilot.press("d")
            await pilot.pause(0.3)
            assert isinstance(app.screen, InfoScreen), type(app.screen).__name__
            assert "sess1" in app.screen._body
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_windows_shows_info(monkeypatch):
    from bravoric_ssh_client.app import WindowsScreen

    monkeypatch.setattr(
        ssh_adapter,
        "tmux_list_windows",
        lambda host, name, cfg=None: FakeRes(ok=True, stdout="0: bash* (1 panes)"),
    )

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(120, 35)) as pilot:
            await open_sessions(app, pilot)
            await pilot.press("w")
            await pilot.pause(0.8)
            assert isinstance(app.screen, WindowsScreen), type(app.screen).__name__
            assert app.screen._windows == ["0: bash* (1 panes)"], app.screen._windows
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_attach_ro_launches(monkeypatch):
    """R deve produrre un LaunchAction attach_ro all'uscita dell'app."""
    from bravoric_ssh_client.app import LaunchAction

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(120, 35)) as pilot:
            await open_sessions(app, pilot)
            await pilot.press("R")
            await pilot.pause(0.3)
            result = app.return_value
            assert isinstance(result, LaunchAction)
            assert result.kind == "attach_ro"
            assert result.session == "sess1"
            await pilot.pause()

    asyncio.run(run())
