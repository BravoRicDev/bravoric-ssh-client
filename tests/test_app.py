"""Test di integrazione della TUI con il pilot di Textual (senza rete reale).

Verificano che:
- l'host screen elenchi gli host;
- premere Enter apra le sessioni in una NUOVA finestra di terminale scelta
  dall'utente (la TUI resta viva, non esce con LaunchAction);
- premere Esc sulla scelta del terminale annulli senza lanciare nulla;
- il flusso legacy request_launch() senza in_new_terminal continui a uscire
  con un LaunchAction (fix: niente exec dentro la TUI, che lasciava il
  terminale sporco);
- premere "q" esca senza azioni.
"""

from __future__ import annotations

from textual.widgets import ListView

from bravoric_ssh_client.app import (
    BravoricApp,
    HostScreen,
    InputScreen,
    LaunchAction,
    SessionScreen,
)
from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.terminal_choice import TerminalChoiceScreen


def make_config() -> Config:
    cfg = Config()
    cfg.hosts = [
        Host(alias="alpha", host="192.0.2.1", user="alice", auth="key"),
        Host(alias="beta", host="192.0.2.2", user="bob", auth="key"),
    ]
    return cfg


def stub_terminal_launch(
    monkeypatch, terminals=("/usr/bin/gnome-terminal", "/usr/bin/kitty")
) -> list[list[str]]:
    """Neutralizza il lancio reale: restituisce la lista degli argv registrati.

    Intercetta detect_gui_terminals, subprocess.Popen e record_history, così il
    test non apre finestre vere e non scrive nella cronologia dell'utente.

    Nota su Popen: `app_module.subprocess` è il modulo subprocess condiviso, quindi
    la patch è globale e intercetta anche le chiamate di ssh_adapter (ssh verso
    l'host irraggiungibile). Per questo si registrano SOLO gli argv il cui primo
    elemento è uno dei terminali stub: `spawned` contiene i soli lanci di finestra.
    """
    from bravoric_ssh_client import app as app_module
    from bravoric_ssh_client import history as history_module

    stub_set = set(terminals)
    spawned: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        argv = list(argv)
        if argv and argv[0] in stub_set:
            spawned.append(argv)
        return object()

    monkeypatch.setattr(app_module, "detect_gui_terminals", lambda: list(terminals))
    monkeypatch.setattr(app_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(history_module, "record_history", lambda *a, **k: None)
    return spawned


async def select_host(app: BravoricApp, pilot, index: int) -> None:
    assert isinstance(app.screen, HostScreen)
    lv = app.screen.query_one("#host-list", ListView)
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


def test_shell_key_opens_terminal_choice(monkeypatch):
    """Il tasto 's' chiede il terminale e apre la shell in una finestra nuova."""
    import asyncio

    spawned = stub_terminal_launch(monkeypatch)

    async def run():
        app = BravoricApp(config=make_config())
        # host irraggiungibile: il refresh sessioni fallirà, ma 's' deve comunque funzionare
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await select_host(app, pilot, 0)
            await pilot.pause()
            assert isinstance(app.screen, SessionScreen)
            await pilot.press("s")
            await pilot.pause()
            # la TUI resta viva e chiede quale terminale usare
            assert app.return_value is None
            assert isinstance(app.screen, TerminalChoiceScreen)
            await pilot.press("enter")
            await pilot.pause()
            # scelto gnome-terminal: la finestra parte con la shell verso alpha
            assert len(spawned) == 1, spawned
            argv = spawned[0]
            assert argv[0] == "/usr/bin/gnome-terminal"
            assert "--shell" in argv[4]  # dentro lo sh -c
            assert "alpha" in argv[4]  # dentro lo sh -c
            # si torna alla SessionScreen
            assert isinstance(app.screen, SessionScreen)

    asyncio.run(run())


def test_shell_key_terminal_choice_cancel(monkeypatch):
    """Esc sulla scelta del terminale non lancia nulla e resta sulla SessionScreen."""
    import asyncio

    spawned = stub_terminal_launch(monkeypatch)

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await select_host(app, pilot, 0)
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, TerminalChoiceScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert spawned == []
            assert isinstance(app.screen, SessionScreen)
            assert app.return_value is None

    asyncio.run(run())


def test_attach_opens_terminal_choice(monkeypatch):
    """Con sessioni note, Enter chiede il terminale e apre la sessione in finestra nuova."""
    import asyncio

    from bravoric_ssh_client.ssh import adapter as ssh_adapter

    class FakeRes:
        ok = True
        sessions = ["sess1", "sess2"]
        error = ""

    monkeypatch.setattr(ssh_adapter, "list_tmux_sessions", lambda host, cfg=None: FakeRes())
    spawned = stub_terminal_launch(
        monkeypatch, terminals=("/usr/bin/kitty", "/usr/bin/gnome-terminal")
    )

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
            assert isinstance(app.screen, TerminalChoiceScreen)
            # il ListView parte vuoto: seleziona il primo terminale
            lv = app.screen.query_one("#term-list", ListView)
            lv.index = 0
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert len(spawned) == 1, spawned
            argv = spawned[0]
            assert argv[0] == "/usr/bin/kitty"
            # kitty usa --hold invece di --attach
            assert "--hold" in argv
            assert "sess1" in argv[4]  # dentro lo sh -c
            assert "alpha" in argv[4]  # dentro lo sh -c

    asyncio.run(run())


def test_single_terminal_skips_choice(monkeypatch):
    """Con UN SOLO terminale disponibile non si chiede: si lancia e basta.

    Chiedere con una voce sola e' una domanda inutile, ed era il sintomo visto
    con BRAVORIC_TERMINAL impostata ("vede solo kitty"). Il lancio deve partire
    al primo Enter, senza passare dalla TerminalChoiceScreen.
    """
    import asyncio

    from bravoric_ssh_client.ssh import adapter as ssh_adapter

    class FakeRes:
        ok = True
        sessions = ["sess1", "sess2"]
        error = ""

    monkeypatch.setattr(ssh_adapter, "list_tmux_sessions", lambda host, cfg=None: FakeRes())
    # un terminale solo: ptyxis, che usa -x (non --hold come kitty)
    spawned = stub_terminal_launch(monkeypatch, terminals=("/usr/bin/ptyxis",))

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
            # nessuna schermata di scelta: e' gia' partito
            assert not isinstance(app.screen, TerminalChoiceScreen), (
                "con un solo terminale la schermata di scelta non deve comparire"
            )
            assert isinstance(app.screen, SessionScreen)
            assert app.return_value is None  # la TUI resta viva
            assert len(spawned) == 1, spawned
            argv = spawned[0]
            assert argv[0] == "/usr/bin/ptyxis"
            assert "-x" in argv  # ptyxis, non kitty
            assert "sess1" in argv[2]  # dentro lo sh -c
            assert "alpha" in argv[2]

    asyncio.run(run())


def test_new_session_input_survives_ctrl_s(monkeypatch):
    """Regressione: 'n' -> nome -> Ctrl+S deve aprire la scelta del terminale.

    L'InputScreen deve chiudere se stesso PRIMA di eseguire la callback. Se il
    pop_screen resta in coda alla callback, colpisce la TerminalChoiceScreen
    appena aperta e annulla il lancio: dopo Ctrl+S si restava bloccati
    sull'InputScreen senza che nulla partisse.
    """
    import asyncio

    from textual.widgets import Input

    from bravoric_ssh_client import history as history_module

    # l'InputScreen della nuova sessione legge la cronologia: va neutralizzata,
    # o il suggerimento dipenderebbe dai dati reali dell'utente
    monkeypatch.setattr(history_module, "load_history", lambda cfg: [])
    spawned = stub_terminal_launch(monkeypatch)

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await select_host(app, pilot, 0)
            await pilot.pause(0.2)
            assert isinstance(app.screen, SessionScreen)

            await pilot.press("n")
            await pilot.pause(0.2)
            assert isinstance(app.screen, InputScreen)
            app.screen.query_one("#text-input", Input).value = "nuovasess"

            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            # il cuore della regressione: NON si deve restare sull'InputScreen
            assert not isinstance(app.screen, InputScreen), (
                "pop in coda alla callback: ha chiuso la scelta del terminale"
            )
            assert isinstance(app.screen, TerminalChoiceScreen)
            assert app.return_value is None  # la TUI resta viva
            assert spawned == []

            # il ListView parte vuoto: seleziona il primo terminale
            app.screen.query_one("#term-list", ListView).index = 0
            await pilot.press("enter")
            await pilot.pause(0.2)

            assert len(spawned) == 1, spawned
            argv = spawned[0]
            assert argv[0] == "/usr/bin/gnome-terminal"
            assert "--new" in argv[4]  # dentro lo sh -c
            assert "nuovasess" in argv[4]
            assert "alpha" in argv[4]
            assert isinstance(app.screen, SessionScreen)

    asyncio.run(run())


def test_request_launch_without_new_terminal_still_exits():
    """Il flusso legacy (in_new_terminal=False) deve ancora uscire con LaunchAction."""
    import asyncio

    async def run():
        app = BravoricApp(config=make_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await select_host(app, pilot, 0)
            await pilot.pause()
            assert isinstance(app.screen, SessionScreen)
            app.request_launch(LaunchAction(kind="shell", host=make_config().hosts[0]))
            await pilot.pause()
            result = app.return_value
            assert isinstance(result, LaunchAction)
            assert result.kind == "shell"
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
