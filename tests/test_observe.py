"""Test della modalità ObserveScreen (auto-cycle) e della riapertura recenti."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from bravoric_ssh_client.app import BravoricApp, HostScreen, ObserveScreen, RecentScreen
from bravoric_ssh_client.config import Config, Host, load_config
from bravoric_ssh_client.ssh import adapter as ssh_adapter

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only shell/exec paths")


def make_cfg() -> Config:
    cfg = Config()
    cfg.hosts = [
        Host(
            alias="alpha",
            host="10.0.0.1",
            user="a",
            auth="key",
            auto_cycle=True,
            cycle_interval=120,
        ),
    ]
    return cfg


class FakeRes:
    def __init__(self, ok=True, stdout="", stderr="", sessions=None):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.sessions = sessions or (stdout.splitlines() if stdout else [])


def test_config_auto_cycle_parsing(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[[hosts]]\nalias='a'\nhost='10.0.0.1'\nauto_cycle=true\ncycle_interval=30\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    h = cfg.host("a")
    assert h is not None
    assert h.auto_cycle is True
    assert h.cycle_interval == 30


def test_config_auto_cycle_default_interval():
    h = Host(alias="a", host="10.0.0.1")
    assert h.cycle_interval == 120
    assert h.auto_cycle is None


def test_observe_screen_shows_content(monkeypatch):
    """L'ObserveScreen deve mostrare il contenuto catturato e le sessioni."""
    monkeypatch.setattr(
        ssh_adapter,
        "list_tmux_sessions",
        lambda host, cfg=None: FakeRes(ok=True, stdout="s1\ns2\n"),
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(
            ok=True, stdout=f"[contenuto di {session}]"
        ),
    )

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HostScreen)
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("o")
            await pilot.pause()
            assert isinstance(app.screen, ObserveScreen), type(app.screen).__name__
            await pilot.pause(0.5)
            assert app.screen._views == [("alpha", "s1"), ("alpha", "s2")]
            pane = app.screen.query_one("#observe-pane")
            content = getattr(pane, "_Static__content", "")
            assert "contenuto di s1" in str(content)
            # n = successiva
            await pilot.press("n")
            await pilot.pause(0.4)
            assert app.screen._current == 1
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, HostScreen)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_observe_next_prev_cycle(monkeypatch):
    monkeypatch.setattr(
        ssh_adapter,
        "list_tmux_sessions",
        lambda host, cfg=None: FakeRes(ok=True, stdout="s1\ns2\ns3\n"),
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(ok=True, stdout=session),
    )

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("o")
            await pilot.pause(0.4)
            s = app.screen
            assert isinstance(s, ObserveScreen)
            assert s._views == [("alpha", "s1"), ("alpha", "s2"), ("alpha", "s3")]
            # next -> wrap
            s._current = 2
            s.action_next()
            assert s._current == 0
            # prev -> wrap indietro
            s.action_prev()
            assert s._current == 2
            # n dal binding
            await pilot.press("n")
            await pilot.pause(0.1)
            assert s._current == 0
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_observe_manual_switch_shows_immediately_with_slow_capture(monkeypatch):
    """Con cattura lenta, n/p deve mostrare SUBITO la vista nuova (placeholder),
    non lasciare il contenuto della vista precedente."""
    import time

    def slow_capture(host, session, cfg=None, lines=200, window=None):
        time.sleep(0.4)
        return FakeRes(ok=True, stdout=f"contenuto di {session}")

    monkeypatch.setattr(
        ssh_adapter,
        "list_tmux_sessions",
        lambda host, cfg=None: FakeRes(ok=True, stdout="s1\ns2\n"),
    )
    monkeypatch.setattr(ssh_adapter, "tmux_capture_pane", slow_capture)

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("o")
            await pilot.pause(1.2)
            s = app.screen
            assert isinstance(s, ObserveScreen)
            pane = s.query_one("#observe-pane")
            assert "contenuto di s1" in str(getattr(pane, "_Static__content", ""))
            # cambio manuale: il pane deve aggiornarsi subito alla nuova vista
            await pilot.press("n")
            await pilot.pause(0.05)
            content = str(getattr(pane, "_Static__content", ""))
            assert "s2" in content and "caricamento" in content, content
            # dopo che la cattura lenta termina, arriva il contenuto vero
            await pilot.pause(0.8)
            assert "contenuto di s2" in str(getattr(pane, "_Static__content", ""))
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_reopen_all_dedup(monkeypatch, tmp_path: Path):
    """Riapri tutte: senza doppioni, una finestra per (host, sessione) unica."""
    from bravoric_ssh_client.history import record_history

    cfg = make_cfg()
    cfg.path = tmp_path / "config.toml"
    # scrivi la cronologia su disco con una entry duplicata
    record_history(cfg, "alpha", "s1")
    record_history(cfg, "alpha", "s1")
    record_history(cfg, "alpha", "s2")

    launched: list = []

    monkeypatch.setattr(
        "bravoric_ssh_client.app.shutil.which",
        lambda name: "/usr/bin/ptyxis",
    )

    def fake_popen(cmd, **kw):
        launched.append(cmd)

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, RecentScreen), type(app.screen).__name__
            app.screen.action_reopen_all()
            await pilot.pause(1.5)
            # 2 uniche -> 2 processi ptyxis
            assert len(launched) == 2, launched
            # ogni Popen: [ptyxis, "-x", "sh -c 'exec ... --attach host sess'"]
            sessions = []
            for cmd in launched:
                assert cmd[0] == "/usr/bin/ptyxis"
                assert cmd[1] == "-x"
                tail = cmd[-1]
                assert tail.startswith("sh -c "), tail
                assert "--attach" in tail
                # estrai la sessione: ultimo token quotato nella stringa comando
                toks = tail.replace("'\\''", "").replace("'", " ").split()
                sessions.append(toks[-1])
            assert sessions.count("s1") == 1  # senza doppioni
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_observe_attach_launches(monkeypatch):
    """Enter nell'ObserveScreen deve produrre un LaunchAction attach."""
    from bravoric_ssh_client.app import LaunchAction

    monkeypatch.setattr(
        ssh_adapter, "list_tmux_sessions", lambda host, cfg=None: FakeRes(ok=True, stdout="s1\n")
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(ok=True, stdout="x"),
    )

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("o")
            await pilot.pause(0.4)
            await pilot.press("enter")
            await pilot.pause(0.3)
            result = app.return_value
            assert isinstance(result, LaunchAction)
            assert result.kind == "attach"
            assert result.host.alias == "alpha"
            assert result.session == "s1"

    asyncio.run(run())


