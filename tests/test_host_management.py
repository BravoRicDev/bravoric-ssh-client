"""Test di integrazione: gestione host dalla TUI (aggiungi/modifica/elimina/duplica/password).

Usano una Config su file temporaneo per verificare anche la persistenza.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Input

from bravoric_ssh_client.app import (
    BravoricApp,
    ConfirmScreen,
    HostFormScreen,
    HostScreen,
    PasswordScreen,
)
from bravoric_ssh_client.config import Config, Host, load_config, save_config
from bravoric_ssh_client.credentials.factory import password_present


def make_config_file(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.hosts = [
        Host(alias="alpha", host="192.0.2.1", user="alice", auth=""),
        Host(alias="beta", host="192.0.2.2", user="bob", auth=""),
    ]
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    return cfg


async def open_host_screen(app, pilot):
    await pilot.pause()
    assert isinstance(app.screen, HostScreen)
    return app.screen


def test_add_host_persists(tmp_path: Path):
    cfg = make_config_file(tmp_path)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            screen = await open_host_screen(app, pilot)
            assert len(screen.query_one("#host-list").children) == 2
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, HostFormScreen)
            # riempi i campi direttamente
            app.screen.query_one("#f-alias", Input).value = "gamma"
            app.screen.query_one("#f-host", Input).value = "10.0.0.9"
            app.screen.query_one("#f-user", Input).value = "gina"
            app.screen.query_one("#f-port", Input).value = "2222"
            app.screen.query_one("#f-auth", Input).value = "keyring"
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            assert isinstance(app.screen, HostScreen)
            reloaded = load_config(cfg.path)
            assert reloaded.host("gamma") is not None
            assert reloaded.host("gamma").port == 2222
            assert reloaded.host("gamma").auth == "keyring"

    asyncio.run(run())


def test_edit_host(tmp_path: Path):
    cfg = make_config_file(tmp_path)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            screen = await open_host_screen(app, pilot)
            lv = screen.query_one("#host-list")
            lv.index = 1  # beta
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, HostFormScreen)
            app.screen.query_one("#f-host", Input).value = "10.9.9.9"
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            reloaded = load_config(cfg.path)
            beta = reloaded.host("beta")
            assert beta is not None
            assert beta.host == "10.9.9.9"

    asyncio.run(run())


def test_delete_host(tmp_path: Path):
    cfg = make_config_file(tmp_path)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            screen = await open_host_screen(app, pilot)
            lv = screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause(0.3)
            assert isinstance(app.screen, HostScreen)
            reloaded = load_config(cfg.path)
            assert reloaded.host("alpha") is None
            assert reloaded.host("beta") is not None

    asyncio.run(run())


def test_duplicate_host(tmp_path: Path):
    cfg = make_config_file(tmp_path)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            screen = await open_host_screen(app, pilot)
            lv = screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("D")
            await pilot.pause(0.3)
            assert isinstance(app.screen, HostScreen)
            reloaded = load_config(cfg.path)
            assert reloaded.host("alpha-copia") is not None
            assert reloaded.host("alpha-copia").host == "192.0.2.1"

    asyncio.run(run())


def test_set_password_via_ui(tmp_path: Path, monkeypatch):
    cfg = make_config_file(tmp_path)
    # usa provider plain su file temporaneo per non toccare il keyring
    cfg.credential_provider = "plain"
    from bravoric_ssh_client.credentials.plain import PlainProvider

    secrets = tmp_path / "secrets.tsv"
    provider = PlainProvider(secrets)
    monkeypatch.setattr(
        "bravoric_ssh_client.credentials.factory.PlainProvider",
        lambda config=None: provider,
    )

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            screen = await open_host_screen(app, pilot)
            lv = screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, PasswordScreen)
            app.screen.query_one("#pw-input", Input).value = "supersecret"
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            assert isinstance(app.screen, HostScreen)
            assert password_present(cfg, cfg.host("alpha")) is True

    asyncio.run(run())


def test_filter_hosts(tmp_path: Path):
    cfg = make_config_file(tmp_path)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            screen = await open_host_screen(app, pilot)
            inp = screen.query_one("#filter-input")
            inp.value = "bet"
            await pilot.pause(0.2)
            lv = screen.query_one("#host-list")
            # solo beta visibile
            visible_labels = [str(c) for c in lv.children]
            assert len(visible_labels) == 1

    asyncio.run(run())


def test_backup_created_on_change(tmp_path: Path):
    cfg = make_config_file(tmp_path)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            screen = await open_host_screen(app, pilot)
            lv = screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("D")
            await pilot.pause(0.3)
            backups = list(tmp_path.glob("config.toml.bak.*"))
            assert len(backups) == 1

    asyncio.run(run())
