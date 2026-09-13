"""Test di cronologia (history) e schermata Recenti + titolo finestra."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bravoric_ssh_client.app import BravoricApp, HostScreen, InputScreen, RecentScreen
from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.history import HistoryEntry, load_history, record_history


def make_cfg(tmp_path: Path) -> Config:
    cfg = Config(hosts=[Host(alias="alpha", host="10.0.0.1", user="a", auth="key")])
    cfg.path = tmp_path / "config.toml"
    return cfg


def test_history_roundtrip(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    record_history(cfg, "alpha", "lavoro")
    record_history(cfg, "beta", "ops")
    record_history(cfg, "alpha", "lavoro")  # duplicato -> in testa senza doppio
    entries = load_history(cfg)
    assert [(e.host, e.session) for e in entries] == [
        ("alpha", "lavoro"),
        ("beta", "ops"),
    ]


def test_history_limit(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    cfg.history_size = 2
    for i in range(5):
        record_history(cfg, "alpha", f"s{i}")
    entries = load_history(cfg)
    assert len(entries) == 2
    assert entries[0].session == "s4"


def test_history_file_missing(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    assert load_history(cfg) == []


def test_recent_screen_shows_and_opens(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    record_history(cfg, "alpha", "lavoro")

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HostScreen)
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, RecentScreen)
            assert len(app.screen._entries) == 1
            assert app.screen._entries[0].session == "lavoro"
            await pilot.press("enter")
            await pilot.pause(0.5)
            from bravoric_ssh_client.app import SessionScreen

            assert isinstance(app.screen, SessionScreen)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_recent_skips_unknown_host(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    record_history(cfg, "sparito", "x")

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, RecentScreen)
            # host non in config: selezione non deve aprire nulla di rotto
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, RecentScreen)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_input_screen_suggestions(tmp_path: Path):
    """I suggerimenti vengono applicati con le frecce."""
    results: list[str] = []

    async def run():
        app = BravoricApp(config=make_cfg(tmp_path))
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            iscreen = InputScreen(
                "Nuova", "nome", lambda v: results.append(v), suggestions=["a", "b", "c"]
            )
            app.push_screen(iscreen)
            await pilot.pause()
            assert isinstance(app.screen, InputScreen)
            # down due volte -> 'c'
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            assert app.screen.query_one("#text-input").value == "c"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert results == ["c"], results

    asyncio.run(run())


def test_history_entry_key():
    assert HistoryEntry("h", "s").key() == "h/s"