def test_rotation_create_and_pick(monkeypatch):
    """Flusso rotazioni: spunta host -> sessioni selezionate -> deseleziona con Invio."""
    from bravoric_ssh_client.app import (
        ObserveScreen,
        RotationCatalogScreen,
        RotationCreateScreen,
        SessionPickScreen,
    )

    monkeypatch.setattr(
        ssh_adapter,
        "list_tmux_sessions",
        lambda host, cfg=None: FakeRes(ok=True, stdout="s1\ns2\n"),
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(ok=True, stdout=session),
    )

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            assert isinstance(app.screen, RotationCatalogScreen)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, RotationCreateScreen)
            # Enter sull'host -> carica sessioni -> apre il pick
            await pilot.press("enter")
            await pilot.pause(0.8)
            assert isinstance(app.screen, SessionPickScreen), type(app.screen).__name__
            # tutte le sessioni selezionate all'apertura
            assert app.screen._creator._selected == {"alpha": ["s1", "s2"]}
            # Invio su item 1 (sess2) -> deseleziona
            await pilot.press("down")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.screen._creator._selected == {"alpha": ["s1"]}, app.screen._creator._selected
            # torna al creator e avvia
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert isinstance(app.screen, RotationCreateScreen)
            await pilot.press("s")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ObserveScreen), type(app.screen).__name__
            assert app.screen._views == [("alpha", "s1")]
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_rotation_save_and_catalog(tmp_path: Path):
    """Salva un profilo e lo ritrova nel catalogo (senza doppioni)."""
    from bravoric_ssh_client.rotation import Rotation, add_rotation, load_rotations

    cfg = make_cfg()
    cfg.path = tmp_path / "config.toml"
    add_rotation(cfg, Rotation("tutti", [("alpha", "s1"), ("alpha", "s1"), ("alpha", "s2")]))
    rotations = load_rotations(cfg)
    assert len(rotations) == 1
    assert rotations[0].unique_entries() == [("alpha", "s1"), ("alpha", "s2")]
    assert rotations[0].name == "tutti"


def test_observe_rotation_window_title(monkeypatch, tmp_path: Path):
    """Il titolo della rotazione deve mostrare il nome dato al gruppo."""
    from bravoric_ssh_client.app import RotationCatalogScreen
    from bravoric_ssh_client.rotation import Rotation, add_rotation

    monkeypatch.setattr(
        ssh_adapter,
        "list_tmux_sessions",
        lambda host, cfg=None: FakeRes(ok=True, stdout="s1\ns2\n"),
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(ok=True, stdout=session),
    )

    cfg = make_cfg()
    cfg.path = tmp_path / "config.toml"
    add_rotation(cfg, Rotation("Claude - Tutti i server", [("alpha", "s1"), ("alpha", "s2")]))

    async def run():
        app = BravoricApp(config=cfg)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(1.0)
            s = app.screen
            assert isinstance(s, ObserveScreen), type(s).__name__
            # il titolo della schermata (Header) mostra il nome della rotazione
            assert "Claude - Tutti i server" in s.title
            assert s.sub_title == "osservazione"
            header = s.query_one("Header")
            assert "Claude - Tutti i server" in str(header.format_title())
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, RotationCatalogScreen)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_observe_pane_handles_markup_chars(monkeypatch):
    from bravoric_ssh_client.app import ObserveScreen

    monkeypatch.setattr(
        ssh_adapter, "list_tmux_sessions", lambda host, cfg=None: FakeRes(ok=True, stdout="s1\n")
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(
            ok=True, stdout="[/b] [dim] prompt $ comando [ERR]"
        ),
    )

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("o")
            await pilot.pause(0.6)
            s = app.screen
            assert isinstance(s, ObserveScreen)
            pane = s.query_one("#observe-pane")
            content = getattr(pane, "_Static__content", "")
            assert "[/b]" in str(content) or "[ERR]" in str(content)
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("q")
            await pilot.pause()

    asyncio.run(run())


