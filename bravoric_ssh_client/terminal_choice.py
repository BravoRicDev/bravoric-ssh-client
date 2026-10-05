"""Schermata per scegliere il terminale GUI in cui aprire una sessione remota."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView


class TerminalChoiceScreen(Screen[None]):
    """Chiede quale terminale usare per aprire la sessione.

    Riceve l'elenco dei terminali già rilevati (percorsi assoluti) invece di
    rilevarli da sé, così la lista mostrata è sempre la stessa che verrà usata
    per il lancio. Mostra il nome base del binario, ma invoca la callback con
    il percorso completo.

    Invio su un terminale: lo sceglie e chiama ``on_submit``.
    Escape / q: annulla (la callback non viene chiamata).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("q", "cancel", "Annulla"),
    ]

    def __init__(
        self,
        terminals: Sequence[str],
        on_submit: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._terminals: list[str] = list(terminals)
        self._on_submit = on_submit

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="term-box"):
            yield Label("[b]Scegli il terminale[/b]", classes="box-title")
            yield ListView(id="term-list")
            yield Label("Invio: apri · Esc/q: annulla", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#term-list", ListView)
        for t in self._terminals:
            lv.append(ListItem(Label(f"[b]{os.path.basename(t)}[/b]")))
        if self._terminals:
            lv.index = 0
            lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None or index >= len(self._terminals):
            return
        self._on_submit(self._terminals[index])
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()
