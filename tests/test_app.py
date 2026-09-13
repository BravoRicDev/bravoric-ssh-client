"""Test di integrazione della TUI con il pilot di Textual (senza rete reale).

Verificano che:
- l'host screen elenchi gli host;
- premere Enter apra le sessioni;
- premere "s" (shell) faccia USCIRE l'app con un LaunchAction (fix: niente exec
  dentro la TUI, che lasciava il terminale sporco);
- premere "q" esca senza azioni.
"""

from __future__ import annotations

from bravoric_ssh_client.app import BravoricApp, HostScreen, LaunchAction, SessionScreen
from bravoric_ssh_client.config import Config, Host


def make_config() -> Config:
    cfg = Config()
    cfg.hosts = [
        Host(alias="alpha", host="192.0.2.1", user="alice", auth="key"),
        Host(alias="beta", host="192.0.2.2", user="bob", auth="key"),
    ]
    return cfg


async def select_host(app: BravoricApp, pilot, index: int) -> None:
    assert isinstance(app.screen, HostScreen)
    lv = app.screen.query_one("#host-list")
    lv.index = index
    await pilot.press("enter")


def test_host_screen_lists_hosts():
    import asyncio

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HostScreen)
            lv = app.screen.query_one("#host-list")
            assert len(lv.children) == 2
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_shell_key_exits_with_launch_action():
    """Il tasto 's' deve uscire dall'app con LaunchAction(kind='shell')."""
    import asyncio

    async def run():
        app = BravoricApp(config=make_config())
        # host irraggiungibile: il refresh sessioni fallirà, ma 's' deve comunque uscire
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await select_host(app, pilot, 0)
            await pilot.pause()
            assert isinstance(app.screen, SessionScreen)
            await pilot.press("s")
            await pilot.pause()
            # l'app deve essersi chiusa con un LaunchAction
            result = app.return_value
            assert isinstance(result, LaunchAction)
            assert result.kind == "shell"
            assert result.host.alias == "alpha"

    asyncio.run(run())


def test_attach_exits_with_launch_action(monkeypatch):
    """Con sessioni note, Enter deve uscire con LaunchAction(kind='attach')."""
    import asyncio

    from bravoric_ssh_client.ssh import adapter as ssh_adapter

    class FakeRes:
        ok = True
        sessions = ["sess1", "sess2"]
        error = ""

    monkeypatch.setattr(ssh_adapter, "list_tmux_sessions", lambda host, cfg=None: FakeRes())

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await select_host(app, pilot, 0)
            await pilot.pause(0.2)
            assert isinstance(app.screen, SessionScreen)
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.2)
            result = app.return_value
            assert isinstance(result, LaunchAction)
            assert result.kind == "attach"
            assert result.session == "sess1"
            assert result.host.alias == "alpha"

    asyncio.run(run())


def test_quit_exits_without_action():
    import asyncio

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            assert app.return_value is None

    asyncio.run(run())


def test_filter_keeps_focus_on_input():
    """Scrivere nel campo filtro non deve rubare il focus dall'Input."""
    import asyncio

    from textual.widgets import Input

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HostScreen)
            inp = app.screen.query_one("#filter-input", Input)
            inp.focus()
            await pilot.pause()
            for ch in "al":
                await pilot.press(ch)
                await pilot.pause()
            assert app.focused is inp, f"focus perso durante il filtro: {app.focused}"
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())