def test_observe_interactive_send_keys(monkeypatch):
    """Modalità interattiva: scrivi, Invio invia testo+Enter, Ctrl+S senza Enter."""
    from bravoric_ssh_client.app import LaunchAction

    sent = []

    def fake_send_keys(host, session, text, cfg=None):
        sent.append(("keys", session, text))
        return FakeRes(ok=True)

    def fake_send_enter(host, session, cfg=None):
        sent.append(("enter", session))
        return FakeRes(ok=True)

    monkeypatch.setattr(
        ssh_adapter,
        "list_tmux_sessions",
        lambda host, cfg=None: FakeRes(ok=True, stdout="s1\ns2\n"),
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(
            ok=True, stdout=f"pane {session}"
        ),
    )
    monkeypatch.setattr(ssh_adapter, "tmux_send_keys", fake_send_keys)
    monkeypatch.setattr(ssh_adapter, "tmux_send_enter", fake_send_enter)

    async def run():
        app = BravoricApp(config=make_cfg())
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            lv = app.screen.query_one("#host-list")
            lv.index = 0
            await pilot.press("o")
            await pilot.pause(0.4)
            s = app.screen
            assert isinstance(s, ObserveScreen)
            # attiva la modalità interattiva
            await pilot.press("i")
            await pilot.pause(0.2)
            assert s._interactive is True
            inp = s.query_one("#observe-input")
            assert inp.disabled is False
            # scrivi e invia con Invio
            inp.focus()
            await pilot.pause(0.1)
            for ch in "hello":
                await pilot.press(ch)
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert ("keys", "s1", "hello") in sent, sent
            assert ("enter", "s1") in sent, sent
            # invio senza Enter via ctrl+s
            for ch in "cd":
                await pilot.press(ch)
            await pilot.press("ctrl+s")
            await pilot.pause(0.4)
            assert ("keys", "s1", "cd") in sent, sent
            # Esc esce dalla modalità interattiva ma resta nella schermata
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert s._interactive is False
            assert isinstance(app.screen, ObserveScreen)
            # attach genera LaunchAction
            await pilot.press("enter")
            await pilot.pause(0.3)
            result = app.return_value
            assert isinstance(result, LaunchAction)
            assert result.kind == "attach"
            assert result.session == "s1"

    asyncio.run(run())


def test_observe_rotation_attach_returns_to_rotation(tmp_path: Path):
    """Da una rotazione, Enter produce LaunchAction con rotation, e all'avvio
    con --rotation la rotazione viene riaperta."""
    from bravoric_ssh_client.app import LaunchAction
    from bravoric_ssh_client.rotation import Rotation, add_rotation

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        ssh_adapter,
        "list_tmux_sessions",
        lambda host, cfg=None: FakeRes(ok=True, stdout="s1\ns2\n"),
    )
    monkeypatch.setattr(
        ssh_adapter,
        "tmux_capture_pane",
        lambda host, session, cfg=None, lines=200, window=None: FakeRes(ok=True, stdout="x"),
    )

    cfg = make_cfg()
    cfg.path = tmp_path / "config.toml"
    add_rotation(cfg, Rotation("rotA", [("alpha", "s1"), ("alpha", "s2")]))

    async def run():
        # 1) avvio con start_rotation -> ObserveScreen della rotazione
        app = BravoricApp(config=cfg, start_rotation="rotA")
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause(1.0)
            s = app.screen
            assert isinstance(s, ObserveScreen)
            assert s._name == "rotA"
            assert s._views == [("alpha", "s1"), ("alpha", "s2")]
            # 2) Enter -> LaunchAction con rotation per tornare dopo l'attach
            await pilot.press("enter")
            await pilot.pause(0.3)
            result = app.return_value
            assert isinstance(result, LaunchAction)
            assert result.kind == "attach"
            assert result.rotation == "rotA"

    asyncio.run(run())
    monkeypatch.undo()
