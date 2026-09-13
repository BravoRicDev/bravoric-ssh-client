"""Test TUI per tunnel, snippet e broadcast (pilot di Textual, nessuna rete)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bravoric_ssh_client.app import (
    BravoricApp,
    BroadcastHostPickerScreen,
    SnippetCatalogScreen,
    TunnelScreen,
)
from bravoric_ssh_client.config import Config, Host, Tunnel


def make_config() -> Config:
    import tempfile

    tmp = tempfile.mkdtemp(prefix="bsc-tui-")
    cfg = Config(path=Path(tmp) / "config.toml", snippets_file=str(Path(tmp) / "snippets.json"))
    cfg.hosts = [
        Host(alias="alpha", host="192.0.2.1", user="alice", auth="key"),
        Host(
            alias="beta",
            host="192.0.2.2",
            user="bob",
            auth="key",
            tunnels=[Tunnel(kind="L", local_port=5432, remote_host="db", remote_port=5432)],
        ),
    ]
    return cfg


def _run(coro):
    return asyncio.run(coro)


def _label_text(screen, widget_id, index=0):
    """Testo visibile dell'ListItem ``index`` della ListView con id=widget_id."""
    lv = screen.query_one(f"#{widget_id}")
    if len(lv.children) <= index:
        return ""
    label = lv.children[index].children[0]
    return str(label.render())


def test_host_screen_shows_tunnel_semaphore_off():
    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "🔴" in _label_text(app.screen, "host-list", 1)  # tunnel configurato ma spento
            await pilot.press("q")
            await pilot.pause()

    _run(run())


def test_tunnel_screen_lists_and_adds(monkeypatch):
    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 1
            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen, TunnelScreen)
            assert "5432" in _label_text(app.screen, "tunnel-list")
            # aggiunge un tunnel SOCKS
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#t-name").value = "socks"
            app.screen.query_one("#t-kind").value = "D"
            app.screen.query_one("#t-local").value = "1080"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(app.screen._host.tunnels) == 2
            await pilot.press("q")
            await pilot.pause()

    _run(run())


def test_snippet_catalog_and_host_picker(monkeypatch):
    from bravoric_ssh_client.ssh import broadcast as bcast_mod

    class FakeResult:
        def __init__(self, alias):
            self.host_alias = alias
            self.ok = True
            self.exit_code = 0
            self.stdout = "ok\n"
            self.stderr = ""
            self.error = ""
            self.session_name = f"bcast-uptime-{alias}-ts"

    def fake_tmux(hosts, cmd, snippet, cfg, timeout=60, max_workers=8):
        return [FakeResult(h.alias) for h in hosts]

    monkeypatch.setattr(bcast_mod, "run_snippet_on_hosts_tmux", fake_tmux)
    monkeypatch.setattr(
        bcast_mod,
        "run_snippet_on_hosts",
        lambda hosts, cmd, cfg, timeout=60, max_workers=8: [FakeResult(h.alias) for h in hosts],
    )

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            await pilot.pause()
            assert isinstance(app.screen, SnippetCatalogScreen)
            # nessuno snippet -> messaggio
            assert "Nessuno snippet" in _label_text(app.screen, "snippet-list")
            # aggiungi snippet
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#s-name").value = "uptime"
            app.screen.query_one("#s-cmd").value = "uptime"
            await pilot.press("ctrl+s")
            await pilot.pause()
            # ora c'è lo snippet
            assert "uptime" in _label_text(app.screen, "snippet-list")
            # entra nel picker host
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, BroadcastHostPickerScreen)
            # marca tutti ed esegui
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "BroadcastResultScreen"
            # attende che il worker completi
            for _ in range(20):
                await pilot.pause()
                if not app.screen._busy:
                    break
            assert not app.screen._busy
            await pilot.press("q")
            await pilot.pause()

    _run(run())


def test_broadcast_mode_toggle(monkeypatch):
    """Il tasto t cambia modalità tmux → diretta (default è tmux)."""
    from bravoric_ssh_client.ssh import broadcast as bcast_mod

    monkeypatch.setattr(
        bcast_mod,
        "run_snippet_on_hosts_tmux",
        lambda hosts, cmd, snippet, cfg, timeout=60, max_workers=8: [],
    )
    monkeypatch.setattr(
        bcast_mod,
        "run_snippet_on_hosts",
        lambda hosts, cmd, cfg, timeout=60, max_workers=8: [],
    )

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            await pilot.pause()
            # crea uno snippet
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#s-name").value = "up"
            app.screen.query_one("#s-cmd").value = "uptime"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, BroadcastHostPickerScreen)
            assert picker._tmux_mode is True  # default tmux
            await pilot.press("t")
            await pilot.pause()
            assert picker._tmux_mode is False
            await pilot.press("t")
            await pilot.pause()
            assert picker._tmux_mode is True
            await pilot.press("q")
            await pilot.pause()

    _run(run())


def test_snippet_form_validation():
    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("B")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            # salva senza comando -> errore, resta nel form
            app.screen.query_one("#s-name").value = "x"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "SnippetFormScreen"
            await pilot.press("q")
            await pilot.pause()

    _run(run())
