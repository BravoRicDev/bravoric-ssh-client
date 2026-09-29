"""Applicazione TUI bravoric-ssh-client.

Avvio: ``bravoric-ssh`` oppure ``python -m bravoric_ssh_client``.
Flusso: lista host -> (Enter) schermata sessioni tmux -> attach/crea/shell.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from textual import getters
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TextArea,
)

from .config import AUTH_METHODS, Config, Host, backup_config, load_config, save_config
from .credentials import CredentialError
from .credentials.factory import clear_password, password_present, resolve_password, store_password
from .ssh import adapter as ssh_adapter
from .ssh import tmux_runner
from .ssh.adapter import SshConfig
from .ssh.shellutil import sh_quote
from .ssh.tunnels import TunnelManager


@dataclass
class LaunchAction:
    """Azione da eseguire DOPO che la TUI ha ripristinato il terminale."""

    kind: str  # "attach" | "attach_ro" | "new" | "shell"
    host: Host
    session: str | None = None
    name: str | None = None
    rotation: str | None = None  # riapri questa rotazione dopo l'azione


async def _io(fn, *args, **kwargs):
    """Esegue una funzione bloccante in un thread, senza congelare la TUI."""
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


# Alias storico: quoting condiviso con il layer ssh.
_sh_quote = sh_quote

# Terminali GUI usati per aprire finestre esterne (attach, sftp, riapertura).
# Ordine di rilevamento automatico: il primo trovato nel PATH viene usato.
# La variabile d'ambiente BRAVORIC_TERMINAL sovrascrive tutto
# (utile per scegliere un emulatore specifico, es. kitty per l'avatar immagine).
_TERMINALS_DEFAULT = ("kitty", "ptyxis", "gnome-terminal", "konsole", "alacritty")


def find_gui_terminal() -> str | None:
    """Percorso del terminale GUI da usare per aprire una finestra.

    Ordine: la variabile BRAVORIC_TERMINAL (nome nel PATH oppure percorso
    eseguibile), poi ptyxis, poi gnome-terminal, konsole, alacritty.
    Restituisce None se non ne trova nessuno.
    """
    override = os.environ.get("BRAVORIC_TERMINAL", "").strip()
    if override:
        found = shutil.which(override)
        if not found:
            # un percorso esplicito non passa dal PATH
            try:
                candidate = Path(override).expanduser()
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    found = str(candidate)
            except OSError:
                found = None
        if found:
            return found
    for name in _TERMINALS_DEFAULT:
        found = shutil.which(name)
        if found:
            return found
    return None


def terminal_argv(terminal: str, inner: str) -> list[str]:
    """Argv per far eseguire al terminale ``terminal`` il comando shell ``inner``.

    Ogni emulatore vuole una sintassi diversa:
      ptyxis / kgx    -> -x "sh -c '<cmd>'"   (il comando come UNA stringa)
      kitty           -> --hold sh -c '<cmd>' (programma, senza flag di exec)
      gnome-terminal  -> -- sh -c '<cmd>'
      konsole / xterm -> -e sh -c '<cmd>'

    Per kitty usiamo ``--hold``: se il comando termina subito (es. sessione tmux
    inesistente, host irraggiungibile) la finestra resta aperta e mostra l'errore,
    invece di sparire all'istante.
    """
    name = os.path.basename(terminal)
    if name in ("ptyxis", "kgx"):
        return [terminal, "-x", f"sh -c {_sh_quote(inner)}"]
    if name == "kitty":
        return [terminal, "--hold", "sh", "-c", inner]
    if name == "gnome-terminal":
        return [terminal, "--", "sh", "-c", inner]
    return [terminal, "-e", "sh", "-c", inner]


class BravoricApp(App):
    """App principale: gestisce configurazione e navigazione tra schermate."""

    TITLE = "bravoric-ssh-client"
    SUB_TITLE = "client SSH + tmux"
    CSS = """
    Screen { width: 100%; height: 1fr; }
    .box { width: 80%; height: 1fr; border: round $accent; padding: 0 1; }
    .box-title { text-style: bold; color: $text-muted; width: 80%; }
    .host-info { width: 80%; height: 3; color: $text; content-align: center middle; }
    .list { width: 80%; height: 1fr; }
    .hint { width: 80%; height: 1; color: $text-muted; content-align: center middle; }
    LoadingScreen { align: center middle; }
    LoadingScreen Static { width: auto; }
    #filter-input { width: 80%; margin: 0 0 1 0; }
    #form-box, #confirm-box, #pw-box {
        width: 80%; height: 1fr; border: round $accent; padding: 0 2;
        overflow-y: auto;
    }
    #form-box Input, #pw-box Input { margin: 0 0 1 0; }
    .suggest-list { width: 80%; height: 6; border: dashed $accent; margin-bottom: 1; }
    HostScreen { align: center top; }
    SessionScreen { align: center top; }
    LaunchAgentScreen { align: center top; }
    ObserveScreen { align: center top; }
    .observe-pane {
        width: 100%; height: 1fr;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }
    .observe-info { width: 100%; height: 1; }
    .observe-input {
        width: 100%; height: auto; margin: 0 0 1 0; display: none;
    }
    .observe-input.interactive { display: block; }
    #broadcast-grid {
        width: 100%; height: 1fr;
        grid-size: 2;
        grid-gutter: 1 1;
        padding: 1;
    }
    .bcast-cell {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
        overflow-y: auto;
    }
    .bcast-cell-ok { border: round $success; }
    .bcast-cell-err { border: round $error; }
    #bcast-mode { width: 80%; content-align: center middle; }
    .tunnel-row { width: 100%; }
    .box-textarea { width: 100%; height: 1fr; }
    #send-text { width: 100%; height: 1fr; }
    """

    def __init__(
        self,
        config: Config | None = None,
        config_path: Path | None = None,
        start_rotation: str | None = None,
        launch_agent: bool = False,
        quick_launch: bool = False,
    ):
        super().__init__()
        self._config_path = config_path
        self._config = config
        self._start_rotation = start_rotation
        self._launch_agent = launch_agent
        self._quick_launch = quick_launch
        self.ssh_cfg: SshConfig | None = None
        self.tunnels = TunnelManager()

    def _localhost_host(self) -> Host:
        return (self._config.host("localhost") if self._config else None) or Host(
            alias="localhost",
            host="127.0.0.1",
            user=os.environ.get("USER", "user"),
            auth="",
            local=True,
        )

    def on_mount(self) -> None:
        self._load_config()
        if self._start_rotation:
            self._open_rotation(self._start_rotation)
        elif self._quick_launch:
            self.push_host_screen()
            self.push_screen(QuickLaunchScreen(self._localhost_host(), self._config or Config()))
        elif self._launch_agent:
            self.push_host_screen()
            self.push_screen(LaunchAgentScreen(self._localhost_host(), self._config or Config()))
        else:
            self.push_host_screen()

    def _tunnels_state_path(self) -> Path | None:
        """Percorso del file di stato dei tunnel (config.tunnels_file o default)."""
        if not self._config:
            return None
        from .ssh.tunnels import default_tunnels_path

        if self._config.tunnels_file:
            return Path(self._config.tunnels_file).expanduser()
        if self._config.path and self._config.path.parent:
            return self._config.path.parent / "tunnels.json"
        return default_tunnels_path()

    def _jump_for(self, host: Host) -> tuple[Host | None, str | None]:
        """Risolve il bastion (ProxyJump) dell'host: (jump_host, jump_password)."""
        if not self._config or not host.jump_host:
            return None, None
        jump = self._config.host(host.jump_host)
        if jump is None:
            return None, None
        return jump, self._password_for(jump)

    def _audit_for(self, host: Host, session: str | None = None) -> Path | None:
        """Percorso del log audit se audit_log attivo, altrimenti None."""
        if not self._config or not self._config.audit_log:
            return None
        from .ssh.audit import audit_log_path

        return audit_log_path(self._config, host.alias, session)

    def _open_rotation(self, name: str) -> None:
        from .rotation import load_rotations

        for r in load_rotations(self._config):
            if r.name == name:
                self.push_screen(
                    ObserveScreen(self._config, views=r.unique_entries(), interval=120, name=r.name)
                )
                return
        self.push_host_screen()

    def _load_config(self) -> None:
        if self._config is None:
            try:
                self._config = load_config(self._config_path)
            except Exception as exc:  # ConfigError
                self.notify(f"Errore di configurazione: {exc}", severity="error", timeout=8)
                self._config = Config()
        self.ssh_cfg = SshConfig(
            password_provider=lambda host: self._password_for(host),
            jump_resolver=lambda host: self._jump_for(host)[0],
        )
        self.tunnels.state_path = self._tunnels_state_path()

    def _password_for(self, host: Host) -> str | None:
        if not self._config:
            return None
        try:
            return resolve_password(self._config, host)
        except CredentialError:
            return None

    def _host_password_present(self, host: Host) -> bool:
        if not self._config:
            return False
        try:
            return password_present(self._config, host)
        except CredentialError:
            return False

    def _password_marks(self, hosts: list[Host]) -> dict[str, bool]:
        """Presenza password per host, da eseguire in un thread (mai sull'event loop).

        Un backend keyring bloccato non deve congelare la TUI: la chiamata viene
        fatta off-thread e, dopo il primo timeout, le successive falliscono subito.
        """
        marks: dict[str, bool] = {}
        if not self._config:
            return marks
        for host in hosts:
            try:
                marks[host.alias] = password_present(self._config, host)
            except CredentialError:
                marks[host.alias] = False
        return marks

    def _store_password(self, host: Host, secret: str) -> bool:
        try:
            store_password(self._config, host, secret)
            return True
        except (CredentialError, OSError) as exc:
            self.notify(f"Errore salvataggio password: {exc}", severity="error", timeout=8)
            return False

    def _clear_password(self, host: Host) -> None:
        try:
            clear_password(self._config, host)
        except (CredentialError, OSError) as exc:
            self.notify(f"Errore rimozione password: {exc}", severity="error", timeout=8)

    def _save_config(self) -> bool:
        """Salva la config su disco (con backup). True se ok."""
        try:
            backup_config(self._config)
            save_config(self._config)
            return True
        except OSError as exc:
            self.notify(f"Errore salvataggio config: {exc}", severity="error", timeout=8)
            return False

    def _hosts_changed(self) -> None:
        """Chiamato dopo ogni modifica agli host da form/confirm: salva ed esce."""
        if not self._save_config():
            return
        self.pop_screen()  # esce dalla schermata form/confirm
        # aggiorna la HostScreen sottostante se è la schermata corrente
        if isinstance(self.screen, HostScreen):
            self.screen.reload_hosts()

    def _persist_and_reload(self) -> None:
        """Salva la config e ricarica la lista host SENZA chiudere schermate."""
        if not self._save_config():
            return
        if isinstance(self.screen, HostScreen):
            self.screen.reload_hosts()

    def push_host_screen(self) -> None:
        self.push_screen(HostScreen(self._config or Config()))

    def open_sessions(self, host: Host) -> None:
        self.push_screen(SessionScreen(self._config or Config(), host))

    def request_launch(self, action: LaunchAction) -> None:
        """Esce dalla TUI (ripristina il terminale) e chiede l'azione SSH."""
        self.exit(action)

    def quit_to_shell(self) -> None:
        self.exit()


class BravoricScreen(Screen):
    """Base delle schermate: `self.app` è tipizzato come `BravoricApp`."""

    app = getters.app(BravoricApp)


class HostScreen(BravoricScreen):
    """Lista degli host configurati."""

    BINDINGS = [
        Binding("q", "quit", "Esci"),
        Binding("r", "refresh", "Ricarica"),
        Binding("a", "add_host", "Aggiungi"),
        Binding("e", "edit_host", "Modifica"),
        Binding("d", "delete_host", "Elimina"),
        Binding("D", "duplicate_host", "Duplica"),
        Binding("p", "password", "Password"),
        Binding("t", "test", "Test conn."),
        Binding("i", "import_ssh", "Import"),
        Binding("g", "cycle_group", "Gruppo"),
        Binding("T", "ping_all", "Ping tutti"),
        Binding("c", "scp", "Copia file"),
        Binding("F", "file_exchange", "Scambio file"),
        Binding("h", "recent", "Recenti"),
        Binding("o", "observe", "Osserva"),
        Binding("R", "rotations", "Rotazioni"),
        Binding("u", "tunnels", "Tunnel"),
        Binding("B", "broadcast", "Broadcast"),
        Binding("ctrl+shift+n", "launch_agent_localhost", "Avvia agente su localhost"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self._config = config
        self._all_hosts: list[Host] = config.hosts
        self._filter = ""
        self._group_filter: str | None = None  # None = tutti
        self._ping: dict[str, bool | None] = {}  # alias -> True/False/None
        self._groups: list[str] = []
        self._pw_cache: dict[str, bool] = {}  # alias -> password presente
        self._pw_loading = False
        self._recompute_groups()

    def _recompute_groups(self) -> None:
        seen: list[str] = []
        for h in self._all_hosts:
            g = (h.group or "").strip()
            if g and g not in seen:
                seen.append(g)
        self._groups = seen

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Host disponibili — Enter per aprire", classes="box-title")
        yield Input(placeholder="Filtro host… (per testo)", id="filter-input")
        yield ListView(id="host-list", classes="list")
        yield Label(
            "Enter: apri · a: aggiungi · e: modifica · d: elimina · D: duplica · p: password · t: test · "
            "i: import · g: gruppo · T: ping tutti · R: rotazioni · o: osserva · F: scambio file · "
            "u: tunnel · B: broadcast · r: ricarica · q: esci · Ctrl+Shift+N: avvia agente su localhost",
            classes="hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate(focus=True)

    def reload_hosts(self) -> None:
        """Ricaricata dalla config appena salvata."""
        self._all_hosts = self._config.hosts
        self._ping = {}
        self._pw_cache = {}
        self._recompute_groups()
        self._populate(focus=True)

    def _visible_hosts(self) -> list[Host]:
        hosts = self._all_hosts
        if self._group_filter:
            hosts = [h for h in hosts if (h.group or "").strip() == self._group_filter]
        if self._filter:
            f = self._filter.lower()
            hosts = [h for h in hosts if f in h.alias.lower() or f in h.host.lower()]
        return hosts

    def _populate(self, *, focus: bool = False) -> None:
        lv = self.query_one("#host-list", ListView)
        lv.clear()
        visible = self._visible_hosts()
        if not visible:
            if self._filter or self._group_filter:
                lv.append(ListItem(Label("[dim]Nessun host corrisponde al filtro[/dim]")))
            else:
                lv.append(ListItem(Label("Nessun host configurato. Premi a per aggiungerne uno.")))
        for h in visible:
            pw = self._pw(h)
            reach = self._ping_mark(h)
            group = f"  [dim][{h.group}][/dim]" if h.group else ""
            tmark = self._tunnel_mark(h)
            label = (
                f"{reach} {pw} {tmark} [b]{h.alias}[/b]  "
                f"[dim]{h.effective_user() or '?'}@{h.host}:{h.port}  auth={h.auth or 'default'}{group}[/dim]"
            )
            lv.append(ListItem(Label(label)))
        if focus:
            lv.focus()
        if not self._pw_loading and any(h.alias not in self._pw_cache for h in self._all_hosts):
            self._pw_loading = True
            self.run_worker(self._load_pw_marks(), thread=False, exclusive=True, group="pwmarks")

    async def _load_pw_marks(self) -> None:
        """Calcola in background l'indicatore password per gli host."""
        try:
            marks = await _io(self.app._password_marks, list(self._all_hosts))
        finally:
            self._pw_loading = False
        self._pw_cache.update(marks)
        if self.is_mounted:
            self._populate()

    def _pw(self, h: Host) -> str:
        """Indicatore password dallo cache (popolato in background, mai bloccante)."""
        return "🔑" if self._pw_cache.get(h.alias) else " "

    def _ping_mark(self, h: Host) -> str:
        st = self._ping.get(h.alias)
        if st is True:
            return "[green]✓[/green]"
        if st is False:
            return "[red]✗[/red]"
        return "[dim]·[/dim]"

    def _tunnel_mark(self, h: Host) -> str:
        """Semaforo tunnel: 🟢 attivo, 🔴 configurato ma spento, vuoto se nessuno."""
        if not h.tunnels:
            return ""
        active = self.app.tunnels.any_active(h.alias)
        if active:
            return "[green]🟢[/green]"
        return "[red]🔴[/red]"

    async def action_ping_all(self) -> None:
        """Ping TCP (in parallelo) su tutti gli host visibili e aggiorna gli indicatori."""
        import asyncio

        hosts = self._visible_hosts()
        if not hosts:
            return
        self._ping = {h.alias: None for h in hosts}
        self._populate()
        self.app.notify(f"Test di {len(hosts)} host…")
        results = await asyncio.gather(*(self._ping_one(h) for h in hosts))
        # results è lista di (alias, ok); aggiorna lo stato finale in un colpo
        for alias, ok in results:
            self._ping[alias] = ok
        self._populate()
        self.app.notify(
            f"Test completato: {sum(1 for _, ok in results if ok)}/{len(results)} raggiungibili"
        )

    async def _ping_one(self, h: Host) -> tuple[str, bool]:
        ok, _ = await _io(ssh_adapter.tcp_ping, h)
        return h.alias, ok

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-input":
            self._filter = event.value
            self._populate()

    def action_cycle_group(self) -> None:
        """Cicla: tutti -> gruppo1 -> gruppo2 -> ... -> tutti."""
        options = [None, *self._groups]
        if not self._groups:
            self.app.notify("Nessun gruppo configurato (usa il form per assegnarne)")
            return
        cur = self._group_filter
        idx = options.index(cur) if cur in options else 0
        self._group_filter = options[(idx + 1) % len(options)]
        if self._group_filter:
            self.app.notify(f"Gruppo: {self._group_filter}")
        else:
            self.app.notify("Gruppo: tutti")
        self._populate()

    def _selected_host(self) -> Host | None:
        lv = self.query_one("#host-list", ListView)
        visible = self._visible_hosts()
        if not visible:
            return None
        if lv.index is None or lv.index >= len(visible):
            return None
        return visible[lv.index]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        host = self._selected_host()
        if host:
            self.app.open_sessions(host)

    def action_add_host(self) -> None:
        self.app.push_screen(HostFormScreen(self._config, None))

    def action_edit_host(self) -> None:
        host = self._selected_host()
        if host:
            self.app.push_screen(HostFormScreen(self._config, host))
        else:
            self.app.notify("Nessun host selezionato")

    def action_delete_host(self) -> None:
        host = self._selected_host()
        if host:

            def do_delete() -> None:
                if host in self._config.hosts:
                    self._config.hosts.remove(host)
                self.app._hosts_changed()

            self.app.push_screen(
                ConfirmScreen(
                    f"Eliminare l'host '{host.alias}'?",
                    f"{host.effective_user()}@{host.host}:{host.port}",
                    do_delete,
                )
            )
        else:
            self.app.notify("Nessun host selezionato")

    def action_duplicate_host(self) -> None:
        host = self._selected_host()
        if not host:
            self.app.notify("Nessun host selezionato")
            return
        new_host = Host(
            alias=f"{host.alias}-copia",
            host=host.host,
            user=host.user,
            port=host.port,
            auth=host.auth,
            cred_key=host.cred_key,
        )
        self._config.hosts.append(new_host)
        self.app._persist_and_reload()

    def action_password(self) -> None:
        host = self._selected_host()
        if host:
            self.app.push_screen(PasswordScreen(self._config, host))
        else:
            self.app.notify("Nessun host selezionato")

    async def action_test(self) -> None:
        host = self._selected_host()
        if not host:
            self.app.notify("Nessun host selezionato")
            return
        self.app.notify(f"Test di {host.alias}…")
        ok, detail = await _io(ssh_adapter.tcp_ping, host)
        sev = "success" if ok else "error"
        self.app.notify(f"{host.alias}: {detail}", severity=sev, timeout=5)

    def action_scp(self) -> None:
        host = self._selected_host()
        if host:
            self.app.push_screen(ScpScreen(self._config, host))
        else:
            self.app.notify("Nessun host selezionato")

    def action_file_exchange(self) -> None:
        host = self._selected_host()
        if host:
            self.app.push_screen(SftpTargetScreen(self._config, host))
        else:
            self.app.notify("Nessun host selezionato")

    def action_recent(self) -> None:
        from .history import load_history

        entries = load_history(self._config)
        if not entries:
            self.app.notify("Nessuna sessione recente (ancora nessun attach)")
            return
        self.app.push_screen(RecentScreen(self._config, entries))

    def action_observe(self) -> None:
        host = self._selected_host()
        if host:
            self.app.push_screen(ObserveScreen(self._config, host))
        else:
            self.app.notify("Nessun host selezionato")

    def action_rotations(self) -> None:
        self.app.push_screen(RotationCatalogScreen(self._config))

    def action_tunnels(self) -> None:
        host = self._selected_host()
        if host:
            self.app.push_screen(TunnelScreen(self._config, host))
        else:
            self.app.notify("Nessun host selezionato")

    def action_broadcast(self) -> None:
        self.app.push_screen(SnippetCatalogScreen(self._config))

    def action_launch_agent_localhost(self) -> None:
        """Apre direttamente la schermata di lancio agente per localhost."""
        host = self._config.host("localhost")
        if not host:
            # Fallback se localhost non è presente nella config: crea istanza fittizia
            host = Host(
                alias="localhost",
                host="127.0.0.1",
                user=os.environ.get("USER", "user"),
                auth="",
                local=True,
            )
        self.app.push_screen(LaunchAgentScreen(host, self._config))

    def action_import_ssh(self) -> None:
        from .importers import import_from_remmina, import_from_ssh_config

        out = self._config.path or (Path.home() / ".config" / "bravoric-ssh-client" / "config.toml")
        total = 0
        added = 0
        # 1) da ~/.ssh/config
        ssh_cfg = Path.home() / ".ssh" / "config"
        if ssh_cfg.exists():
            try:
                t, a = import_from_ssh_config(
                    ssh_cfg, out, provider=self._config.credential_provider, merge=True
                )
                total += t
                added += a
            except Exception as exc:
                self.app.notify(f"Errore import ssh_config: {exc}", severity="error", timeout=8)
        # 2) da Remmina (flatpak + legacy)
        try:
            t, a = import_from_remmina(out, provider=self._config.credential_provider, merge=True)
            total += t
            added += a
        except Exception as exc:
            self.app.notify(f"Errore import Remmina: {exc}", severity="error", timeout=8)
        if total == 0:
            self.app.notify("Nessun host importabile", severity="error")
            return
        self._config.hosts = load_config(out).hosts
        self.reload_hosts()
        self.app.notify(f"Import completato: {total} host ({added} nuovi)")

    def action_refresh(self) -> None:
        try:
            self._config = load_config(self._config.path)
        except Exception as exc:
            self.app.notify(f"Errore: {exc}", severity="error")
            return
        self.reload_hosts()
        self.app.notify("Config ricaricata")

    def action_quit(self) -> None:
        self.app.exit()


class RecentScreen(BravoricScreen):
    """Sessioni tmux usate di recente: Enter per rientrarci."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
        Binding("a", "reopen_all", "Riapri tutte"),
    ]

    def __init__(self, config: Config, entries: list):
        super().__init__()
        self._config = config
        self._entries = entries

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Sessioni recenti — Enter per rientrare", classes="box-title")
        yield ListView(id="recent-list", classes="list")
        yield Label(
            "Enter: rientra · a: riapri tutte (finestre separate) · Esc/q: indietro", classes="hint"
        )
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#recent-list", ListView)
        for e in self._entries:
            lv.append(ListItem(Label(f"[b]{e.session}[/b]  [dim]{e.host}[/dim]")))
        if self._entries:
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:

        e = self._entries[event.list_view.index]
        host = self._config.host(e.host)
        if not host:
            self.app.notify(f"Host '{e.host}' non trovato in config", severity="error")
            return
        self.app.push_screen(SessionScreen(self._config, host))

    def action_reopen_all(self) -> None:
        """Riapre tutte le sessioni recenti (senza doppioni) in finestre separate,
        con un piccolo delay tra un terminale e l'altro per evitare sovraccarichi
        su macchine lente o server che applicano rate-limit/ban."""
        self.run_worker(self._reopen_all_worker(), thread=False)

    async def _reopen_all_worker(self) -> None:
        import asyncio

        unique: dict[str, tuple[str, str]] = {}
        for e in self._entries:
            if self._config.host(e.host):
                unique.setdefault(f"{e.host}/{e.session}", (e.host, e.session))
        if not unique:
            self.app.notify("Nessuna sessione da riaprire")
            return
        terminal = find_gui_terminal()
        wrap = str(Path.home() / ".local" / "bin" / "bravoric-ssh")
        launched = 0
        for host_alias, session in unique.values():
            if terminal:
                import subprocess

                try:
                    # sintassi per-terminale: vedi terminal_argv()
                    inner = f"exec {_sh_quote(wrap)} --attach {_sh_quote(host_alias)} {_sh_quote(session)}"
                    subprocess.Popen(
                        terminal_argv(terminal, inner),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched += 1
                    # Piccolo delay per non sovraccaricare macchine lente o server
                    if launched < len(unique):
                        await asyncio.sleep(0.5)
                except OSError as exc:
                    self.app.notify(f"Errore apertura {host_alias}: {exc}", severity="error")
            else:
                # fallback: esci e apri solo la prima
                self.app.notify("Nessun terminale GUI trovato; apri manualmente")
                break
        self.app.notify(f"Aperte {launched} finestre ({len(unique)} sessioni uniche)")

    def action_back(self) -> None:
        self.app.pop_screen()


class HostFormScreen(BravoricScreen):
    """Form per aggiungere (host=None) o modificare un host."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("ctrl+s", "save", "Salva"),
    ]

    def __init__(self, config: Config, host: Host | None):
        super().__init__()
        self._config = config
        self._host = host

    def compose(self) -> ComposeResult:
        mode = "Modifica" if self._host else "Nuovo host"
        yield Header()
        with Vertical(id="form-box"):
            yield Label(f"[b]{mode}[/b]", classes="box-title")
            yield Label("Alias:")
            yield Input(value=self._host.alias if self._host else "", id="f-alias")
            yield Label("Host (IP/hostname):")
            yield Input(value=self._host.host if self._host else "", id="f-host")
            yield Label("User:")
            yield Input(value=self._host.user or "" if self._host else "", id="f-user")
            yield Label("Porta (default 22):")
            yield Input(value=str(self._host.port) if self._host else "22", id="f-port")
            yield Label(f"Auth ({', '.join(a or 'default' for a in AUTH_METHODS)}):")
            yield Input(value=self._host.auth if self._host else "", id="f-auth")
            yield Label("cred_key (opzionale, default user@host):")
            yield Input(value=self._host.cred_key or "" if self._host else "", id="f-credkey")
            yield Label("Gruppo (opzionale):")
            yield Input(value=self._host.group or "" if self._host else "", id="f-group")
            yield Label("Jump host / bastion (alias, opzionale):")
            yield Input(
                value=self._host.jump_host or "" if self._host else "",
                placeholder="alias del bastion per ProxyJump",
                id="f-jumphost",
            )
            yield Label("Auto-rotate sessioni (sì/vero per abilitare):")
            yield Input(
                value="true" if (self._host and self._host.auto_cycle) else "",
                placeholder="vuoto = disabilitato",
                id="f-autocycle",
            )
            yield Label("Intervallo rotazione (secondi, default 120):")
            yield Input(
                value=str(self._host.cycle_interval) if self._host else "120",
                id="f-cycleinterval",
            )
            yield Label("Ctrl+S: salva  ·  Esc: annulla", classes="hint")
        yield Footer()

    def _read_fields(self) -> dict[str, str]:
        return {
            "alias": self.query_one("#f-alias", Input).value.strip(),
            "host": self.query_one("#f-host", Input).value.strip(),
            "user": self.query_one("#f-user", Input).value.strip(),
            "port": self.query_one("#f-port", Input).value.strip() or "22",
            "auth": self.query_one("#f-auth", Input).value.strip(),
            "credkey": self.query_one("#f-credkey", Input).value.strip(),
            "group": self.query_one("#f-group", Input).value.strip(),
            "jumphost": self.query_one("#f-jumphost", Input).value.strip(),
            "autocycle": self.query_one("#f-autocycle", Input).value.strip(),
            "cycleinterval": self.query_one("#f-cycleinterval", Input).value.strip() or "120",
        }

    def action_save(self) -> None:
        from .config import ConfigError, _parse_host

        f = self._read_fields()
        try:
            port = int(f["port"])
        except ValueError:
            self.app.notify("Porta non valida", severity="error")
            return
        auth = f["auth"]
        if auth not in AUTH_METHODS:
            self.app.notify(f"auth non valido: {auth}", severity="error")
            return
        raw = {
            "host": f["host"],
            "user": f["user"] or None,
            "port": port,
            "auth": auth,
        }
        if f["credkey"]:
            raw["cred_key"] = f["credkey"]
        if f["group"]:
            raw["group"] = f["group"]
        if f["jumphost"]:
            raw["jump_host"] = f["jumphost"]
        # auto-rotate
        ac = f["autocycle"].strip().lower()
        if ac in ("1", "true", "yes", "sì", "si", "on"):
            raw["auto_cycle"] = True
        elif ac in ("0", "false", "no", "off", "no "):
            raw["auto_cycle"] = False
        try:
            raw["cycle_interval"] = int(f["cycleinterval"])
        except ValueError:
            self.app.notify("Intervallo rotazione non valido", severity="error")
            return
        try:
            parsed = _parse_host(f["alias"], raw, {})
        except ConfigError as exc:
            self.app.notify(str(exc), severity="error")
            return
        # aggiorna o aggiunge
        if self._host is None:
            if self._config.host(parsed.alias):
                self.app.notify(f"Alias '{parsed.alias}' già esistente", severity="error")
                return
            self._config.hosts.append(parsed)
        else:
            idx = self._config.hosts.index(self._host)
            self._config.hosts[idx] = parsed
        self.app._hosts_changed()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class ConfirmScreen(BravoricScreen):
    """Conferma generica: titolo + messaggio + callback alla conferma."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("y", "confirm", "Conferma"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, title: str, message: str, on_confirm: Callable[[], None]):
        super().__init__()
        self._title = title
        self._message = message
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="confirm-box"):
            yield Label(f"[b]{self._title}[/b]", classes="box-title")
            yield Label(self._message)
            yield Label("Y: conferma  ·  N/Esc: annulla", classes="hint")
        yield Footer()

    def action_confirm(self) -> None:
        self._on_confirm()
        # pop solo se la callback non ha già navigato (es. _hosts_changed fa pop)
        if self.app.screen is self:
            self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class PasswordScreen(BravoricScreen):
    """Imposta/rimuove la password per un host nel provider."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("ctrl+s", "save", "Salva"),
        Binding("ctrl+d", "clear_pw", "Rimuovi"),
    ]

    def __init__(self, config: Config, host: Host):
        super().__init__()
        self._config = config
        self._host = host

    def compose(self) -> ComposeResult:
        has_pw = self.app._host_password_present(self._host)
        yield Header()
        with Vertical(id="pw-box"):
            yield Label(f"[b]Password per '{self._host.alias}'[/b]", classes="box-title")
            yield Label(
                f"auth={self._host.auth or 'default'}  ·  provider={self._config.credential_provider}"
            )
            yield Label("Password:")
            yield Input(password=True, placeholder="nuova password", id="pw-input")
            yield Label(
                "Stato attuale: "
                + ("[green]presente[/green]" if has_pw else "[yellow]assente[/yellow]")
            )
            yield Label("Ctrl+S: salva · Ctrl+D: rimuovi · Esc: annulla", classes="hint")
        yield Footer()

    def action_save(self) -> None:
        value = self.query_one("#pw-input", Input).value
        if not value:
            self.app.notify("Password vuota", severity="error")
            return
        if self.app._store_password(self._host, value):
            self.app.notify("Password salvata")
            self.app.pop_screen()

    def action_clear_pw(self) -> None:
        self.app._clear_password(self._host)
        self.app.notify("Password rimossa")
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class ScpScreen(BravoricScreen):
    """Copia file da/verso un host via scp (upload/download)."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("u", "do_upload", "Upload"),
        Binding("d", "do_download", "Download"),
    ]

    def __init__(self, config: Config, host: Host):
        super().__init__()
        self._config = config
        self._host = host

    def compose(self) -> ComposeResult:

        yield Header()
        with Vertical(id="pw-box"):
            yield Label(f"[b]Copia file — {self._host.alias}[/b]", classes="box-title")
            yield Label(f"{self._host.effective_user()}@{self._host.host}:{self._host.port}")
            yield Label("Percorso locale:")
            yield Input(placeholder="/percorso/locale/file", id="scp-local")
            yield Label("Percorso remoto:")
            yield Input(placeholder="/percorso/remoto/file", id="scp-remote")
            yield Label(
                "U: upload (locale→server) · D: download (server→locale) · Esc: annulla",
                classes="hint",
            )
        yield Footer()

    def _paths(self) -> tuple[str, str] | None:
        local = self.query_one("#scp-local", Input).value.strip()
        remote = self.query_one("#scp-remote", Input).value.strip()
        if not local or not remote:
            self.app.notify("Servono entrambi i percorsi", severity="error")
            return None
        return local, remote

    async def action_do_upload(self) -> None:
        from .ssh import file_ops

        paths = self._paths()
        if not paths:
            return
        local, remote = paths
        password = self.app._password_for(self._host)
        self.app.notify(f"Upload {local} → {remote}…")
        res = await self._run_in_thread(file_ops.upload, local, remote, password)
        self._report(res, "Upload")

    async def action_do_download(self) -> None:
        from .ssh import file_ops

        paths = self._paths()
        if not paths:
            return
        local, remote = paths
        password = self.app._password_for(self._host)
        self.app.notify(f"Download {remote} → {local}…")
        res = await self._run_in_thread(file_ops.download, remote, local, password)
        self._report(res, "Download")

    async def _run_in_thread(self, fn, *args):
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, self._host, *args)

    def _report(self, res, what: str) -> None:
        if res.ok:
            self.app.notify(f"{what} completato")
        else:
            self.app.notify(
                f"{what} fallito: {res.stderr.strip() or res.stdout.strip() or 'errore'}",
                severity="error",
                timeout=8,
            )

    def action_cancel(self) -> None:
        self.app.pop_screen()


class SftpTargetScreen(BravoricScreen):
    """Scegli il secondo lato dello scambio file: locale o un altro host."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
    ]

    def __init__(self, config: Config, host: Host):
        super().__init__()
        self._config = config
        self._host = host

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            f"Scambio file con '{self._host.alias}' — scegli il secondo lato", classes="box-title"
        )
        yield ListView(id="sftp-target-list", classes="list")
        yield Label("Enter: apri commander · Esc: indietro", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#sftp-target-list", ListView)
        lv.append(ListItem(Label("[b]Locale[/b]  [dim]questo computer[/dim]")))
        for h in self._config.hosts:
            if h.alias == self._host.alias:
                continue
            lv.append(
                ListItem(Label(f"[b]{h.alias}[/b]  [dim]{h.effective_user()}@{h.host}[/dim]"))
            )
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx == 0:
            target = Host(alias="locale", host="localhost", local=True)
        else:
            others = [h for h in self._config.hosts if h.alias != self._host.alias]
            target = others[idx - 1]
        self._launch(self._host, target)

    def _launch(self, host_a: Host, host_b: Host) -> None:
        from .ssh import commander

        if not commander.mc_available():
            self.app.notify(
                "Midnight Commander (mc) non installato: dnf install mc",
                severity="error",
                timeout=8,
            )
            return
        terminal = find_gui_terminal()
        wrap = str(Path.home() / ".local" / "bin" / "bravoric-ssh")
        if not terminal:
            self.app.notify("Nessun terminale GUI trovato; apri mc manualmente", severity="error")
            return
        # il comando va eseguito via shell: la forma esatta dipende dal terminale
        inner = f"exec {_sh_quote(wrap)} --sftp {_sh_quote(host_a.alias)} {_sh_quote(host_b.alias)}"
        try:
            subprocess.Popen(
                terminal_argv(terminal, inner),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.app.notify(f"Errore apertura mc: {exc}", severity="error")
            return
        self.app.notify(f"Apertura commander: {host_a.alias} ↔ {host_b.alias}")
        self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class TunnelScreen(BravoricScreen):
    """Gestione dei tunnel SSH (port forwarding) di un host."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
        Binding("n", "new", "Nuovo tunnel"),
        Binding("s", "start", "Avvia"),
        Binding("x", "stop", "Ferma"),
        Binding("a", "start_all", "Avvia tutti"),
        Binding("X", "stop_all", "Ferma tutti"),
        Binding("d", "delete", "Rimuovi"),
    ]

    def __init__(self, config: Config, host: Host):
        super().__init__()
        self._config = config
        self._host = host

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            f"Tunnel SSH di '{self._host.alias}' — s: avvia · x: ferma · n: nuovo · d: rimuovi",
            classes="box-title",
        )
        yield ListView(id="tunnel-list", classes="list")
        yield Label("Enter: nessuna · Esc: indietro", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        self._populate()

    def _populate(self) -> None:
        active = self.app.tunnels.active(self._host.alias)
        active_ports = {t.port for t in active}
        lv = self.query_one("#tunnel-list", ListView)
        lv.clear()
        if not self._host.tunnels:
            lv.append(
                ListItem(
                    Label("[dim]Nessun tunnel configurato. Premi n per aggiungerne uno.[/dim]")
                )
            )
        for t in self._host.tunnels:
            state = (
                "[green]🟢 attivo[/green]"
                if t.local_port in active_ports
                else "[red]🔴 spento[/red]"
            )
            detail = self._describe(t)
            lv.append(
                ListItem(Label(f"{state}  [b]{t.name or t.local_port}[/b]  [dim]{detail}[/dim]"))
            )
        if self._host.tunnels:
            lv.index = 0
        lv.focus()

    def _describe(self, t) -> str:

        if t.kind == "D":
            return f"SOCKS {t.local_addr()}"
        arrow = "→" if t.kind == "L" else "←"
        return f"{t.kind} {t.local_addr()} {arrow} {t.remote_host}:{t.remote_port}"

    def _selected_tunnel(self):

        lv = self.query_one("#tunnel-list", ListView)
        if not self._host.tunnels or lv.index is None or lv.index >= len(self._host.tunnels):
            return None
        return self._host.tunnels[lv.index]

    def action_new(self) -> None:
        self.app.push_screen(TunnelFormScreen(self._config, self._host))

    def action_start(self) -> None:
        t = self._selected_tunnel()
        if not t:
            self.app.notify("Nessun tunnel selezionato")
            return
        jump, jump_password = self.app._jump_for(self._host)
        ok, msg = self.app.tunnels.start(
            self._host,
            t,
            self.app._password_for(self._host),
            password_resolver=self.app._password_for,
            jump_host=jump,
        )
        self.app.notify(msg, severity="success" if ok else "error")
        self._populate()

    def action_start_all(self) -> None:
        jump, jump_password = self.app._jump_for(self._host)
        started = 0
        for t in self._host.tunnels:
            ok, _ = self.app.tunnels.start(
                self._host,
                t,
                self.app._password_for(self._host),
                password_resolver=self.app._password_for,
                jump_host=jump,
            )
            if ok:
                started += 1
        self.app.notify(f"Avviati {started}/{len(self._host.tunnels)} tunnel")
        self._populate()

    def action_stop(self) -> None:
        t = self._selected_tunnel()
        if not t:
            return
        if self.app.tunnels.stop(self._host.alias, t.local_port):
            self.app.notify(f"Tunnel {t.local_port} fermato")
        else:
            self.app.notify("Tunnel non attivo")
        self._populate()

    def action_stop_all(self) -> None:
        n = self.app.tunnels.stop_all(self._host.alias)
        self.app.notify(f"Fermati {n} tunnel")
        self._populate()

    def action_delete(self) -> None:
        t = self._selected_tunnel()
        if not t:
            return

        def do_delete() -> None:
            self.app.tunnels.stop(self._host.alias, t.local_port)
            if t in self._host.tunnels:
                self._host.tunnels.remove(t)
            self.app._persist_and_reload()
            self._populate()

        self.app.push_screen(
            ConfirmScreen(
                f"Rimuovere il tunnel '{t.name or t.local_port}'?",
                self._describe(t),
                do_delete,
            )
        )

    def action_back(self) -> None:
        self.app.pop_screen()


class TunnelFormScreen(BravoricScreen):
    """Form per aggiungere un tunnel: kind, porta locale, destinazione."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("ctrl+s", "save", "Salva"),
    ]

    def __init__(self, config: Config, host: Host):
        super().__init__()
        self._config = config
        self._host = host

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="form-box"):
            yield Label(f"[b]Nuovo tunnel su '{self._host.alias}'[/b]", classes="box-title")
            yield Label("Nome (opzionale):")
            yield Input(placeholder="es. postgres", id="t-name")
            yield Label("Tipo (L = locale, R = remoto, D = SOCKS):")
            yield Input(value="L", id="t-kind")
            yield Label("Porta locale (o porta SOCKS per D):")
            yield Input(placeholder="es. 5432", id="t-local")
            yield Label("Host destinazione (per L/R):")
            yield Input(placeholder="es. localhost", id="t-rhost")
            yield Label("Porta destinazione (per L/R):")
            yield Input(placeholder="es. 5432", id="t-rport")
            yield Label("Bind (default localhost):")
            yield Input(value="localhost", id="t-bind")
            yield Label("Ctrl+S: salva · Esc: annulla", classes="hint")
        yield Footer()

    def action_save(self) -> None:
        from .config import ConfigError, _parse_tunnel

        raw = {
            "name": self.query_one("#t-name", Input).value.strip(),
            "kind": self.query_one("#t-kind", Input).value.strip().upper() or "L",
            "local_port": self.query_one("#t-local", Input).value.strip(),
            "remote_host": self.query_one("#t-rhost", Input).value.strip(),
            "remote_port": self.query_one("#t-rport", Input).value.strip(),
            "bind": self.query_one("#t-bind", Input).value.strip() or "localhost",
        }
        try:
            tunnel = _parse_tunnel(raw)
        except ConfigError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self._host.tunnels.append(tunnel)
        self.app._persist_and_reload()
        self.app.pop_screen()
        self.app.notify(f"Tunnel aggiunto: {tunnel.local_addr()}")

    def action_cancel(self) -> None:
        self.app.pop_screen()


class WindowsScreen(BravoricScreen):
    """Elenca le finestre di una sessione: rinomina (r), chiude (k)."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("r", "rename_window", "Rinomina"),
        Binding("k", "kill_window", "Chiudi"),
    ]

    def __init__(self, host: Host, session: str):
        super().__init__()
        self._host = host
        self._session = session
        self._windows: list[str] = []
        self._loading = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            f"[b]Finestre di '{self._session}'[/b]  [dim]{self._host.alias}[/dim]",
            classes="host-info",
        )
        self._status = Static("…", id="status", classes="hint")
        yield self._status
        yield ListView(id="window-list", classes="list")
        yield Label("r: rinomina · k: chiudi · Esc: indietro", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_windows()

    def refresh_windows(self) -> None:
        self._loading = True
        self._update_status("Elencazione finestre…")
        self.run_worker(self._load_windows(), thread=False, exclusive=True)

    async def _load_windows(self) -> None:
        try:
            res = await _io(
                ssh_adapter.tmux_list_windows, self._host, self._session, self.app.ssh_cfg
            )
        except Exception as exc:
            self._loading = False
            self._update_status(f"Errore: {exc}", error=True)
            return
        self._loading = False
        if res.ok:
            self._windows = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        else:
            self._windows = []
            self._update_status(
                f"Errore: {res.stderr.strip() or 'elencazione fallita'}", error=True
            )
        self._render_windows()

    def _render_windows(self) -> None:
        lv = self.query_one("#window-list", ListView)
        lv.clear()
        if self._windows:
            self._update_status(f"{len(self._windows)} finestra/e")
            for w in self._windows:
                lv.append(ListItem(Label(f"[b]{w}[/b]")))
            lv.index = 0
        else:
            self._update_status("Nessuna finestra (usa W nella schermata sessioni per crearne una)")
        lv.focus()

    def _update_status(self, text: str, error: bool = False) -> None:
        if hasattr(self, "_status"):
            sev = "red" if error else "default"
            self._status.update(f"[{sev}]{text}[/]")

    def _selected_window(self) -> str | None:
        lv = self.query_one("#window-list", ListView)
        if not self._windows or lv.index is None or lv.index >= len(self._windows):
            return None
        return self._windows[lv.index]

    def action_rename_window(self) -> None:
        win = self._selected_window()
        if not win:
            self.app.notify("Nessuna finestra selezionata")
            return
        # estrae l'ID (prima cifra prima di ':' o spazio)
        win_id = win.split(":")[0].strip() if ":" in win else win.split()[0]
        self.app.push_screen(
            InputScreen("Rinomina finestra", "nuovo nome", self._rename_win(win_id))
        )

    def _rename_win(self, win_id: str):
        def do(new_name: str) -> None:
            self.run_worker(self._rename_win_async(win_id, new_name), thread=False, exclusive=True)

        return do

    async def _rename_win_async(self, win_id: str, new_name: str) -> None:
        self._update_status("Rinomino finestra…")
        try:
            res = await _io(
                ssh_adapter.tmux_rename_window,
                self._host,
                self._session,
                win_id,
                new_name,
                self.app.ssh_cfg,
            )
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify("Finestra rinominata")
            self.refresh_windows()
        else:
            self._update_status(f"Errore: {res.stderr.strip() or 'fallita'}", error=True)

    def action_kill_window(self) -> None:
        win = self._selected_window()
        if not win:
            self.app.notify("Nessuna finestra selezionata")
            return
        win_id = win.split(":")[0].strip() if ":" in win else win.split()[0]
        self.app.push_screen(
            ConfirmScreen(
                f"Chiudere la finestra '{win}'?",
                "Il processo nella finestra verrà terminato.",
                self._kill_win(win_id),
            )
        )

    def _kill_win(self, win_id: str):
        def do() -> None:
            self.run_worker(self._kill_win_async(win_id), thread=False, exclusive=True)

        return do

    async def _kill_win_async(self, win_id: str) -> None:
        self._update_status("Chiudo finestra…")
        try:
            res = await _io(
                ssh_adapter.tmux_kill_window, self._host, self._session, win_id, self.app.ssh_cfg
            )
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify("Finestra chiusa")
            self.refresh_windows()
        else:
            self._update_status(f"Errore: {res.stderr.strip() or 'fallita'}", error=True)

    def action_back(self) -> None:
        self.app.pop_screen()


class RotationCatalogScreen(BravoricScreen):
    """Elenco dei profili di rotazione salvati: avvia o riapri tutto."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
        Binding("n", "new", "Nuova"),
        Binding("a", "reopen_all", "Riapri tutte"),
        Binding("d", "delete", "Elimina"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self._config = config
        self._rotations: list = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Rotazioni salvate — Enter per osservare", classes="box-title")
        yield ListView(id="rotation-list", classes="list")
        yield Label(
            "Enter: osserva · n: nuova · a: riapri tutte · d: elimina · Esc: indietro",
            classes="hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        from .rotation import load_rotations

        self._rotations = load_rotations(self._config)
        lv = self.query_one("#rotation-list", ListView)
        lv.clear()
        if not self._rotations:
            lv.append(
                ListItem(Label("[dim]Nessuna rotazione salvata. Premi n per crearne una.[/dim]"))
            )
        for r in self._rotations:
            entries = r.unique_entries()
            detail = " · ".join(f"{h}/{s}" for h, s in entries[:3])
            more = f" +{len(entries) - 3}" if len(entries) > 3 else ""
            lv.append(ListItem(Label(f"[b]{r.name}[/b]  [dim]{detail}{more}[/dim]")))
        if self._rotations:
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not self._rotations:
            return
        r = self._rotations[event.list_view.index]
        self.app.push_screen(
            ObserveScreen(self._config, views=r.unique_entries(), interval=120, name=r.name)
        )

    def action_new(self) -> None:
        self.app.push_screen(RotationCreateScreen(self._config))

    def action_delete(self) -> None:
        if not self._rotations:
            return
        r = self._rotations[self.query_one("#rotation-list", ListView).index]
        self.app.push_screen(
            ConfirmScreen(
                f"Eliminare la rotazione '{r.name}'?",
                "Le sessioni tmux non vengono toccate.",
                lambda: self._delete(r.name),
            )
        )

    def _delete(self, name: str) -> None:
        from .rotation import remove_rotation

        remove_rotation(self._config, name)
        self.reload()
        self.app.notify(f"Rotazione '{name}' eliminata")

    def action_reopen_all(self) -> None:
        """Riapre tutte le sessioni di TUTTE le rotazioni salvate (senza doppioni),
        con un piccolo delay tra un terminale e l'altro."""
        self.run_worker(self._reopen_all_rotations_worker(), thread=False)

    async def _reopen_all_rotations_worker(self) -> None:
        import asyncio

        unique: dict[str, tuple[str, str]] = {}
        for r in self._rotations:
            for h, s in r.unique_entries():
                if self._config.host(h):
                    unique.setdefault(f"{h}/{s}", (h, s))
        if not unique:
            self.app.notify("Nessuna sessione da riaprire")
            return
        terminal = find_gui_terminal()
        wrap = str(Path.home() / ".local" / "bin" / "bravoric-ssh")
        launched = 0
        for host_alias, session in unique.values():
            if terminal:
                import subprocess

                try:
                    # sintassi per-terminale: vedi terminal_argv()
                    inner = f"exec {_sh_quote(wrap)} --attach {_sh_quote(host_alias)} {_sh_quote(session)}"
                    subprocess.Popen(
                        terminal_argv(terminal, inner),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched += 1
                    # Piccolo delay per non sovraccaricare macchine lente o server
                    if launched < len(unique):
                        await asyncio.sleep(0.5)
                except OSError as exc:
                    self.app.notify(f"Errore apertura {host_alias}: {exc}", severity="error")
            else:
                break
        self.app.notify(f"Aperte {launched} finestre ({len(unique)} sessioni uniche)")

    def action_back(self) -> None:
        self.app.pop_screen()


class RotationCreateScreen(BravoricScreen):
    """Launcher per creare una rotazione: spunta server, poi sessioni."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
        Binding("ctrl+s", "save", "Salva profilo"),
        Binding("s", "start", "Avvia ora"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self._config = config
        self._selected: dict[str, list[str]] = {}  # host_alias -> [sessions]
        self._sessions_cache: dict[str, list[str]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Crea rotazione — spunta i server, poi le sessioni", classes="box-title")
        yield Static("", id="rotation-status", classes="hint")
        yield ListView(id="rotation-host-list", classes="list")
        yield Label(
            "Invio: sessioni · Ctrl+S: salva profilo · s: avvia ora · Esc: indietro",
            classes="hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate_hosts()

    def _populate_hosts(self) -> None:
        lv = self.query_one("#rotation-host-list", ListView)
        lv.clear()
        for h in self._config.hosts:
            mark = "☑" if self._selected.get(h.alias) else "☐"
            lv.append(
                ListItem(
                    Label(f"{mark} [b]{h.alias}[/b]  [dim]{h.effective_user()}@{h.host}[/dim]")
                )
            )
        if self._config.hosts:
            lv.index = 0
        lv.focus()
        self._update_status()

    def _update_status(self) -> None:
        total = sum(len(v) for v in self._selected.values())
        self.query_one("#rotation-status", Static).update(
            f"[b]{total}[/b] sessione/i selezionate: "
            + ("; ".join(f"{h}({len(s)})" for h, s in self._selected.items() if s) or "nessuna")
        )

    async def _load_sessions(self, host: Host) -> list[str]:
        if host.alias in self._sessions_cache:
            return self._sessions_cache[host.alias]
        res = await _io(ssh_adapter.list_tmux_sessions, host, self.app.ssh_cfg)
        sessions = res.sessions if res.ok else []
        self._sessions_cache[host.alias] = sessions
        return sessions

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Seleziona/deseleziona un host; mostra le sessioni per la selezione."""
        host = self._config.hosts[event.list_view.index]
        # toggle dello stato dell'host
        if host.alias in self._selected:
            del self._selected[host.alias]
        else:
            sessions = await self._load_sessions(host)
            if not sessions:
                self.app.notify(f"Nessuna sessione su '{host.alias}'", severity="warning")
            self._selected[host.alias] = list(sessions)
        self._populate_hosts()
        # riapri la schermata sessioni del server per la selezione fine
        if host.alias in self._selected:
            self.app.push_screen(
                SessionPickScreen(self._config, host, self._selected[host.alias], self)
            )

    def action_start(self) -> None:
        views: list[tuple[str, str]] = []
        for h, sessions in self._selected.items():
            for s in sessions:
                views.append((h, s))
        if not views:
            self.app.notify("Seleziona almeno una sessione", severity="error")
            return
        self.app.push_screen(ObserveScreen(self._config, views=views, interval=120))

    def action_save(self) -> None:
        views = [(h, s) for h, sessions in self._selected.items() for s in sessions]
        if not views:
            self.app.notify("Seleziona almeno una sessione", severity="error")
            return

        self.app.push_screen(
            InputScreen(
                "Nome rotazione", "es. tutti-i-server", lambda name: self._save_named(name, views)
            )
        )

    def _save_named(self, name: str, views: list[tuple[str, str]]) -> None:
        from .rotation import Rotation, add_rotation

        add_rotation(self._config, Rotation(name=name, entries=views))
        self.app.notify(f"Rotazione '{name}' salvata ({len(views)} sessioni)")

    def action_back(self) -> None:
        self.app.pop_screen()


class SessionPickScreen(BravoricScreen):
    """Selezione fine delle sessioni di un host (checkbox per sessione)."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
        Binding("enter", "toggle_session", "Seleziona", priority=True),
    ]

    def __init__(
        self, config: Config, host: Host, sessions: list[str], creator: RotationCreateScreen
    ):
        super().__init__()
        self._config = config
        self._host = host
        self._sessions = sessions
        self._creator = creator

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            f"Sessioni di '{self._host.alias}' — Invio per selezionare/deselezionare",
            classes="box-title",
        )
        yield ListView(id="pick-list", classes="list")
        yield Label("Invio: seleziona/deseleziona · Esc: indietro", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#pick-list", ListView)
        selected = set(self._creator._selected.get(self._host.alias, []))
        for s in self._sessions:
            mark = "☑" if s in selected else "☐"
            lv.append(ListItem(Label(f"{mark} [b]{s}[/b]")))
        if self._sessions:
            lv.index = 0
        lv.focus()

    def action_toggle_session(self) -> None:
        lv = self.query_one("#pick-list", ListView)
        if lv.index is None or lv.index >= len(self._sessions):
            return
        session = self._sessions[lv.index]
        current = set(self._creator._selected.get(self._host.alias, []))
        if session in current:
            current.discard(session)
        else:
            current.add(session)
        self._creator._selected[self._host.alias] = sorted(current)
        # aggiorna la spunta nella riga corrente
        item = lv.children[lv.index]
        mark = "☑" if session in current else "☐"
        item.query_one(Label).update(f"{mark} [b]{session}[/b]")
        self._creator._populate_hosts()

    def action_back(self) -> None:
        self.app.pop_screen()


class ObserveScreen(BravoricScreen):
    """Osservazione automatica delle sessioni tmux (mono-host o multi-host).

    Mostra il contenuto della sessione corrente (capture-pane) e, se non riceve
    input per ``interval`` secondi, ruota automaticamente tra le ``views``
    (coppie host/sessione). Ogni input resetta il contatore e si ferma.
    """

    BINDINGS = [
        Binding("escape", "exit_observe", "Esci"),
        Binding("q", "exit_observe", "Esci"),
        Binding("n", "next", "Successiva"),
        Binding("p", "prev", "Precedente"),
        Binding("s", "toggle_rotate", "Rotazione"),
        Binding("R", "refresh_views", "Refresh"),
        Binding("g", "goto", "Vai a…"),
        Binding("enter", "attach", "Attach"),
        Binding("i", "interactive", "Interattiva"),
        Binding("ctrl+s", "send_no_enter", "Invia senza Invio", priority=True),
        Binding("ctrl+t", "toggle_info", "Info"),
    ]

    POLL_SECONDS = 1.5

    def __init__(
        self,
        config: Config,
        host: Host | None = None,
        views: list[tuple[str, str]] | None = None,
        *,
        interval: int | None = None,
        name: str | None = None,
    ):
        """``views`` = lista (host_alias, session). Se assente, usa l'host singolo."""
        super().__init__()
        self._config = config
        self._host = host
        self._views: list[tuple[str, str]] = views or []  # (host_alias, session)
        self._name = name
        self._current = 0
        self._rotating = True
        self._last_input = 0.0
        self._interval = max(5, int(interval or (host.cycle_interval if host else 120) or 120))
        self._show_info = True
        self._started = False
        self._capture_token = 0
        self._interactive = False
        self._send_buffer: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("…", id="observe-pane", classes="observe-pane", markup=False)
        yield Static("", id="observe-info", classes="hint")
        yield Input(
            placeholder="Interattiva: scrivi qui e premi Invio per inviare alla sessione",
            id="observe-input",
            classes="observe-input",
            disabled=True,
        )
        yield Footer()

    def on_mount(self) -> None:
        import time

        if self._name:
            self.title = f"Rotazione: {self._name}"
            self.sub_title = "osservazione"
            try:
                from .ssh.tmux_runner import _set_terminal_title

                _set_terminal_title(self._name)
            except Exception:
                pass
        self._last_input = time.monotonic()
        self._started = True
        self.refresh_views()
        self.run_worker(self._rotate_check(), thread=False, name="rotate")
        self.run_worker(self._poll_pane(), thread=False, name="poll")

    def _reset_timer(self) -> None:
        import time

        self._last_input = time.monotonic()
        self._rotating = False
        self._update_info()

    def on_key(self, event) -> None:
        if self._interactive and event.key == "escape":
            event.stop()
            self._exit_interactive()
            return
        if self._started and not self._interactive:
            self._reset_timer()

    def _exit_interactive(self) -> None:
        self._interactive = False
        inp = self.query_one("#observe-input", Input)
        inp.disabled = True
        inp.set_class(False, "interactive")
        inp.value = ""
        self._update_info()

    async def _rotate_check(self) -> None:
        import asyncio
        import time

        while self._started:
            await asyncio.sleep(1.0)
            if not self._started:
                break
            if self._rotating and self._views and not self._interactive:
                if time.monotonic() - self._last_input >= self._interval:
                    self._advance(1)
                    self._last_input = time.monotonic()
                    await self._capture()
            elif not self._rotating and self._views and not self._interactive:
                if time.monotonic() - self._last_input >= self._interval:
                    self._rotating = True
                    self._update_info()

    def refresh_views(self) -> None:
        self.run_worker(self._load_views_worker(), thread=False, exclusive=True)

    async def _load_views_worker(self) -> None:
        if not self._views and self._host:
            # modalità mono-host: elenca le sessioni dell'host
            res = await _io(ssh_adapter.list_tmux_sessions, self._host, self.app.ssh_cfg)
            if not res.ok:
                self.app.notify(f"Impossibile elencare sessioni: {res.error}", severity="error")
                return
            self._views = [(self._host.alias, s) for s in res.sessions]
        if not self._views:
            self.query_one("#observe-pane", Static).update(
                "[dim]Nessuna sessione da osservare[/dim]"
            )
            return
        if self._current >= len(self._views):
            self._current = 0
        await self._capture()

    def _view_host(self, host_alias: str) -> Host | None:
        return self._config.host(host_alias) or self._host

    def _advance(self, delta: int) -> None:
        if not self._views:
            return
        if self._interactive:
            self._interactive = False
            inp = self.query_one("#observe-input", Input)
            inp.disabled = True
            inp.set_class(False, "interactive")
            inp.value = ""
        self._current = (self._current + delta) % len(self._views)
        self._capture_token += 1
        self._show_current_stale()
        self._update_info()

    def _show_current_stale(self) -> None:
        """Mostra subito la vista corrente (placeholder) senza aspettare la cattura."""
        if not self._views:
            return
        host_alias, session = self._views[self._current]
        self.query_one("#observe-pane", Static).update(
            f"[dim]({host_alias}/{session} — caricamento…)[/dim]"
        )

    async def _capture(self) -> None:
        if not self._views:
            return
        token = self._capture_token
        host_alias, session = self._views[self._current]
        host = self._view_host(host_alias)
        if not host:
            self.query_one("#observe-pane", Static).update(
                f"[dim](host '{host_alias}' non trovato)[/dim]"
            )
            return
        res = await _io(
            ssh_adapter.tmux_capture_pane,
            host,
            session,
            self.app.ssh_cfg,
            lines=self._pane_lines(),
        )
        if token != self._capture_token:
            return  # risposta stantia: la vista è cambiata nel frattempo
        pane = self.query_one("#observe-pane", Static)
        if res.ok and res.stdout:
            pane.update(res.stdout)
        else:
            pane.update(f"[dim]({host_alias}/{session}: nessun contenuto o errore)[/dim]")
        self._update_info()

    def _pane_lines(self) -> int:
        try:
            return max(10, self.size.height - 6)
        except Exception:
            return 200

    def _update_info(self) -> None:
        if not self._started:
            return
        import time

        cur = self._views[self._current] if self._views else ("-", "-")
        remaining = max(0, self._interval - (time.monotonic() - self._last_input))
        mm, ss = divmod(int(remaining), 60)
        rotate = "ON" if self._rotating else "off"
        mode = "interattiva" if self._interactive else "osservazione"
        text = (
            f"[b]{cur[0]}/{cur[1]}[/b]  {self._current + 1}/{len(self._views)}  "
            f"modalità: {mode}  rotazione: {rotate}  prossima: {mm:02d}:{ss:02d}  "
            f"(n/p: cambia · s: rotazione · i: scrivi · Enter: attach · g: vai a · R: refresh · Esc: esci)"
        )
        self.query_one("#observe-info", Static).update(text)

    async def _poll_pane(self) -> None:
        import asyncio

        while self._started:
            interval = 0.4 if self._interactive else self.POLL_SECONDS
            await asyncio.sleep(interval)
            if self._started and self._views:
                await self._capture()

    def action_next(self) -> None:
        self._advance(1)
        self.run_worker(self._capture(), thread=False, exclusive=True)

    def action_prev(self) -> None:
        self._advance(-1)
        self.run_worker(self._capture(), thread=False, exclusive=True)

    def action_toggle_rotate(self) -> None:
        self._rotating = not self._rotating
        self._last_input = self._now()
        self._update_info()

    def _now(self) -> float:
        import time

        return time.monotonic()

    def action_goto(self) -> None:
        if not self._views:
            return
        suggestions = [f"{h}/{s}" for h, s in self._views]
        self.app.push_screen(
            InputScreen(
                "Vai a sessione",
                "host/sessione",
                self._goto_named,
                initial=suggestions[self._current],
                suggestions=suggestions,
            )
        )

    def _goto_named(self, name: str) -> None:
        for i, (h, s) in enumerate(self._views):
            if f"{h}/{s}" == name:
                if i != self._current:
                    self._current = i
                    self._capture_token += 1
                    self._show_current_stale()
                    self._update_info()
                self.run_worker(self._capture(), thread=False, exclusive=True)
                return

    def action_attach(self) -> None:
        if not self._views:
            return
        host_alias, session = self._views[self._current]
        host = self._view_host(host_alias)
        if host:
            self.app.request_launch(
                LaunchAction(kind="attach", host=host, session=session, rotation=self._name)
            )

    def action_interactive(self) -> None:
        """Toggle della modalità interattiva: scrivi nell'input e premi Invio."""
        if not self._views:
            self.app.notify("Nessuna sessione da cui interagire", severity="warning")
            return
        self._interactive = not self._interactive
        inp = self.query_one("#observe-input", Input)
        inp.disabled = not self._interactive
        inp.set_class(self._interactive, "interactive")
        if self._interactive:
            self._rotating = False
            inp.focus()
            self.app.notify(
                "Interattiva: scrivi e premi Invio (Ctrl+S per inviare senza Enter)", timeout=4
            )
        else:
            inp.value = ""
        self._update_info()

    async def _send_line(self, text: str, *, enter: bool) -> None:
        if not self._views:
            return
        host_alias, session = self._views[self._current]
        host = self._view_host(host_alias)
        if not host:
            return
        if not text and not enter:
            return
        self._capture_token += 1
        if text:
            await _io(ssh_adapter.tmux_send_keys, host, session, text, self.app.ssh_cfg)
        if enter:
            await _io(ssh_adapter.tmux_send_enter, host, session, self.app.ssh_cfg)
        await self._capture()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "observe-input":
            return
        text = event.value
        inp = self.query_one("#observe-input", Input)
        inp.value = ""
        self.run_worker(self._send_line(text, enter=True), thread=False)

    def action_send_no_enter(self) -> None:
        if not self._interactive:
            return
        inp = self.query_one("#observe-input", Input)
        text = inp.value
        inp.value = ""
        self.run_worker(self._send_line(text, enter=False), thread=False)

    def action_refresh_views(self) -> None:
        self.refresh_views()

    def action_toggle_info(self) -> None:
        self._show_info = not self._show_info
        self.query_one("#observe-info", Static).display = self._show_info

    def action_exit_observe(self) -> None:
        self._started = False
        self._interactive = False
        if self._name:
            try:
                from .ssh.tmux_runner import _set_terminal_title

                _set_terminal_title("bravoric-ssh-client")
            except Exception:
                pass
        self.app.pop_screen()


class InputScreen(BravoricScreen):
    """Raccoglie un input testuale (con eventuali suggerimenti) e chiama una callback."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("ctrl+s", "save", "Ok"),
        Binding("up", "suggest_prev", "Suggerimento ↑"),
        Binding("down", "suggest_next", "Suggerimento ↓"),
    ]

    def __init__(
        self,
        title: str,
        placeholder: str,
        on_submit: Callable[[str], None],
        *,
        initial: str = "",
        suggestions: list[str] | None = None,
    ):
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._on_submit = on_submit
        self._initial = initial
        self._suggestions = suggestions or []
        self._suggest_index = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="pw-box"):
            yield Label(f"[b]{self._title}[/b]", classes="box-title")
            yield Input(value=self._initial, placeholder=self._placeholder, id="text-input")
            if self._suggestions:
                yield ListView(id="suggestions", classes="suggest-list", disabled=True)
                yield Label("Frecce: suggerimenti · Ctrl+S: ok · Esc: annulla", classes="hint")
            else:
                yield Label("Ctrl+S: ok · Esc: annulla", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        if self._suggestions:
            lv = self.query_one("#suggestions", ListView)
            for s in self._suggestions:
                lv.append(ListItem(Label(s)))
            lv.index = 0

    def action_save(self) -> None:
        value = self.query_one("#text-input", Input).value.strip()
        if not value and self._suggestions:
            value = self._suggestions[0]
        if not value:
            self.app.notify("Valore vuoto", severity="error")
            return
        import asyncio
        import inspect

        result = self._on_submit(value)
        if inspect.isawaitable(result):
            asyncio.create_task(result)
        self.app.pop_screen()

    def action_suggest_prev(self) -> None:
        if not self._suggestions:
            return
        self._suggest_index = (self._suggest_index - 1) % len(self._suggestions)
        self._apply_suggestion()

    def action_suggest_next(self) -> None:
        if not self._suggestions:
            return
        self._suggest_index = (self._suggest_index + 1) % len(self._suggestions)
        self._apply_suggestion()

    def _apply_suggestion(self) -> None:
        if not self._suggestions:
            return
        value = self._suggestions[self._suggest_index]
        self.query_one("#text-input", Input).value = value
        try:
            self.query_one("#suggestions", ListView).index = self._suggest_index
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.app.pop_screen()


class InfoScreen(BravoricScreen):
    """Mostra un testo (dettagli sessione, elenco finestre...)."""

    BINDINGS = [
        Binding("escape", "close", "Chiudi"),
        Binding("q", "close", "Chiudi"),
    ]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="pw-box"):
            yield Label(f"[b]{self._title}[/b]", classes="box-title")
            yield Static(self._body or "(vuoto)", id="info-body")
            yield Label("Esc/q: chiudi", classes="hint")
        yield Footer()

    def action_close(self) -> None:
        self.app.pop_screen()


class PaneInfoScreen(BravoricScreen):
    """Mostra informazioni dettagliate sulla pane attiva di una sessione tmux.

    Visualizza processo, CWD, PID, titolo, geometria e flag is_shell.
    Si aggiorna automaticamente ogni 2 secondi. Usa `n`/`p` per cambiare
    sessione, `r` per refresh manuale, `Esc`/`q` per chiudere.
    """

    BINDINGS = [
        Binding("escape", "close", "Chiudi"),
        Binding("q", "close", "Chiudi"),
        Binding("n", "next", "Successiva"),
        Binding("p", "prev", "Precedente"),
        Binding("r", "refresh", "Refresh"),
    ]

    POLL_SECONDS = 2.0

    def __init__(self, host: Host, session: str, sessions: list[str] | None = None):
        super().__init__()
        self._host = host
        self._sessions = sessions or [session]
        self._current_idx = 0
        if session in self._sessions:
            self._current_idx = self._sessions.index(session)
        self._started = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="pw-box"):
            yield Label("[b]Info pane[/b]", classes="box-title")
            self._body = Static("Carico...", id="info-body")
            yield self._body
            yield Label(
                "n/p: cambia sessione · r: refresh · Esc/q: chiudi",
                classes="hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._started = True
        self.refresh_info()
        self.run_worker(self._poll_loop(), thread=False, name="pane-info-poll")

    @property
    def _current_session(self) -> str:
        if not self._sessions:
            return ""
        return self._sessions[self._current_idx]

    def refresh_info(self) -> None:
        self.run_worker(self._load_info(), thread=False, exclusive=True)

    async def _load_info(self) -> None:
        session = self._current_session
        if not session:
            self._body.update("[dim]nessuna sessione[/dim]")
            return
        try:
            info = await _io(ssh_adapter.tmux_pane_info, self._host, session, self.app.ssh_cfg)
        except Exception as exc:
            self._body.update(f"[red]Errore: {exc}[/red]")
            return
        if not info.ok:
            self._body.update(f"[red]Errore: {info.error}[/red]")
            return
        # Formattiamo le info in modo leggibile
        status = (
            "[green]idle (shell)[/green]" if info.is_shell else "[yellow]processo attivo[/yellow]"
        )
        lines = [
            f"[b]Sessione:[/b] {session}",
            f"[b]Processo:[/b] {info.command}  {status}",
            f"[b]CWD:[/b] {info.cwd or '(n/d)'}",
            f"[b]PID:[/b] {info.pid or '-'}",
            f"[b]Titolo:[/b] {info.title or '(nessuno)'}",
            f"[b]Geometria:[/b] {info.width}x{info.height}",
            "",
            f"[dim]Aggiornamento automatico ogni {int(self.POLL_SECONDS)}s[/dim]",
        ]
        self._body.update("\n".join(lines))

    async def _poll_loop(self) -> None:
        import asyncio

        while self._started:
            await asyncio.sleep(self.POLL_SECONDS)
            if self._started:
                self.refresh_info()

    def action_close(self) -> None:
        self._started = False
        self.app.pop_screen()

    def action_next(self) -> None:
        if len(self._sessions) <= 1:
            return
        self._current_idx = (self._current_idx + 1) % len(self._sessions)
        self.refresh_info()

    def action_prev(self) -> None:
        if len(self._sessions) <= 1:
            return
        self._current_idx = (self._current_idx - 1) % len(self._sessions)
        self.refresh_info()

    def action_refresh(self) -> None:
        self.refresh_info()


class QuickLaunchScreen(BravoricScreen):
    """Lancio rapido agente su localhost: griglia di bottoni, un bottone per agente."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("up", "nav_up", "Su", show=False, priority=True),
        Binding("down", "nav_down", "Giù", show=False, priority=True),
    ]

    CSS = """
    QuickLaunchScreen #ql-outer { width: 100%; align-horizontal: center; margin-top: 4; }
    QuickLaunchScreen #ql-box { width: 60; height: auto; padding: 1 2; border: round $primary; }
    QuickLaunchScreen #ql-grid { height: auto; }
    QuickLaunchScreen #ql-status { height: 1; color: $text-muted; margin-top: 1; text-align: center; }
    QuickLaunchScreen .box-title { width: 100%; text-align: center; }
    QuickLaunchScreen .ql-btn { margin: 0 0 1 0; width: 100%; }
    QuickLaunchScreen #btn-terminal { margin-top: 1; width: 100%; }
    """

    def __init__(self, host: Host, config: Config):
        super().__init__()
        self._host = host
        self._config = config
        self._agents_cfg: list[dict] = LaunchAgentScreen._load_agents_config()
        self._status = Static("Rilevamento agenti...", id="ql-status")

    def compose(self) -> ComposeResult:
        with Vertical(id="ql-outer"):
            with Vertical(id="ql-box"):
                yield Label("[b]Lancia agente[/b]", classes="box-title")
                with Vertical(id="ql-grid"):
                    for c in self._agents_cfg:
                        yield Button(
                            c["name"],
                            id=f"btn-agent-{c['name']}",
                            classes="ql-btn",
                            variant="primary",
                        )
                yield Button("Terminale", id="btn-terminal", variant="default")
                yield self._status
                yield Label("Esc: annulla", classes="hint")

    def on_mount(self) -> None:
        self.run_worker(self._detect_agents(), exclusive=True, group="detect")

    def action_nav_up(self) -> None:
        self.focus_previous()

    def action_nav_down(self) -> None:
        self.focus_next()

    async def _detect_agents(self) -> None:
        cfg = self.app.ssh_cfg
        q = _sh_quote
        names = [c["name"] for c in self._agents_cfg]
        grid = self.query_one("#ql-grid")
        if not names:
            self._status.update("[yellow]Nessun agente configurato[/]")
            self.query_one("#btn-terminal").focus()
            return
        check_cmd = (
            "for _a in "
            + " ".join(q(n) for n in names)
            + '; do command -v $_a >/dev/null 2>&1 && echo "FOUND:$_a"; done'
        )
        result = await _io(ssh_adapter.run_tmux_action, self._host, check_cmd, cfg)
        found = set()
        for line in (result.stdout or "").splitlines():
            if line.startswith("FOUND:"):
                found.add(line[6:].strip())
        available = [c["name"] for c in self._agents_cfg if c["name"] in found]
        if not available:
            self._status.update("[yellow]Nessun agente trovato[/]")
            self.query_one("#btn-terminal").focus()
            return
        all_names = [c["name"] for c in self._agents_cfg]
        for name in all_names:
            if name not in available:
                self.query_one(f"#btn-agent-{name}").remove()
        self.query_one(f"#btn-agent-{available[0]}").focus()
        self._status.update(f"Trovati: {', '.join(available)}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "btn-terminal":
            await self._do_launch(None)
        elif btn_id.startswith("btn-agent-"):
            await self._do_launch(btn_id[len("btn-agent-") :])

    async def _do_launch(self, agent: str | None) -> None:
        import re as _re

        label = agent or "terminale"
        self._status.update(f"Lancio {label}...")
        cfg = self.app.ssh_cfg
        q = _sh_quote
        base = _re.sub(r"[^A-Za-z0-9_-]+", "-", label)[:40].strip("-")
        slug = base
        for i in range(2, 100):
            has = await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                f"tmux has-session -t {q(slug)} 2>/dev/null",
                cfg,
            )
            if not has.ok:
                break
            slug = f"{base}-{i}"
        res = await _io(
            ssh_adapter.run_tmux_action,
            self._host,
            f"tmux new -d -s {q(slug)} 'exec bash -l'",
            cfg,
        )
        if not res.ok:
            self._status.update(f"[red]{res.stderr.strip() or 'errore sessione'}[/]")
            return
        if agent:
            await _io(
                ssh_adapter.tmux_send_input, self._host, slug, agent, cfg, enter=True, mode="keys"
            )
        self.app.notify(f"'{label}' in sessione '{slug}'", severity="information")
        self.app.request_launch(LaunchAction(kind="attach", host=self._host, session=slug))

    def action_cancel(self) -> None:
        self.app.pop_screen()


class LaunchAgentScreen(BravoricScreen):
    """Schermata per lanciare un agente AI in tmux detached.

    Rileva automaticamente gli agenti installati sull'host e, per quelli che
    lo supportano, carica la lista modelli disponibili via SSH.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("ctrl+s", "submit", "Lancia"),
        Binding("ctrl+r", "refresh", "Rileva agenti"),
    ]

    CSS = """
    LaunchAgentScreen #agent-form { overflow-y: auto; }
    LaunchAgentScreen #agent-form Input { margin: 0 0 1 0; }
    LaunchAgentScreen #agent-form TextArea { margin: 0 0 1 0; }
    LaunchAgentScreen #prompt { height: 4; }
    LaunchAgentScreen #timeout { width: 10; }
    LaunchAgentScreen #status-box { height: 1; width: 80%; }
    LaunchAgentScreen #output-box { display: none; }
    LaunchAgentScreen #model-section { display: none; }
    LaunchAgentScreen #model-section.visible { display: block; }
    LaunchAgentScreen #expert-label { color: $warning; text-style: bold; }
    LaunchAgentScreen.compact .opt-field { display: none; }
    LaunchAgentScreen.very-compact .opt-field-2 { display: none; }
    """

    def __init__(self, host: Host, config: Config):
        super().__init__()
        self._host = host
        self._config = config
        self._status_text = Static("Rilevamento agenti...", id="status-box", classes="hint")
        self._output = Static("", id="output-box")
        self._agents_cfg: list[dict] = self._load_agents_config()
        self._current_model_arg: str = ""

    @staticmethod
    def _load_agents_config() -> list[dict]:
        import tomllib
        from pathlib import Path as _Path

        cfg_path = _Path(__file__).parent / "agents_config.toml"
        try:
            with open(cfg_path, "rb") as f:
                return tomllib.load(f).get("candidates", [])
        except Exception:
            return []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="agent-form", classes="box"):
            yield Label("[b]Lancia agente AI[/b]", classes="box-title")
            yield Label(f"Host: {self._host.alias}")
            yield Label("Agente:")
            yield Select(
                [("Rilevamento in corso...", "__detecting__")],
                value="__detecting__",
                id="agent",
                allow_blank=False,
            )
            yield Label("Directory di lavoro:")
            yield Input(id="path", placeholder="Lascia vuoto per home")
            with Vertical(id="model-section"):
                yield Label("Modello:")
                yield Select(
                    [("— agente sceglie —", "")],
                    value="",
                    id="model",
                    allow_blank=False,
                )
            yield Label("Prompt iniziale (opzionale):")
            yield TextArea(id="prompt")
            yield Label("Timeout attesa (s):")
            yield Input(type="number", value="25", id="timeout", placeholder="5-120")
            yield Label("Titolo sessione (opzionale):", classes="opt-field")
            yield Input(id="title", placeholder="lascia vuoto per auto", classes="opt-field")
            yield Label("Force (ricrea se esiste):")
            yield Checkbox(value=False, id="force")
            yield Label("─── Expert ───────────────────", id="expert-label", classes="opt-field-2")
            yield Label(
                "Argomenti extra (override modello e prompt):",
                classes="opt-field-2",
            )
            yield Input(
                id="extra",
                placeholder="--model gpt-5 --flag ...",
                classes="opt-field-2",
            )
            yield Label("Ctrl+S: lancia · Ctrl+R: rileva · Esc: annulla", classes="hint")
        yield self._status_text
        yield Footer()

    def on_mount(self) -> None:
        self._apply_compact(self.size.height)
        self.run_worker(self._detect_agents(), exclusive=True, group="detect")

    def on_resize(self, event) -> None:
        self._apply_compact(event.size.height)

    def _apply_compact(self, height: int) -> None:
        if height < 26:
            self.add_class("compact")
        else:
            self.remove_class("compact")
        if height < 22:
            self.add_class("very-compact")
        else:
            self.remove_class("very-compact")

    async def _detect_agents(self) -> None:
        """SSH: controlla quali agenti candidati sono installati, popola il Select."""
        cfg = self.app.ssh_cfg
        q = _sh_quote
        names = [c["name"] for c in self._agents_cfg]
        if not names:
            self._status_text.update("[yellow]Nessun agente configurato in agents_config.toml[/]")
            return

        check_cmd = (
            "for _a in "
            + " ".join(q(n) for n in names)
            + '; do command -v $_a >/dev/null 2>&1 && echo "FOUND:$_a"; done'
        )
        result = await _io(ssh_adapter.run_tmux_action, self._host, check_cmd, cfg)
        found = set()
        for line in (result.stdout or "").splitlines():
            if line.startswith("FOUND:"):
                found.add(line[6:].strip())

        available = [c for c in self._agents_cfg if c["name"] in found]
        if not available:
            self._status_text.update("[yellow]Nessun agente trovato sul host[/]")
            return

        agent_select = self.query_one("#agent", Select)
        agent_select.set_options([(c["name"], c["name"]) for c in available])
        agent_select.value = available[0]["name"]
        agent_select.focus()
        self._status_text.update(f"Trovati: {', '.join(c['name'] for c in available)}")
        await self._load_models(available[0]["name"])

    async def _load_models(self, agent_name: str) -> None:
        """SSH: carica modelli per l'agente selezionato, aggiorna il Select modello."""
        cfg = self.app.ssh_cfg
        model_section = self.query_one("#model-section")
        model_section.remove_class("visible")
        self._current_model_arg = ""

        agent_cfg = next((c for c in self._agents_cfg if c["name"] == agent_name), None)
        if not agent_cfg or not agent_cfg.get("model_cmd", "").strip():
            return

        self._status_text.update(f"Caricamento modelli per {agent_name}...")
        result = await _io(ssh_adapter.run_tmux_action, self._host, agent_cfg["model_cmd"], cfg)
        models = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if not models:
            err = (result.stderr or "").strip().splitlines()
            hint = f": {err[-1][:60]}" if err else ""
            self._status_text.update(
                f"[dim]Nessun modello per {agent_name}{hint} — agente sceglie da solo[/]"
            )
            return

        self._current_model_arg = agent_cfg.get("model_arg", "")
        model_select = self.query_one("#model", Select)
        options = [("— agente sceglie —", "")] + [(m, m) for m in models]
        model_select.set_options(options)
        model_select.value = ""
        model_section.add_class("visible")
        self._status_text.update(f"Pronto — {len(models)} modelli disponibili")

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "agent" and event.value and event.value != Select.BLANK:
            await self._load_models(str(event.value))

    def action_refresh(self) -> None:
        self._status_text.update("Rilevamento agenti...")
        self.query_one("#model-section").remove_class("visible")
        self.run_worker(self._detect_agents(), exclusive=True, group="detect")

    async def action_submit(self) -> None:
        agent_val = self.query_one("#agent", Select).value
        if not agent_val or agent_val == Select.BLANK:
            self._status_text.update("[red]Seleziona un agente[/]")
            return
        agent = str(agent_val)

        path = self.query_one("#path", Input).value.strip()
        title = self.query_one("#title", Input).value.strip()
        extra = self.query_one("#extra", Input).value.strip()
        prompt = self.query_one("#prompt", TextArea).text.strip()
        timeout = int(self.query_one("#timeout", Input).value or 25)
        force = self.query_one("#force", Checkbox).value

        model_val = self.query_one("#model", Select).value
        model = str(model_val) if (model_val and model_val != Select.BLANK) else ""

        # Costruisci comando: agent [--model MODEL] [extra_args]
        # extra_args viene DOPO e fa override di tutto
        cmd_parts = [agent]
        if model and self._current_model_arg:
            cmd_parts += [self._current_model_arg, _sh_quote(model)]
        if extra:
            cmd_parts.append(extra)
        agent_cmd = " ".join(cmd_parts)

        self._status_text.update("Lancio agente in corso...")
        self._output.update("")

        await self._launch_agent(agent_cmd, path, title, "", prompt, timeout, force)

    async def _launch_agent(
        self,
        agent: str,
        path: str,
        title: str,
        extra_args: str,
        prompt: str,
        wait_timeout: int,
        force: bool,
    ) -> None:
        """Esegue il lancio richiamando le funzioni adapter, replicando MCP launch_agent."""
        import time as _time

        cfg = self.app.ssh_cfg
        q = _sh_quote
        started = _time.time()

        # agent è il comando completo (es. "opencode --model foo"); il binario è il primo token
        agent_bin = agent.split()[0] if agent else agent

        # Se path vuoto, usa la directory corrente della shell remota (default tmux)
        if path:
            pre = await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                f"bash -lc 'if [ -d {q(path)} ]; then echo PATH_OK; else echo PATH_MISSING; fi ; "
                f"if command -v {q(agent_bin)} >/dev/null 2>&1; then echo BIN_OK; "
                f"else echo BIN_MISSING; fi'",
                cfg,
            )
            pre_out = pre.stdout or ""
            if "PATH_MISSING" in pre_out:
                self._status_text.update(f"[red]Errore: directory inesistente: {path}[/]")
                return
        else:
            pre = await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                f"bash -lc 'if command -v {q(agent_bin)} >/dev/null 2>&1; then echo BIN_OK; else echo BIN_MISSING; fi'",
                cfg,
            )
            pre_out = pre.stdout or ""
            if "BIN_MISSING" in pre_out:
                self._status_text.update(
                    f"[red]Errore: binario '{agent_bin}' non trovato su {self._host.alias}[/]"
                )
                return

        # 2. Nome sessione (usa il nome binario, non il comando completo)
        sess_title = (
            title or f"{agent_bin}-{Path(path).name or 'agent'}"
            if path
            else title or f"{agent_bin}-sessione"
        )
        # slug sicuro
        import re

        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", sess_title.strip())
        slug = re.sub(r"-{2,}", "-", slug).strip("-_")[:40] or f"{agent_bin}-{_time.time():.0f}"
        win_title = (
            re.sub(r"[\x00-\x1f\x7f]", "", sess_title).replace(":", "-").replace(".", "-")[:50]
        )

        # 3. Gestione collisione sessione
        has = await _io(
            ssh_adapter.run_tmux_action,
            self._host,
            f"tmux has-session -t {q(slug)} 2>/dev/null",
            cfg,
        )
        if has.ok:
            if force:
                await _io(
                    ssh_adapter.run_tmux_action,
                    self._host,
                    f"tmux kill-session -t {q(slug)} 2>/dev/null",
                    cfg,
                )
            else:
                # Auto-trova nome libero: slug-2, slug-3, ...
                base_slug = slug
                found = False
                for i in range(2, 100):
                    candidate = f"{base_slug}-{i}"
                    has2 = await _io(
                        ssh_adapter.run_tmux_action,
                        self._host,
                        f"tmux has-session -t {q(candidate)} 2>/dev/null",
                        cfg,
                    )
                    if not has2.ok:
                        slug = candidate
                        win_title = candidate
                        found = True
                        break
                if not found:
                    self._status_text.update(
                        f"[red]Nessun nome libero per '{base_slug}' (prova Force)[/]"
                    )
                    return

        # 4. Crea sessione detached
        self._status_text.update("Creazione sessione tmux...")
        if path:
            res = await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                f"tmux new -d -s {q(slug)} -c {q(path)} 'exec bash -l'",
                cfg,
            )
        else:
            res = await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                f"tmux new -d -s {q(slug)} 'exec bash -l'",
                cfg,
            )
        if not res.ok:
            self._status_text.update(
                f"[red]Errore creazione sessione: {res.stderr.strip() or 'fallita'}[/]"
            )
            return

        # 5. Rinomina finestra (best-effort)
        if win_title:
            await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                f"tmux rename-window -t {q(slug + ':0')} {q(win_title)}",
                cfg,
            )

        # 6. Attendi che la shell sia pronta
        self._status_text.update("Attesa shell pronta...")
        settle_deadline = _time.time() + 3.0
        while _time.time() < settle_deadline:
            info = await _io(ssh_adapter.tmux_pane_info, self._host, slug, cfg)
            if info.ok and info.is_shell:
                break
            await _io(_time.sleep, 0.2)

        # 7. Invio comando agente (agent è già il comando completo con flags modello)
        cmd = f"{agent} {extra_args}".strip() if extra_args else agent
        self._status_text.update(f"Invio comando: {cmd}")
        send = await _io(
            ssh_adapter.tmux_send_input, self._host, slug, cmd, cfg, enter=True, mode="keys"
        )
        if not send.ok:
            self._status_text.update(
                f"[red]Errore invio comando: {send.stderr.strip() or 'fallito'}[/]"
            )
            return

        # 8. Polling caricamento TUI
        self._status_text.update(f"Attesa caricamento TUI (max {wait_timeout}s)...")
        probe_cmd = (
            f"tmux has-session -t {q(slug)} 2>/dev/null && echo HAS || echo NONE ; "
            f"echo __SEP__ ; "
            f"tmux display-message -p -t {q(slug)} '#{{pane_current_command}}' 2>/dev/null ; "
            f"echo __SEP__ ; "
            f"tmux capture-pane -p -t {q(slug)} -S -200 2>/dev/null"
        )
        deadline = _time.time() + wait_timeout
        last_screen: str | None = None
        stable_since: float | None = None
        non_shell_since: float | None = None
        saw_non_shell = False
        pane_command = ""
        screen = ""

        while True:
            if not self.is_mounted:
                return
            raw = (await _io(ssh_adapter.run_tmux_action, self._host, probe_cmd, cfg)).stdout or ""
            parts = raw.split("__SEP__")
            alive = bool(parts) and parts[0].strip().endswith("HAS")
            pane_command = parts[1].strip() if len(parts) > 1 else ""
            screen = parts[2].strip("\n") if len(parts) > 2 else ""
            now = _time.time()

            if not alive:
                self._status_text.update("[red]Sessione terminata inaspettatamente[/]")
                self._output.update(screen or "(nessun output)")
                return

            # Detect launch error
            err = self._detect_launch_error(screen)
            if err:
                self._status_text.update(f"[red]Errore avvio: {err}[/]")
                self._output.update(screen)
                return

            cmd_clean = ssh_adapter.clean_cmd(pane_command)
            if cmd_clean in ssh_adapter.SHELL_COMMANDS:
                if saw_non_shell:
                    self._status_text.update("[yellow]Agente terminato durante polling[/]")
                    self._output.update(screen)
                    return
                stable_since = None
                non_shell_since = None
            else:
                saw_non_shell = True
                if non_shell_since is None:
                    non_shell_since = now
                if screen != last_screen:
                    last_screen = screen
                    stable_since = now
                elif stable_since is None:
                    stable_since = now

                if (stable_since and now - stable_since >= 1.2) or (now - non_shell_since >= 8.0):
                    # TUI caricata
                    if prompt:
                        self._status_text.update("Invio prompt iniziale...")
                        ps = await _io(
                            ssh_adapter.tmux_send_input,
                            self._host,
                            slug,
                            prompt,
                            cfg,
                            enter=True,
                            mode="auto",
                            bracketed=True,
                        )
                        if not ps.ok:
                            self._status_text.update(
                                f"[red]Errore invio prompt: {ps.stderr.strip()}[/]"
                            )
                            return
                        await _io(_time.sleep, 2.0)
                        post = await _io(
                            ssh_adapter.tmux_capture_pane, self._host, slug, cfg, lines=200
                        )
                        screen = (post.stdout or screen).strip("\n")

                    elapsed = int((_time.time() - started) * 1000)
                    self._status_text.update(f"[green]Agente lanciato in {elapsed}ms[/]")
                    self._output.update(f"Sessione: {slug}\nProcesso: {pane_command}\n\n{screen}")
                    self.app.notify(
                        f"Agente '{agent}' lanciato in '{slug}'", severity="information"
                    )
                    # Esci dalla TUI e apri il terminale con la sessione tmux appena
                    # creata (con l'agente dentro), esattamente come quando si preme
                    # Enter su una sessione o si crea con `n` + nome + Ctrl+S.
                    if not self.is_mounted:
                        return
                    self.app.request_launch(
                        LaunchAction(kind="attach", host=self._host, session=slug)
                    )
                    return

            if now >= deadline:
                self._status_text.update(
                    f"[yellow]Timeout: TUI non caricata entro {wait_timeout}s[/]"
                )
                self._output.update(screen)
                return

            await _io(_time.sleep, 0.6)

    @staticmethod
    def _detect_launch_error(screen: str) -> str:
        """Ritorna la riga di errore d'avvio se presente, altrimenti ''."""
        for line in (screen or "").splitlines():
            low = line.lower()
            for pat in (
                "command not found",
                "no such file or directory",
                "permission denied",
                "cannot execute",
                "eacces",
                "is a directory",
                "traceback",
                "invalid api key",
                "not logged in",
                "authentication failed",
                "unauthorized",
                "login required",
            ):
                if pat in low:
                    return line.strip()
        return ""

    def action_cancel(self) -> None:
        self.app.pop_screen()


class SendTextScreen(BravoricScreen):
    """Schermata per inviare testo libero o codice (multiriga) alla sessione tmux."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Indietro"),
        Binding("ctrl+s", "send", "Invia (Ctrl+S)"),
    ]

    def __init__(self, host, session: str) -> None:
        super().__init__()
        self._host = host
        self._session = session
        self._status_text = ""

    def compose(self):
        yield Header()
        with Vertical(id="form-box"):
            yield Label(
                f"[b]Invia testo a '{self._session}'[/b]  [dim]{self._host.alias}[/dim]",
                classes="box-title",
            )
            yield Label(
                "Testo da inviare (multiriga supportato; inviato con bracketed paste):",
                classes="hint",
            )
            yield TextArea(id="send-text")
            yield Static(self._status_text, id="send-status", classes="hint")
            yield Label("Ctrl+S: invia · Esc: indietro", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#send-text", TextArea).focus()

    def _update_status(self, msg: str, error: bool = False) -> None:
        self._status_text = msg
        try:
            status = self.query_one("#send-status", Static)
            status.update(f"[red]{msg}[/red]" if error else msg)
        except Exception:
            pass

    def action_send(self) -> None:
        text = self.query_one("#send-text", TextArea).text
        if not text:
            self._update_status("Nessun testo da inviare", error=True)
            return
        self.run_worker(self._send(text), exclusive=True)

    async def _send(self, content: str) -> None:
        self._update_status(f"Invio {len(content)} caratteri…")
        try:
            res = await _io(
                ssh_adapter.tmux_send_input,
                self._host,
                self._session,
                content,
                self.app.ssh_cfg,
                enter=True,
                mode="auto",
                bracketed=True,
            )
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return

        if res.ok:
            self.app.notify(f"Testo inviato ({len(content)} char)")
            self.dismiss()
        else:
            self._update_status(f"Errore: {(res.stderr or '').strip() or 'fallito'}", error=True)


class SnippetCatalogScreen(BravoricScreen):
    """Catalogo snippet: scegli uno da eseguire in broadcast su più host."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
        Binding("n", "new", "Nuovo snippet"),
        Binding("d", "delete", "Elimina"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self._config = config
        self._snippets: list = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Snippet — Enter per eseguire su più host", classes="box-title")
        yield ListView(id="snippet-list", classes="list")
        yield Label("Enter: esegui · n: nuovo · d: elimina · Esc: indietro", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        from .snippets import load_snippets

        self._snippets = load_snippets(self._config)
        lv = self.query_one("#snippet-list", ListView)
        lv.clear()
        if not self._snippets:
            lv.append(ListItem(Label("[dim]Nessuno snippet. Premi n per crearne uno.[/dim]")))
        for s in self._snippets:
            desc = f"  [dim]{s.description}[/dim]" if s.description else ""
            lv.append(ListItem(Label(f"[b]{s.name}[/b]{desc}")))
        if self._snippets:
            lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not self._snippets:
            return
        s = self._snippets[event.list_view.index]
        self.app.push_screen(BroadcastHostPickerScreen(self._config, s))

    def action_new(self) -> None:
        self.app.push_screen(SnippetFormScreen(self._config))

    def action_delete(self) -> None:
        if not self._snippets:
            return
        s = self._snippets[self.query_one("#snippet-list", ListView).index]

        def do_delete() -> None:
            from .snippets import remove_snippet

            remove_snippet(self._config, s.name)
            self.reload()
            self.app.notify(f"Snippet '{s.name}' eliminato")

        self.app.push_screen(
            ConfirmScreen(f"Eliminare lo snippet '{s.name}'?", s.command, do_delete)
        )

    def action_back(self) -> None:
        self.app.pop_screen()


class SnippetFormScreen(BravoricScreen):
    """Form per aggiungere uno snippet: nome, descrizione, comando."""

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("ctrl+s", "save", "Salva"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="form-box"):
            yield Label("[b]Nuovo snippet[/b]", classes="box-title")
            yield Label("Nome:")
            yield Input(id="s-name")
            yield Label("Descrizione (opzionale):")
            yield Input(id="s-desc")
            yield Label("Comando (shell, eseguito su ogni host):")
            yield Input(id="s-cmd")
            yield Label("Ctrl+S: salva · Esc: annulla", classes="hint")
        yield Footer()

    def action_save(self) -> None:
        from .snippets import Snippet, add_snippet

        name = self.query_one("#s-name", Input).value.strip()
        cmd = self.query_one("#s-cmd", Input).value.strip()
        if not name or not cmd:
            self.app.notify("Nome e comando obbligatori", severity="error")
            return
        add_snippet(
            self._config,
            Snippet(
                name=name,
                command=cmd,
                description=self.query_one("#s-desc", Input).value.strip(),
            ),
        )
        self.app.pop_screen()
        self.app.notify(f"Snippet '{name}' salvato")
        if isinstance(self.app.screen, SnippetCatalogScreen):
            self.app.screen.reload()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class BroadcastHostPickerScreen(BravoricScreen):
    """Seleziona gli host (Space per marcare) ed esegui lo snippet in parallelo."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("space", "toggle", "Marca"),
        Binding("enter", "run", "Esegui", priority=True),
        Binding("a", "all", "Tutti"),
        Binding("t", "toggle_mode", "Modalità"),
    ]

    def __init__(self, config: Config, snippet):
        super().__init__()
        self._config = config
        self._snippet = snippet
        self._selected: set[str] = set()
        self._tmux_mode = True  # default: nuova sessione tmux per host

    def compose(self) -> ComposeResult:
        yield Header()
        mode = "in nuova sessione tmux" if self._tmux_mode else "diretta (ssh batch)"
        yield Label(
            f"Snippet: [b]{self._snippet.name}[/b] — modalità {mode} · Space marca, Enter esegue",
            id="bcast-mode",
            classes="box-title",
        )
        yield ListView(id="bcast-hosts", classes="list")
        yield Label(
            "Space: marca · a: tutti · t: modalità · Enter: esegui · Esc: indietro", classes="hint"
        )
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#bcast-hosts", ListView)
        for h in self._config.hosts:
            lv.append(
                ListItem(Label(f"[ ] [b]{h.alias}[/b]  [dim]{h.effective_user()}@{h.host}[/dim]"))
            )
        if self._config.hosts:
            lv.index = 0
        lv.focus()

    def _refresh_mode(self) -> None:
        mode = "in nuova sessione tmux" if self._tmux_mode else "diretta (ssh batch)"
        self.query_one("#bcast-mode", Label).update(
            f"Snippet: [b]{self._snippet.name}[/b] — modalità {mode} · Space marca, Enter esegue"
        )

    def action_toggle_mode(self) -> None:
        self._tmux_mode = not self._tmux_mode
        self._refresh_mode()

    def _refresh(self) -> None:
        lv = self.query_one("#bcast-hosts", ListView)
        for i, h in enumerate(self._config.hosts):
            if i >= len(lv.children):
                break
            mark = "[x]" if h.alias in self._selected else "[ ]"
            label = lv.children[i].children[0]
            label.update(f"{mark} [b]{h.alias}[/b]  [dim]{h.effective_user()}@{h.host}[/dim]")

    def action_toggle(self) -> None:
        lv = self.query_one("#bcast-hosts", ListView)
        if lv.index is None or lv.index >= len(self._config.hosts):
            return
        h = self._config.hosts[lv.index]
        if h.alias in self._selected:
            self._selected.discard(h.alias)
        else:
            self._selected.add(h.alias)
        self._refresh()

    def action_all(self) -> None:
        if len(self._selected) == len(self._config.hosts):
            self._selected.clear()
        else:
            self._selected = {h.alias for h in self._config.hosts}
        self._refresh()

    async def action_run(self) -> None:
        hosts = [h for h in self._config.hosts if h.alias in self._selected]
        if not hosts:
            self.app.notify("Nessun host marcato", severity="error")
            return
        self.app.push_screen(
            BroadcastResultScreen(
                self._config,
                hosts,
                self._snippet.name,
                self._snippet.command,
                mode="tmux" if self._tmux_mode else "direct",
            )
        )

    def action_back(self) -> None:
        self.app.pop_screen()


class BroadcastResultScreen(BravoricScreen):
    """Griglia dei risultati del broadcast: un riquadro per host.

    In modalità tmux mostra la sessione creata per host (da attachare per
    vedere l'output); in modalità diretta mostra stdout/stderr/exit code.
    """

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("q", "back", "Indietro"),
    ]

    def __init__(
        self,
        config: Config,
        hosts: list[Host],
        snippet_name: str,
        command: str,
        *,
        mode: str = "tmux",
    ):
        super().__init__()
        self._config = config
        self._hosts = hosts
        self._snippet_name = snippet_name
        self._command = command
        self._mode = mode  # "tmux" | "direct"
        self._results: list = []
        self._busy = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Broadcast in esecuzione…", id="bcast-status", classes="box-title")
        yield Grid(id="broadcast-grid")
        yield Label("Esc/q: indietro", classes="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._execute(), thread=False, exclusive=True)

    async def _execute(self) -> None:
        from .ssh.broadcast import run_snippet_on_hosts, run_snippet_on_hosts_tmux

        try:
            if self._mode == "tmux":
                self._results = await _io(
                    run_snippet_on_hosts_tmux,
                    self._hosts,
                    self._command,
                    self._snippet_name,
                    self.app.ssh_cfg,
                )
            else:
                self._results = await _io(
                    run_snippet_on_hosts,
                    self._hosts,
                    self._command,
                    self.app.ssh_cfg,
                )
        except Exception as exc:
            self.query_one("#bcast-status", Label).update(f"Errore broadcast: {exc}")
            self._busy = False
            return
        self._busy = False
        mode_txt = "in sessione tmux" if self._mode == "tmux" else "diretta"
        self.query_one("#bcast-status", Label).update(
            f"Broadcast '{self._snippet_name}' ({mode_txt}) — {len(self._results)} host"
        )
        grid = self.query_one("#broadcast-grid", Grid)
        for r in self._results:
            cls = "bcast-cell bcast-cell-ok" if r.ok else "bcast-cell bcast-cell-err"
            if self._mode == "tmux":
                if r.session_name:
                    body = (
                        f"[b]{r.host_alias}[/b]  [green]LANCIATA[/]\n"
                        f"sessione: [b]{r.session_name}[/b]\n"
                        "[dim]apri l'host per attach/monitorare[/dim]"
                    )
                elif r.error:
                    body = f"[b]{r.host_alias}[/b]  [red]ERRORE[/]\n{r.error}"
                else:
                    body = f"[b]{r.host_alias}[/b]  [red]FALLBACK diretto[/]\n{(r.stdout or '(nessun output)')[:200]}"
            else:
                body = r.stdout or ""
                if r.stderr:
                    body += f"\n[red](stderr)[/red]\n{r.stderr}"
                if not body.strip():
                    body = "(nessun output)"
                if r.error:
                    body = f"[red]{r.error}[/red]"
                body = f"[b]{r.host_alias}[/b]  [{cls and 'green' if r.ok else 'red'}]{'OK' if r.ok else f'EXIT {r.exit_code}'}[/]\n{body}"
            grid.mount(Static(body, classes=cls))

    def action_back(self) -> None:
        self.app.pop_screen()


class SessionScreen(BravoricScreen):
    """Sessioni tmux di un host: attach, crea, rename, kill, dettagli."""

    BINDINGS = [
        Binding("escape", "back", "Indietro"),
        Binding("n", "new_session", "Nuova"),
        Binding("r", "rename_session", "Rinomina"),
        Binding("k", "kill_session", "Kill"),
        Binding("K", "kill_server", "Kill server"),
        Binding("s", "shell", "Shell"),
        Binding("R", "attach_ro", "Attach RO"),
        Binding("d", "details", "Dettagli"),
        Binding("w", "windows", "Finestre"),
        Binding("W", "new_window", "Nuova finestra"),
        Binding("D", "detach_clients", "Detach client"),
        Binding("g", "refresh", "Aggiorna"),
        Binding("i", "pane_info", "Info pane"),
        Binding("a", "launch_agent", "Lancia agente"),
        Binding("P", "send_text", "Invia testo"),
        Binding("F", "send_file", "Invia file"),
        Binding("y", "copy_buffer", "Copia buffer"),
        Binding("q", "quit", "Esci"),
    ]

    def __init__(self, config: Config, host: Host):
        super().__init__()
        self._config = config
        self._host = host
        self._sessions: list[str] = []
        self._loading = True
        self._tmux_present = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(
            f"[b]{self._host.alias}[/b]  [dim]{self._host.effective_user()}@{self._host.host}:{self._host.port}[/dim]",
            classes="host-info",
        )
        self._status = Static("…", id="status", classes="hint")
        yield self._status
        yield ListView(id="session-list", classes="list")
        yield Label(
            "Enter: attach · R: attach RO · n: nuova · r: rinomina · k: kill · K: kill server · "
            "d: dettagli · i: info pane · a: lancia agente · P: invia testo · F: invia file · "
            "y: copia buffer · w: finestre · D: detach · g: aggiorna · s: shell · Esc: indietro",
            classes="hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_sessions()

    def refresh_sessions(self) -> None:
        """Elenca le sessioni in un thread, senza bloccare la TUI."""
        self._loading = True
        self._update_status("Elencazione sessioni…")
        self.run_worker(self._load_sessions(), thread=False, exclusive=True)

    async def _load_sessions(self) -> None:
        try:
            res = await _io(ssh_adapter.list_tmux_sessions, self._host, self.app.ssh_cfg)
            self._loading = False
            if res.ok:
                self._sessions = res.sessions
                self._tmux_present = True
            else:
                # distinguiamo "tmux assente" da "errore di rete/auth"
                if "command not found" in res.error or "not found" in res.error:
                    self._tmux_present = False
                    self._sessions = []
                    self._update_status(f"tmux non presente sul server ({res.error})", error=True)
                else:
                    self._tmux_present = True
                    self._sessions = []
                    self._update_status(f"Impossibile elencare: {res.error}", error=True)
                    return
        except Exception as exc:
            self._loading = False
            self._update_status(f"Errore: {exc}", error=True)
            return
        self._render_sessions()

    def _render_sessions(self) -> None:
        lv = self.query_one("#session-list", ListView)
        lv.clear()
        if self._sessions:
            self._update_status(f"{len(self._sessions)} sessione/i")
            for s in self._sessions:
                lv.append(ListItem(Label(f"[b]{s}[/b]")))
            lv.index = 0  # evidenzia la prima sessione: Enter aggancia subito
        else:
            if self._tmux_present:
                self._update_status("Nessuna sessione attiva. Premi n per crearne una.")
            else:
                self._update_status("tmux non installato sul server.")
        lv.focus()

    def _update_status(self, text: str, error: bool = False) -> None:
        if hasattr(self, "_status"):
            sev = "red" if error else "default"
            self._status.update(f"[{sev}]{text}[/]")

    def _selected_session(self) -> str | None:
        lv = self.query_one("#session-list", ListView)
        if not self._sessions or lv.index is None or lv.index >= len(self._sessions):
            return None
        return self._sessions[lv.index]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._loading:
            return
        if self._sessions:
            name = self._sessions[event.list_view.index]
            self._do_attach(name)
        else:
            self.action_new_session()

    def _do_attach(self, name: str) -> None:
        self.app.request_launch(LaunchAction(kind="attach", host=self._host, session=name))

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_new_session(self) -> None:
        from .history import load_history

        # suggerimenti: sessioni recenti di questo host + alias
        suggestions = [self._host.alias]
        for e in load_history(self._config):
            if e.host == self._host.alias and e.session not in suggestions:
                suggestions.append(e.session)
        self.app.push_screen(
            InputScreen(
                "Nuova sessione",
                "nome sessione (frecce per i suggerimenti)",
                self._new_named,
                initial=self._host.alias,
                suggestions=suggestions,
            )
        )

    def _new_named(self, name: str) -> None:
        self.app.request_launch(LaunchAction(kind="new", host=self._host, name=name))

    def action_shell(self) -> None:
        self.app.request_launch(LaunchAction(kind="shell", host=self._host))

    def action_attach_ro(self) -> None:
        name = self._selected_session()
        if name:
            self.app.request_launch(LaunchAction(kind="attach_ro", host=self._host, session=name))
        else:
            self.app.notify("Nessuna sessione selezionata")

    def action_rename_session(self) -> None:
        old = self._selected_session()
        if not old:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.app.push_screen(
            InputScreen("Rinomina sessione", "nuovo nome", self._rename, initial=old)
        )

    async def _rename(self, new_name: str) -> None:
        old = self._selected_session()
        if not old:
            return
        self._update_status(f"Rinomino '{old}' → '{new_name}'…")
        try:
            res = await _io(
                ssh_adapter.tmux_rename_session, self._host, old, new_name, self.app.ssh_cfg
            )
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify(f"Sessione rinominata in '{new_name}'")
            self.refresh_sessions()
        else:
            self._update_status(
                f"Errore: {res.stderr.strip() or res.stdout.strip() or 'fallita'}", error=True
            )

    def action_kill_session(self) -> None:
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.app.push_screen(
            ConfirmScreen(
                f"Terminare la sessione '{name}'?",
                "Le finestre della sessione verranno chiuse.",
                lambda: self._kill(name),
            )
        )

    def _kill(self, name: str) -> None:
        self.run_worker(self._kill_async(name), thread=False, exclusive=True)

    async def _kill_async(self, name: str) -> None:
        self._update_status(f"Termino '{name}'…")
        try:
            res = await _io(ssh_adapter.tmux_kill_session, self._host, name, self.app.ssh_cfg)
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify(f"Sessione '{name}' terminata")
            self.refresh_sessions()
        else:
            self._update_status(
                f"Errore: {res.stderr.strip() or res.stdout.strip() or 'fallita'}", error=True
            )

    def action_kill_server(self) -> None:
        self.app.push_screen(
            ConfirmScreen(
                "Terminare TUTTE le sessioni del server?",
                f"Tutte le sessioni tmux su {self._host.alias} verranno chiuse.",
                self._kill_server,
            )
        )

    def _kill_server(self) -> None:
        self.run_worker(self._kill_server_async(), thread=False, exclusive=True)

    async def _kill_server_async(self) -> None:
        self._update_status("Termino il server…")
        try:
            res = await _io(ssh_adapter.tmux_kill_server, self._host, self.app.ssh_cfg)
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify("Server tmux terminato")
            self.refresh_sessions()
        else:
            self._update_status(
                f"Errore: {res.stderr.strip() or res.stdout.strip() or 'fallita'}", error=True
            )

    def action_detach_clients(self) -> None:
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.run_worker(self._detach_async(name), thread=False, exclusive=True)

    async def _detach_async(self, name: str) -> None:
        self._update_status(f"Stacco client da '{name}'…")
        try:
            res = await _io(ssh_adapter.tmux_detach_clients, self._host, name, self.app.ssh_cfg)
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify(f"Altri client staccati da '{name}'")
        else:
            self._update_status(
                f"Errore: {res.stderr.strip() or res.stdout.strip() or 'fallita'}", error=True
            )

    def action_copy_buffer(self) -> None:
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.run_worker(self._copy_buffer_async(name), thread=False, exclusive=True)

    async def _copy_buffer_async(self, name: str) -> None:
        self._update_status(f"Leggo buffer di '{name}'…")
        try:
            res = await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                "tmux show-buffer",
                self.app.ssh_cfg,
            )
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if not res.ok:
            self._update_status(
                f"Buffer vuoto o errore: {res.stderr.strip() or 'nessun buffer'}", error=True
            )
            return
        content = res.stdout
        try:
            import subprocess as _sp

            _sp.run(["wl-copy"], input=content, text=True, check=True)
            self.app.notify(f"Buffer di '{name}' copiato negli appunti ({len(content)} car.)")
            self._update_status(f"Buffer copiato ({len(content)} caratteri)")
        except FileNotFoundError:
            self._update_status("wl-copy non trovato — installa wl-clipboard", error=True)
        except Exception as exc:
            self._update_status(f"Errore copia locale: {exc}", error=True)

    def action_details(self) -> None:
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.run_worker(self._details_async(name), thread=False, exclusive=True)

    async def _details_async(self, name: str) -> None:
        self._update_status(f"Dettagli di '{name}'…")
        try:
            res = await _io(ssh_adapter.tmux_session_details, self._host, name, self.app.ssh_cfg)
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.push_screen(
                InfoScreen(
                    f"Dettagli sessione '{name}'", res.stdout.strip() or "(nessun dettaglio)"
                )
            )
        else:
            self._update_status(
                f"Errore: {res.stderr.strip() or res.stdout.strip() or 'fallita'}", error=True
            )

    def action_windows(self) -> None:
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.app.push_screen(WindowsScreen(self._host, name))

    def action_pane_info(self) -> None:
        """Apri la schermata con informazioni dettagliate sulla pane attiva."""
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.app.push_screen(PaneInfoScreen(self._host, name, sessions=self._sessions))

    def action_launch_agent(self) -> None:
        """Apri la schermata per lanciare un agente AI in una sessione tmux."""
        self.app.push_screen(LaunchAgentScreen(self._host, self._config))

    def action_send_text(self) -> None:
        """Apre una schermata per inviare testo (anche multiriga) alla pane selezionata."""
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.app.push_screen(SendTextScreen(self._host, name))

    def action_send_file(self) -> None:
        """Invia il contenuto di un file locale alla pane selezionata (bracketed paste)."""
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.app.push_screen(
            InputScreen(
                "Invia file alla pane",
                "percorso file locale (es. ./patch.diff)",
                lambda path: self._send_file_to(name, path),
            )
        )

    def _send_file_to(self, session: str, path: str) -> None:
        self.run_worker(self._send_file_async(session, path), thread=False, exclusive=True)

    async def _send_file_async(self, session: str, path: str) -> None:
        p = Path(path).expanduser()
        if not p.is_file():
            self.app.notify(f"File non trovato: {p}", severity="error")
            return
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.app.notify(f"Errore lettura file: {exc}", severity="error")
            return
        self._update_status(f"Invio {len(content)} caratteri a '{session}'…")
        try:
            res = await _io(
                ssh_adapter.tmux_paste_buffer, self._host, session, content, self.app.ssh_cfg
            )
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify(f"File inviato ({len(content)} char) a '{session}'")
        else:
            self._update_status(f"Errore: {(res.stderr or '').strip() or 'fallito'}", error=True)

    def action_new_window(self) -> None:
        name = self._selected_session()
        if not name:
            self.app.notify("Nessuna sessione selezionata")
            return
        self.app.push_screen(
            InputScreen(
                "Nuova finestra", "nome finestra (opzionale)", self._new_window_in, initial=""
            )
        )

    def _new_window_in(self, window_name: str) -> None:
        session = self._selected_session()
        if not session:
            return
        self.run_worker(self._new_window_async(session, window_name), thread=False, exclusive=True)

    async def _new_window_async(self, session: str, window_name: str) -> None:
        self._update_status("Creo finestra…")
        try:
            res = await _io(
                ssh_adapter.tmux_new_window,
                self._host,
                session,
                window_name or None,
                self.app.ssh_cfg,
            )
        except Exception as exc:
            self._update_status(f"Errore: {exc}", error=True)
            return
        if res.ok:
            self.app.notify("Finestra creata")
            self.refresh_sessions()
        else:
            self._update_status(f"Errore: {res.stderr.strip() or 'fallita'}", error=True)

    def action_refresh(self) -> None:
        self.refresh_sessions()

    def action_quit(self) -> None:
        self.app.exit()


def _sftp_cli(config, host_a_ref: str, host_b_ref: str) -> None:
    """Apre Midnight Commander sui due host (alias o 'local'/locale)."""
    from .ssh import commander

    if not commander.mc_available():
        print("Midnight Commander (mc) non installato: dnf install mc", file=sys.stderr)
        return

    def _resolve(ref: str):
        ref = ref.strip().lower()
        if ref in ("local", "locale"):
            return Host(alias="locale", host="localhost", local=True)
        host = config.host(ref)
        if not host:
            print(f"Host '{ref}' non trovato in config", file=sys.stderr)
            return None
        return host

    host_a = _resolve(host_a_ref)
    if host_a is None:
        return
    host_b = _resolve(host_b_ref)
    if host_b is None:
        return

    env, helper = commander.build_env(
        config, host_a, host_b, password_resolver=app_password_resolver(config)
    )
    try:
        commander.launch_commander(host_a, host_b, env)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)


def app_password_resolver(config):
    """Resolver password da usare per il commander (keyring)."""
    from .credentials.factory import resolve_password

    def resolver(host: Host) -> str | None:
        return resolve_password(config, host)

    return resolver


def _cli_app(config_path):
    """Crea un BravoricApp con la config caricata (pattern dei blocchi CLI di main)."""
    app = BravoricApp(config_path=config_path)
    if app._config is None:
        app._load_config()
    return app


def _cli_resolve_host(app, ref: str) -> Host:
    """Risolve un host per alias; errore descrittivo su stderr + exit 1 se assente."""
    host = app._config.host(ref) if app._config else None
    if not host:
        print(f"Host '{ref}' non trovato in config", file=sys.stderr)
        sys.exit(1)
    return host


def _list_hosts_cli(config_path) -> None:
    """--list-hosts: stampa JSON di tutti gli host configurati."""
    app = _cli_app(config_path)
    cfg = app._config
    out = []
    for h in cfg.hosts:
        out.append(
            {
                "alias": h.alias,
                "host": h.host,
                "user": h.user,
                "port": h.port,
                "auth": h.auth,
                "group": h.group,
                "jump_host": h.jump_host,
                "auto_cycle": h.auto_cycle,
                "cycle_interval": h.cycle_interval,
                "local": bool(h.is_local()),
                "tunnels": [
                    {
                        "name": t.name,
                        "kind": t.kind,
                        "local_port": t.local_port,
                        "remote_host": t.remote_host,
                        "remote_port": t.remote_port,
                        "bind": t.bind,
                    }
                    for t in h.tunnels
                ],
                "password_present": app._host_password_present(h),
            }
        )
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return


def _list_sessions_cli(config_path, host_ref: str) -> None:
    """--list-sessions <host>: stampa JSON delle sessioni tmux dell'host."""
    app = _cli_app(config_path)
    host = _cli_resolve_host(app, host_ref)
    res = ssh_adapter.list_tmux_sessions(host, app.ssh_cfg)
    print(
        json.dumps(
            {"ok": res.ok, "sessions": res.sessions, "error": res.error},
            indent=2,
            ensure_ascii=False,
        )
    )
    if not res.ok:
        sys.exit(1)
    return


def _ping_cli(config_path, host_ref: str) -> None:
    """--ping <host>: test TCP di raggiungibilità."""
    app = _cli_app(config_path)
    host = _cli_resolve_host(app, host_ref)
    ok, msg = ssh_adapter.tcp_ping(host)
    print(msg)
    sys.exit(0 if ok else 1)


def _pane_info_cli(config_path, host_ref: str, session: str) -> None:
    """--pane-info <host> <session>: JSON con i dettagli della pane attiva."""
    app = _cli_app(config_path)
    host = _cli_resolve_host(app, host_ref)
    res = ssh_adapter.tmux_pane_info(host, session, app.ssh_cfg)
    print(
        json.dumps(
            {
                "ok": res.ok,
                "error": res.error,
                "command": res.command,
                "cwd": res.cwd,
                "pid": res.pid,
                "title": res.title,
                "width": res.width,
                "height": res.height,
                "is_shell": res.is_shell,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if not res.ok:
        print(res.error or "pane-info fallito", file=sys.stderr)
        sys.exit(1)
    return


def _copy_buffer_cli(config_path, host_ref: str, session: str) -> None:
    """--copy-buffer <host> <session>: copia il buffer tmux negli appunti (wl-copy)."""
    app = _cli_app(config_path)
    host = _cli_resolve_host(app, host_ref)
    res = ssh_adapter.run_tmux_action(host, "tmux show-buffer", app.ssh_cfg)
    if not res.ok or not res.stdout:
        print(res.stderr.strip() or "buffer vuoto o errore", file=sys.stderr)
        sys.exit(1)
    content = res.stdout
    try:
        subprocess.run(["wl-copy"], input=content, text=True, check=True)
    except FileNotFoundError:
        print(
            "wl-copy non installato (installa wl-clipboard): contenuto su stdout",
            file=sys.stderr,
        )
        print(content, end="")
    except Exception as exc:  # noqa: BLE001 - il contenuto resta comunque su stdout
        print(f"Errore copia locale: {exc}", file=sys.stderr)
        print(content, end="")
    return


def _send_text_cli(config_path, host_ref: str, session: str, text: str) -> None:
    """--send-text <host> <session> <testo>: incolla il testo nella sessione tmux."""
    app = _cli_app(config_path)
    host = _cli_resolve_host(app, host_ref)
    res = ssh_adapter.tmux_paste_buffer(host, session, text, app.ssh_cfg)
    if res.ok:
        print(f"Testo inviato a {host_ref}/{session}")
    else:
        print(res.stderr.strip() or "invio fallito", file=sys.stderr)
        sys.exit(1)
    return


def _send_file_cli(config_path, host_ref: str, session: str, path: str) -> None:
    """--send-file <host> <session> <percorso>: incolla il contenuto del file."""
    app = _cli_app(config_path)
    host = _cli_resolve_host(app, host_ref)
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError) as exc:
        print(f"Impossibile leggere '{path}': {exc}", file=sys.stderr)
        sys.exit(1)
    res = ssh_adapter.tmux_paste_buffer(host, session, content, app.ssh_cfg)
    if res.ok:
        print(f"File inviato a {host_ref}/{session}")
    else:
        print(res.stderr.strip() or "invio fallito", file=sys.stderr)
        sys.exit(1)
    return


def _broadcast_worker(app, command: str, snippet_name: str, hosts_opt, use_tmux: bool) -> None:
    """Esegue ``command`` sugli host indicati e stampa il JSON dei risultati."""
    from .ssh import broadcast

    cfg = app._config
    if hosts_opt:
        hosts = []
        for ref in hosts_opt.split(","):
            ref = ref.strip()
            if not ref:
                continue
            hosts.append(_cli_resolve_host(app, ref))
    else:
        hosts = list(cfg.hosts)
    if not hosts:
        print("Nessun host da contattare", file=sys.stderr)
        sys.exit(1)
    if use_tmux:
        results = broadcast.run_snippet_on_hosts_tmux(hosts, command, snippet_name, app.ssh_cfg)
    else:
        results = broadcast.run_snippet_on_hosts(hosts, command, app.ssh_cfg)
    payload = [
        {
            "host_alias": r.host_alias,
            "ok": r.ok,
            "exit_code": r.exit_code,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "error": r.error,
            "session_name": r.session_name,
        }
        for r in results
    ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if any(not r.ok for r in results):
        sys.exit(1)
    return


def _snippet_run_cli(config_path, name: str, hosts_opt, use_tmux: bool) -> None:
    """--snippet-run <nome> [--hosts ...] [--tmux]: esegue uno snippet salvato."""
    from .snippets import load_snippets

    app = _cli_app(config_path)
    snippet = next((s for s in load_snippets(app._config) if s.name == name), None)
    if not snippet:
        print(f"Snippet '{name}' non trovato", file=sys.stderr)
        sys.exit(1)
    _broadcast_worker(app, snippet.command, snippet.name, hosts_opt, use_tmux)
    return


def _broadcast_cli(config_path, command: str, hosts_opt, use_tmux: bool) -> None:
    """--broadcast <comando> --hosts <a,b> [--tmux]: comando libero su più host."""
    if not hosts_opt:
        print("--broadcast richiede --hosts <alias1,alias2,...>", file=sys.stderr)
        sys.exit(1)
    app = _cli_app(config_path)
    _broadcast_worker(app, command, "broadcast", hosts_opt, use_tmux)
    return


def _host_add_cli(
    config_path,
    alias: str,
    host_addr: str,
    opt_user,
    opt_port,
    opt_auth,
    opt_group,
    opt_jump_host,
    opt_cred_key,
) -> None:
    """--host-add <alias> <host> [opzioni]: aggiunge un host alla config."""
    app = _cli_app(config_path)
    cfg = app._config
    if cfg.host(alias):
        print(f"Host già esistente: '{alias}'", file=sys.stderr)
        sys.exit(1)
    if opt_auth is not None and opt_auth not in AUTH_METHODS:
        print(f"auth non valido: '{opt_auth}' (attesi {AUTH_METHODS})", file=sys.stderr)
        sys.exit(1)
    if opt_port:
        try:
            port = int(opt_port)
        except ValueError:
            print(f"Porta non valida: '{opt_port}'", file=sys.stderr)
            sys.exit(1)
    else:
        port = 22
    host = Host(
        alias=alias,
        host=host_addr,
        user=opt_user,
        port=port,
        auth=opt_auth or "",
        group=opt_group,
        jump_host=opt_jump_host,
        cred_key=opt_cred_key,
    )
    cfg.hosts.append(host)
    backup_config(cfg)
    save_config(cfg)
    print(f"Host aggiunto: {alias}")
    return


def _host_edit_cli(
    config_path,
    alias: str,
    opt_user,
    opt_port,
    opt_auth,
    opt_group,
    opt_jump_host,
    opt_cred_key,
) -> None:
    """--host-edit <alias> [opzioni]: modifica i campi passati di un host."""
    app = _cli_app(config_path)
    cfg = app._config
    host = _cli_resolve_host(app, alias)
    if opt_auth is not None:
        if opt_auth not in AUTH_METHODS:
            print(f"auth non valido: '{opt_auth}' (attesi {AUTH_METHODS})", file=sys.stderr)
            sys.exit(1)
        host.auth = opt_auth
    if opt_user is not None:
        host.user = opt_user
    if opt_port is not None:
        try:
            host.port = int(opt_port)
        except ValueError:
            print(f"Porta non valida: '{opt_port}'", file=sys.stderr)
            sys.exit(1)
    if opt_group is not None:
        host.group = opt_group
    if opt_jump_host is not None:
        host.jump_host = opt_jump_host
    if opt_cred_key is not None:
        host.cred_key = opt_cred_key
    backup_config(cfg)
    save_config(cfg)
    print(f"Host modificato: {alias}")
    return


def _host_delete_cli(config_path, alias: str) -> None:
    """--host-delete <alias>: rimuove un host dalla config."""
    app = _cli_app(config_path)
    cfg = app._config
    host = _cli_resolve_host(app, alias)
    cfg.hosts.remove(host)
    backup_config(cfg)
    save_config(cfg)
    print(f"Host eliminato: {alias}")
    return


def _tunnel_action_cli(config_path, host_ref: str, tunnel_ref: str, start: bool) -> None:
    """Avvia/ferma un tunnel dell'host (per nome o per local_port)."""
    app = _cli_app(config_path)
    host = _cli_resolve_host(app, host_ref)
    tunnel = None
    for cand in host.tunnels:
        if cand.name == tunnel_ref or str(cand.local_port) == tunnel_ref:
            tunnel = cand
            break
    if tunnel is None:
        print(f"Tunnel '{tunnel_ref}' non trovato su '{host_ref}'", file=sys.stderr)
        sys.exit(1)
    if start:
        jump, _ = app._jump_for(host)
        ok, msg = app.tunnels.start(
            host,
            tunnel,
            app._password_for(host),
            password_resolver=app._password_for,
            jump_host=jump,
        )
        print(msg)
        sys.exit(0 if ok else 1)
    ok = app.tunnels.stop(host.alias, tunnel.local_port)
    if ok:
        print(f"Tunnel {tunnel.local_port} fermato su {host.alias}")
    else:
        print(f"Tunnel {tunnel.local_port} non attivo su {host.alias}", file=sys.stderr)
    sys.exit(0 if ok else 1)


def _tunnel_start_cli(config_path, host_ref: str, tunnel_ref: str) -> None:
    """--tunnel-start <host> <tunnel>."""
    _tunnel_action_cli(config_path, host_ref, tunnel_ref, True)
    return


def _tunnel_stop_cli(config_path, host_ref: str, tunnel_ref: str) -> None:
    """--tunnel-stop <host> <tunnel>."""
    _tunnel_action_cli(config_path, host_ref, tunnel_ref, False)
    return


def _rotation_list_cli(config_path) -> None:
    """--rotation-list: stampa JSON dei profili di rotazione salvati."""
    from .rotation import load_rotations

    app = _cli_app(config_path)
    payload = [
        {
            "name": r.name,
            "entries": [{"host": h, "session": s} for h, s in r.unique_entries()],
        }
        for r in load_rotations(app._config)
    ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return


def _rotation_add_cli(config_path, name: str, raw_entries: list[str]) -> None:
    """--rotation-add <nome> <host:sessione> ...: salva un profilo di rotazione."""
    from .rotation import Rotation, add_rotation

    app = _cli_app(config_path)
    entries: list[tuple[str, str]] = []
    for entry in raw_entries:
        if ":" not in entry:
            print(f"Entry non valida (atteso host:sessione): '{entry}'", file=sys.stderr)
            sys.exit(1)
        host_ref, session = entry.split(":", 1)
        if not host_ref or not session:
            print(f"Entry malformata: '{entry}'", file=sys.stderr)
            sys.exit(1)
        entries.append((host_ref, session))
    if not entries:
        print("Nessuna entry valida per la rotazione", file=sys.stderr)
        sys.exit(1)
    add_rotation(app._config, Rotation(name=name, entries=entries))
    print(f"Rotazione salvata: {name}")
    return


def _rotation_remove_cli(config_path, name: str) -> None:
    """--rotation-remove <nome>: rimuove un profilo di rotazione."""
    from .rotation import remove_rotation

    app = _cli_app(config_path)
    remove_rotation(app._config, name)
    print(f"Rotazione rimossa: {name}")
    return


# ============================================================================
# CLI JSON per la GUI: output JSON nudo su stdout, errori su stderr + exit != 0.
# Ogni flag riusa le funzioni di ssh/adapter.py, ssh/tunnels.py, ssh/audit.py,
# snippets.py, config.py, file_ops.py, inspection.py. La TUI non viene toccata.
# ============================================================================

# flag -> numero di argomenti posizionali ("1+" = 1 + resto come lista)
_JSON_FLAGS: dict[str, object] = {
    "--list-hosts": 0,
    "--host-info": 1,
    "--tmux-present": 1,
    "--ping": 1,
    "--hosts-summary": 0,
    "--status": 0,
    "--host-health": 1,
    "--host-network-ports": 1,
    "--host-top-processes": 1,
    "--list-sessions": 1,
    "--session-details": 2,
    "--create-session": 1,
    "--rename-session": 3,
    "--kill-session": 2,
    "--detach-clients": 2,
    "--kill-server": 1,
    "--session-history": 0,
    "--pane-info": 2,
    "--pane-command": 2,
    "--pane-diff": 2,
    "--capture-pane": 2,
    "--copy-buffer": 2,
    "--list-windows": 2,
    "--new-window": 2,
    "--select-window": 3,
    "--rename-window": 4,
    "--kill-window": 3,
    "--send-text": 3,
    "--send-raw": 3,
    "--send-file": 3,
    "--snippet-list": 0,
    "--snippet-add": 2,
    "--snippet-remove": 1,
    "--snippet-run": "1+",
    "--broadcast": "1+",
    "--broadcast-wait": "1+",
    "--tunnel-list": 0,
    "--tunnel-start": 3,
    "--tunnel-stop": 2,
    "--stop-tunnels": 1,
    "--tunnel-health": 1,
    "--rotation-list": 0,
    "--rotation-add": "1+",
    "--rotation-remove": 1,
    "--read-file": 2,
    "--write-file": 3,
    "--edit-file": 2,
    "--replace-block": 2,
    "--project-tree": 1,
    "--search-files": 2,
    "--git-status": 1,
    "--sftp-list": 1,
    "--sftp-download": 3,
    "--sftp-upload": 3,
    "--sftp-get": 3,
    "--sftp-put": 3,
    "--sftp-mkdir": 2,
    "--sftp-rm": 2,
    "--sftp-rename": 3,
    "--sftp-batch": "2+",
    "--transfer-file": 4,
    "--transfer-file-direct": 4,
    "--run-command": 2,
    "--run-command-all": 1,
    "--run-command-many": 2,
    "--run-and-wait": 2,
    "--audit-list": 0,
    "--audit-list-remote": 1,
    "--read-audit-log": 1,
    "--read-remote-audit-log": 2,
    "--session-audit-log": 2,
    "--find-in-sessions": 1,
    "--packages": 3,
    "--list-services": 1,
    "--service": 2,
    "--service-logs": 2,
    "--sql": 2,
    "--launch-agent": 2,
}

_JSON_OPTS = {
    "--limit",
    "--lines",
    "--timeout",
    "--max-lines",
    "--max-depth",
    "--offset",
    "--pattern",
    "--replacement",
    "--old-text",
    "--new-text",
    "--content",
    "--mode",
    "--name",
    "--command",
    "--description",
    "--path",
    "--action",
    "--manager",
    "--level",
    "--grep",
    "--sort-by",
    "--remote-host",
    "--remote-port",
    "--bind",
    "--title",
    "--extra-args",
    "--wait-timeout",
    "--prompt",
    "--alias",
    "--host",
    "--user",
    "--password",
    "--host-addr",
}
_JSON_BOOL_OPTS = {"--recursive", "--enter", "--bracketed", "--force", "--tmux"}

# Tutti i flag noti (TUI + JSON): serve a intercettare i flag sconosciuti.
_ALL_KNOWN_FLAGS = (
    set(_JSON_FLAGS)
    | _JSON_OPTS
    | _JSON_BOOL_OPTS
    | {
        "-c",
        "--config",
        "--attach",
        "--attach-ro",
        "--shell",
        "--new",
        "--rotation",
        "--sftp",
        "--launch-agent",
        "--quick-launch",
        "--list-hosts",
        "--ping",
        "--hosts",
        "--host-add",
        "--host-edit",
        "--host-delete",
        "--auth",
        "--group",
        "--jump-host",
        "--cred-key",
        "--port",
    }
)


class _JsonCliError(Exception):
    """Errore CLI da riportare su stderr con exit code non-zero."""


def _jprint(obj: object) -> None:
    """Stampa un oggetto come JSON nudo su stdout."""
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _jfail(msg: str, code: int = 1) -> None:
    """Stampa l'errore su stderr ed esce con codice non-zero."""
    print(msg, file=sys.stderr)
    sys.exit(code)


def _unwrap(res: object) -> object:
    """Normalizza il risultato di un tool di inspection.

    ``inspection._run_py`` avvolge la risposta in ``{"ok": true, "data": ...}``:
    qui si estrae il payload per emettere JSON nudo. Su errore esce con messaggio
    su stderr + exit code != 0.
    """
    if isinstance(res, dict):
        if res.get("ok") is False or res.get("success") is False:
            _jfail(str(res.get("error") or res.get("message") or "operazione fallita"))
        if set(res) == {"ok", "data"}:
            return res["data"]
    return res


def _parse_json_flags(argv: list[str]):
    """Estrae (flag, args, opts, config_path) del primo comando JSON in argv.

    Ritorna ``None`` se in argv non compare nessun flag JSON noto.
    """
    config_path = None
    i = 0
    found = None
    while i < len(argv):
        tok = argv[i]
        if tok in ("-c", "--config") and i + 1 < len(argv):
            config_path = Path(argv[i + 1]).expanduser()
            i += 2
            continue
        if tok in _JSON_FLAGS:
            arity = _JSON_FLAGS[tok]
            if arity == "1+":
                args = [argv[i + 1]] if i + 1 < len(argv) else []
                j = i + 2
                while j < len(argv) and not argv[j].startswith("-"):
                    args.append(argv[j])
                    j += 1
                found = (tok, args)
                i = j
                break
            if arity == "2+":
                args = list(argv[i + 1 : i + 3])
                j = i + 3
                while j < len(argv) and not argv[j].startswith("-"):
                    args.append(argv[j])
                    j += 1
                found = (tok, args)
                i = j
                break
            if arity == 0:
                found = (tok, [])
                i += 1
                break
            n = int(arity)
            args = list(argv[i + 1 : i + 1 + n])
            found = (tok, args)
            i += 1 + n
            break
        i += 1
    if found is None:
        return None
    flag, args = found
    opts: dict[str, object] = {}
    while i < len(argv):
        tok = argv[i]
        if tok in _JSON_OPTS and i + 1 < len(argv):
            opts[tok] = argv[i + 1]
            i += 2
            continue
        if tok in _JSON_BOOL_OPTS:
            opts[tok] = True
            i += 1
            continue
        i += 1
    return flag, args, opts, config_path


def _opt_int(opts: dict, key: str, default: int) -> int:
    try:
        return int(opts.get(key, default))
    except (TypeError, ValueError):
        return default


def _opt_str(opts: dict, key: str, default: str = "") -> str:
    val = opts.get(key)
    return str(val) if val is not None else default


def _host_tunnels_payload(app, host: Host) -> list[dict]:
    active = {t.port for t in app.tunnels.active(host.alias)}
    return [
        {
            "name": t.name,
            "kind": t.kind,
            "local_port": t.local_port,
            "remote_host": t.remote_host,
            "remote_port": t.remote_port,
            "bind": t.bind,
            "active": t.local_port in active,
        }
        for t in host.tunnels
    ]


# ---------- Host / diagnostica ----------


def _jh_list_hosts(app, args, opts) -> None:
    cfg = app._config
    out = []
    for h in cfg.hosts:
        out.append(
            {
                "alias": h.alias,
                "host": h.host,
                "user": h.effective_user(),
                "port": h.port,
                "auth": h.auth or cfg.credential_provider,
                "group": h.group or "",
                "jump_host": h.jump_host or "",
                "local": h.is_local(),
            }
        )
    _jprint(out)


def _jh_host_info(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    _jprint(
        {
            "alias": h.alias,
            "host": h.host,
            "user": h.effective_user(),
            "port": h.port,
            "auth": h.auth,
            "group": h.group or "",
            "jump_host": h.jump_host or "",
            "local": h.is_local(),
            "tunnels": _host_tunnels_payload(app, h),
        }
    )


def _jh_tmux_present(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    present = ssh_adapter.tmux_present(h, app.ssh_cfg)
    _jprint({"alias": h.alias, "present": present})


def _jh_ping(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    ok, detail = ssh_adapter.tcp_ping(h, timeout=_opt_int(opts, "--timeout", 2))
    _jprint({"ok": ok, "detail": detail})
    if not ok:
        sys.exit(1)


def _jh_hosts_summary(app, args, opts) -> None:
    import concurrent.futures

    cfg = app._config
    timeout = float(opts.get("--timeout", 2.0) or 2.0)

    def probe(h: Host) -> dict:
        ok, detail = ssh_adapter.tcp_ping(h, timeout=timeout)
        if not ok:
            return {
                "alias": h.alias,
                "reachable": False,
                "detail": detail,
                "tmux": None,
                "sessions": None,
            }
        try:
            present = ssh_adapter.tmux_present(h, app.ssh_cfg)
            res = ssh_adapter.list_tmux_sessions(h, app.ssh_cfg)
            sessions = res.sessions if res.ok else None
        except Exception as exc:  # noqa: BLE001
            return {
                "alias": h.alias,
                "reachable": True,
                "detail": str(exc),
                "tmux": None,
                "sessions": None,
            }
        return {
            "alias": h.alias,
            "reachable": True,
            "detail": detail,
            "tmux": present,
            "sessions": sessions,
            "session_count": len(sessions) if sessions is not None else None,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, cfg.hosts))
    results.sort(key=lambda r: r["alias"])
    _jprint(results)


def _jh_status(app, args, opts) -> None:
    from . import __version__
    from .ssh.audit import cfg_path_logs_dir

    cfg = app._config
    provider = cfg.credential_provider or "keyring"
    keyring_ok = None
    if provider == "keyring":
        try:
            import keyring

            keyring_ok = keyring.get_keyring() is not None
        except Exception:  # noqa: BLE001
            keyring_ok = False
    try:
        logs_dir = str(cfg_path_logs_dir(cfg))
    except OSError:
        logs_dir = "n/d"
    try:
        local = next((h for h in cfg.hosts if h.is_local()), None)
        tmux_local = ssh_adapter.tmux_present(local, app.ssh_cfg) if local else None
    except Exception:  # noqa: BLE001
        tmux_local = None
    _jprint(
        {
            "version": __version__,
            "config_path": str(cfg.path) if cfg.path else None,
            "provider": provider,
            "keyring_available": keyring_ok,
            "plain_file": cfg.plain_file,
            "hosts": len(cfg.hosts),
            "tmux_local": tmux_local,
            "logs_dir": logs_dir,
            "audit_log": cfg.audit_log,
            "snippets_file": cfg.snippets_file,
            "tunnels_file": cfg.tunnels_file,
            "restart_after_ssh": cfg.restart_after_ssh,
        }
    )


def _jh_host_health(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_host_health(h, app.ssh_cfg, timeout=_opt_int(opts, "--timeout", 30))
        )
    )


def _jh_host_network_ports(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_host_network_ports(
                h, app.ssh_cfg, timeout=_opt_int(opts, "--timeout", 30)
            )
        )
    )


def _jh_host_top_processes(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_host_top_processes(
                h,
                app.ssh_cfg,
                limit=_opt_int(opts, "--limit", 10),
                sort_by=_opt_str(opts, "--sort-by", "cpu"),
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


# ---------- Sessioni tmux ----------


def _jh_list_sessions(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.list_tmux_sessions(h, app.ssh_cfg)
    if not res.ok:
        _jfail(res.error or "list-sessions fallito")
    _jprint(res.sessions)


def _jh_session_details(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_session_details(h, args[1], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "session-details fallito")
    _jprint({"session": args[1], "details": (res.stdout or "").strip()})


def _jh_create_session(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    name = _opt_str(opts, "--name") or h.alias
    command = _opt_str(opts, "--command")
    q = ssh_adapter._sh_quote
    tmux_cmd = f"tmux new -d -s {q(name)}"
    if command:
        tmux_cmd += f" {q(command)}"
    res = ssh_adapter.run_tmux_action(h, tmux_cmd, app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "creazione sessione fallita")
    _jprint({"alias": h.alias, "session": name, "created": True})


def _jh_rename_session(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_rename_session(h, args[1], args[2], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "rename-session fallito")
    _jprint({"alias": h.alias, "old": args[1], "new": args[2]})


def _jh_kill_session(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_kill_session(h, args[1], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "kill-session fallito")
    _jprint({"alias": h.alias, "session": args[1], "killed": True})


def _jh_detach_clients(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_detach_clients(h, args[1], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "detach-clients fallito")
    _jprint({"alias": h.alias, "session": args[1], "detached": True})


def _jh_kill_server(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_kill_server(h, app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "kill-server fallito")
    _jprint({"alias": h.alias, "killed": True})


def _jh_session_history(app, args, opts) -> None:
    from .history import load_history

    entries = load_history(app._config)
    limit = _opt_int(opts, "--limit", 0)
    if limit > 0:
        entries = entries[:limit]
    _jprint([{"host": e.host, "session": e.session} for e in entries])


# ---------- Pane / finestre ----------


def _jh_pane_info(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_pane_info(h, args[1], app.ssh_cfg)
    if not res.ok:
        _jfail(res.error or "pane-info fallito")
    _jprint(
        {
            "pane_id": res.pid,
            "command": res.command,
            "cwd": res.cwd,
            "pid": res.pid,
            "title": res.title,
            "width": res.width,
            "height": res.height,
            "is_shell": res.is_shell,
        }
    )


def _jh_pane_command(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_pane_command(h, args[1], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "pane-command fallito")
    _jprint({"alias": h.alias, "session": args[1], "command": (res.stdout or "").strip()})


_PANE_DIFF_CACHE: dict[tuple[str, str], list[str]] = {}


def _jh_pane_diff(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    session = args[1]
    max_lines = _opt_int(opts, "--max-lines", 200)
    cap = ssh_adapter.tmux_capture_pane(h, session, app.ssh_cfg, lines=max_lines)
    if not cap.ok:
        _jfail((cap.stderr or "").strip() or "pane-diff fallito")
    current = (cap.stdout or "").splitlines()
    key = (h.alias, session)
    old = _PANE_DIFF_CACHE.get(key)
    _PANE_DIFF_CACHE[key] = current
    if old is None:
        _jprint(
            {
                "alias": h.alias,
                "session": session,
                "is_first_sample": True,
                "total_lines": len(current),
                "diff_count": 0,
                "new_lines": current[-15:],
            }
        )
        return
    delta: list[str] = []
    max_overlap = min(len(old), len(current))
    for k in range(max_overlap, 0, -1):
        if old[-k:] == current[:k]:
            delta = current[k:]
            break
    else:
        import difflib

        sm = difflib.SequenceMatcher(a=old, b=current, autojunk=False)
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                delta.extend(current[j1:j2])
    _jprint(
        {
            "alias": h.alias,
            "session": session,
            "is_first_sample": False,
            "has_changes": len(delta) > 0,
            "diff_count": len(delta),
            "total_lines": len(current),
            "new_content": "\n".join(delta),
        }
    )


def _jh_capture_pane(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    lines = _opt_int(opts, "--lines", 200)
    res = ssh_adapter.tmux_capture_pane(h, args[1], app.ssh_cfg, lines=lines)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "capture-pane fallito")
    _jprint({"alias": h.alias, "session": args[1], "content": res.stdout or ""})


def _jh_copy_buffer(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.run_tmux_action(h, "tmux show-buffer", app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "copy-buffer fallito")
    _jprint({"alias": h.alias, "session": args[1], "text": res.stdout or ""})


def _jh_list_windows(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_list_windows_parsed(h, args[1], app.ssh_cfg)
    if not res.ok:
        _jfail(res.error or "list-windows fallito")
    _jprint(
        {
            "alias": h.alias,
            "session": args[1],
            "count": len(res.windows),
            "windows": [
                {
                    "index": w.index,
                    "name": w.name,
                    "active": w.active,
                    "pane_count": w.pane_count,
                    "layout": w.layout,
                }
                for w in res.windows
            ],
        }
    )


def _jh_new_window(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    name = _opt_str(opts, "--name") or None
    res = ssh_adapter.tmux_new_window(h, args[1], name, app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "new-window fallito")
    _jprint({"alias": h.alias, "session": args[1], "name": name or "", "created": True})


def _jh_select_window(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_select_window(h, args[1], args[2], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "select-window fallito")
    _jprint({"alias": h.alias, "session": args[1], "index": args[2]})


def _jh_rename_window(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_rename_window(h, args[1], args[2], args[3], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "rename-window fallito")
    _jprint({"alias": h.alias, "session": args[1], "window": args[2], "name": args[3]})


def _jh_kill_window(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_kill_window(h, args[1], args[2], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "kill-window fallito")
    _jprint({"alias": h.alias, "session": args[1], "window": args[2], "killed": True})


def _jh_send_text(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_send_input(
        h, args[1], args[2], app.ssh_cfg, enter=bool(opts.get("--enter", False))
    )
    if not res.ok:
        _jfail((res.stderr or "").strip() or "send-text fallito")
    _jprint({"alias": h.alias, "session": args[1], "sent": True})


def _jh_send_raw(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    res = ssh_adapter.tmux_send_raw(h, args[1], args[2], app.ssh_cfg)
    if not res.ok:
        _jfail((res.stderr or "").strip() or "send-raw fallito")
    _jprint({"alias": h.alias, "session": args[1], "sent": True})


def _jh_send_file(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    try:
        content = Path(args[2]).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _jfail(f"Impossibile leggere '{args[2]}': {exc}")
    res = ssh_adapter.tmux_paste_buffer(
        h, args[1], content, app.ssh_cfg, bracketed=bool(opts.get("--bracketed", True))
    )
    if not res.ok:
        _jfail((res.stderr or "").strip() or "send-file fallito")
    _jprint({"alias": h.alias, "session": args[1], "sent": True})


# ---------- Snippet / broadcast ----------


def _jh_snippet_list(app, args, opts) -> None:
    from .snippets import load_snippets

    _jprint(
        [
            {"name": s.name, "command": s.command, "description": s.description}
            for s in load_snippets(app._config)
        ]
    )


def _jh_snippet_add(app, args, opts) -> None:
    from .snippets import Snippet, add_snippet

    add_snippet(
        app._config,
        Snippet(name=args[0], command=args[1], description=_opt_str(opts, "--description")),
    )
    _jprint({"name": args[0], "added": True})


def _jh_snippet_remove(app, args, opts) -> None:
    from .snippets import remove_snippet

    remove_snippet(app._config, args[0])
    _jprint({"name": args[0], "removed": True})


def _jh_snippet_run(app, args, opts) -> None:
    from .snippets import load_snippets
    from .ssh import broadcast

    name = args[0]
    aliases = args[1:]
    snippet = next((s for s in load_snippets(app._config) if s.name == name), None)
    if not snippet:
        _jfail(f"Snippet '{name}' non trovato")
    hosts = [_cli_resolve_host(app, a) for a in aliases if app._config.host(a) is not None]
    results = broadcast.run_snippet_on_hosts(hosts, snippet.command, app.ssh_cfg)
    _jprint(
        [
            {
                "alias": r.host_alias,
                "ok": r.ok,
                "exit_code": r.exit_code,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "error": r.error,
            }
            for r in results
        ]
    )


def _jh_broadcast(app, args, opts) -> None:
    from .ssh import broadcast

    command = args[0]
    aliases = args[1:]
    mode = _opt_str(opts, "--mode", "direct")
    use_tmux = mode == "tmux" or bool(opts.get("--tmux"))
    hosts = [_cli_resolve_host(app, a) for a in aliases]
    if not hosts:
        _jfail("broadcast richiede almeno un alias")
    if use_tmux:
        results = broadcast.run_snippet_on_hosts_tmux(hosts, command, "broadcast", app.ssh_cfg)
    else:
        results = broadcast.run_snippet_on_hosts(hosts, command, app.ssh_cfg)
    _jprint(
        [
            {
                "alias": r.host_alias,
                "ok": r.ok,
                "exit_code": r.exit_code,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "error": r.error,
            }
            for r in results
        ]
    )


def _jh_broadcast_wait(app, args, opts) -> None:
    import concurrent.futures

    command = args[0]
    aliases = args[1:]
    timeout = _opt_int(opts, "--timeout", 300)
    hosts = [_cli_resolve_host(app, a) for a in aliases]
    if not hosts:
        _jfail("broadcast-wait richiede almeno un alias")

    def run(h: Host) -> dict:
        ok, output, error = _tmux_run_and_read(app, h, command, timeout, "bwait")
        return {"alias": h.alias, "ok": ok, "error": error, "output": output}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, hosts))
    results.sort(key=lambda r: r["alias"])
    _jprint(results)


# ---------- Tunnel ----------


def _jh_tunnel_list(app, args, opts) -> None:
    if args:
        hosts = [_cli_resolve_host(app, args[0])]
    else:
        hosts = list(app._config.hosts)
    payload = []
    for h in hosts:
        payload.append({"alias": h.alias, "tunnels": _host_tunnels_payload(app, h)})
    _jprint(payload if len(payload) != 1 else payload[0]["tunnels"])


def _jh_tunnel_start(app, args, opts) -> None:
    from .config import Tunnel

    h = _cli_resolve_host(app, args[0])
    kind = args[1].upper()
    local_port = int(args[2])
    spec = Tunnel(
        name=_opt_str(opts, "--name"),
        kind=kind,
        local_port=local_port,
        remote_host=_opt_str(opts, "--remote-host"),
        remote_port=_opt_int(opts, "--remote-port", 0),
        bind=_opt_str(opts, "--bind", "localhost") or "localhost",
    )
    jump, _jpw = app._jump_for(h)
    ok, msg = app.tunnels.start(
        h,
        spec,
        app._password_for(h),
        password_resolver=app._password_for,
        jump_host=jump,
    )
    if not ok:
        _jfail(msg)
    _jprint(
        {
            "alias": h.alias,
            "name": spec.name,
            "kind": spec.kind,
            "local_port": spec.local_port,
            "remote_host": spec.remote_host,
            "remote_port": spec.remote_port,
            "bind": spec.bind,
            "active": True,
            "message": msg,
        }
    )


def _jh_tunnel_stop(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    ok = app.tunnels.stop(h.alias, int(args[1]))
    if not ok:
        _jfail(f"Tunnel {args[1]} non attivo su {h.alias}")
    _jprint({"alias": h.alias, "local_port": int(args[1]), "stopped": True})


def _jh_stop_tunnels(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    n = app.tunnels.stop_all(h.alias)
    _jprint({"alias": h.alias, "stopped": n})


def _jh_tunnel_health(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    tunnels = _host_tunnels_payload(app, h)
    active = [t for t in tunnels if t["active"]]
    _jprint(
        {
            "alias": h.alias,
            "configured": len(tunnels),
            "active": len(active),
            "tunnels": tunnels,
        }
    )


# ---------- Rotazioni ----------


def _jh_rotation_list(app, args, opts) -> None:
    from .rotation import load_rotations

    _jprint(
        [
            {
                "name": r.name,
                "entries": [{"host": h, "session": s} for h, s in r.unique_entries()],
            }
            for r in load_rotations(app._config)
        ]
    )


def _jh_rotation_add(app, args, opts) -> None:
    from .rotation import Rotation, add_rotation

    name = args[0]
    entries = []
    for entry in args[1:]:
        if ":" not in entry:
            _jfail(f"Entry non valida (atteso host:sessione): '{entry}'")
        host_ref, session = entry.split(":", 1)
        entries.append((host_ref, session))
    if not entries:
        _jfail("Nessuna entry valida per la rotazione")
    add_rotation(app._config, Rotation(name=name, entries=entries))
    _jprint({"name": name, "entries": len(entries), "saved": True})


def _jh_rotation_remove(app, args, opts) -> None:
    from .rotation import remove_rotation

    remove_rotation(app._config, args[0])
    _jprint({"name": args[0], "removed": True})


# ---------- File ----------


def _jh_read_file(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_read_file(
                h,
                app.ssh_cfg,
                args[1],
                offset=_opt_int(opts, "--offset", 1),
                limit=_opt_int(opts, "--limit", 100),
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_write_file(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_write_file(
                h,
                app.ssh_cfg,
                args[1],
                args[2],
                mode=_opt_str(opts, "--mode", "overwrite"),
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_edit_file(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_edit_file(
                h,
                app.ssh_cfg,
                args[1],
                pattern=_opt_str(opts, "--pattern"),
                replacement=_opt_str(opts, "--replacement"),
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_replace_block(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.replace_block(
                h,
                app.ssh_cfg,
                args[1],
                _opt_str(opts, "--old-text"),
                _opt_str(opts, "--new-text"),
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_project_tree(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.project_tree(
                h,
                app.ssh_cfg,
                _opt_str(opts, "--path", ".") or ".",
                max_depth=_opt_int(opts, "--max-depth", 3),
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_search_files(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_search_files(
                h,
                app.ssh_cfg,
                path=_opt_str(opts, "--path", ".") or ".",
                pattern=args[1],
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_git_status(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_git_status(
                h,
                app.ssh_cfg,
                path=_opt_str(opts, "--path", ".") or ".",
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


# ---------- SFTP / trasferimento ----------


def _jh_sftp_list(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    path = args[1] if len(args) > 1 else _opt_str(opts, "--path", ".")
    res = file_ops.sftp_list(h, path or ".", app._password_for(h))
    _jprint(
        {"alias": h.alias, "path": path, "ok": res.ok, "stdout": res.stdout, "stderr": res.stderr}
    )


def _jh_sftp_download(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.download(h, args[1], args[2], app._password_for(h))
    _jprint(
        {"alias": h.alias, "remote": args[1], "local": args[2], "ok": res.ok, "stderr": res.stderr}
    )


def _jh_sftp_upload(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.upload(h, args[1], args[2], app._password_for(h))
    _jprint(
        {"alias": h.alias, "local": args[1], "remote": args[2], "ok": res.ok, "stderr": res.stderr}
    )


def _jh_sftp_get(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.sftp_get(
        h, args[1], args[2], app._password_for(h), recursive=bool(opts.get("--recursive"))
    )
    _jprint(
        {"alias": h.alias, "remote": args[1], "local": args[2], "ok": res.ok, "stderr": res.stderr}
    )


def _jh_sftp_put(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.sftp_put(
        h, args[1], args[2], app._password_for(h), recursive=bool(opts.get("--recursive"))
    )
    _jprint(
        {"alias": h.alias, "local": args[1], "remote": args[2], "ok": res.ok, "stderr": res.stderr}
    )


def _jh_sftp_mkdir(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.sftp_mkdir(h, args[1], app._password_for(h))
    _jprint({"alias": h.alias, "path": args[1], "ok": res.ok, "stderr": res.stderr})


def _jh_sftp_rm(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.sftp_rm(h, args[1], app._password_for(h))
    _jprint({"alias": h.alias, "path": args[1], "ok": res.ok, "stderr": res.stderr})


def _jh_sftp_rename(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.sftp_rename(h, args[1], args[2], app._password_for(h))
    _jprint({"alias": h.alias, "old": args[1], "new": args[2], "ok": res.ok, "stderr": res.stderr})


def _jh_sftp_batch(app, args, opts) -> None:
    from .ssh import file_ops

    h = _cli_resolve_host(app, args[0])
    res = file_ops.sftp_batch(h, args[1:], app._password_for(h))
    _jprint(
        {
            "alias": h.alias,
            "commands": args[1:],
            "ok": res.ok,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }
    )


def _jh_transfer_file(app, args, opts) -> None:
    import hashlib
    import tempfile

    from .ssh import file_ops

    src = _cli_resolve_host(app, args[0])
    dst = _cli_resolve_host(app, args[2])
    with tempfile.TemporaryDirectory(prefix="bravoric-xfer-") as tmpdir:
        tmp = Path(tmpdir) / Path(args[1]).name
        down = file_ops.download(src, args[1], str(tmp), app._password_for(src))
        if not down.ok:
            _jfail(f"download fallito: {down.stderr.strip()}")
        size = tmp.stat().st_size
        md5 = hashlib.md5(tmp.read_bytes()).hexdigest()
        up = file_ops.upload(dst, str(tmp), args[3], app._password_for(dst))
        if not up.ok:
            _jfail(f"upload fallito: {up.stderr.strip()}")
    _jprint(
        {
            "src": f"{args[0]}:{args[1]}",
            "dst": f"{args[2]}:{args[3]}",
            "bytes": size,
            "md5": md5,
            "ok": True,
        }
    )


def _jh_transfer_file_direct(app, args, opts) -> None:
    from .ssh import file_ops

    src = _cli_resolve_host(app, args[0])
    dst = _cli_resolve_host(app, args[2])
    res = file_ops.transfer_file_direct(
        src, args[1], dst, args[3], app._password_for(src), app._password_for(dst)
    )
    _jprint(
        {
            "src": f"{args[0]}:{args[1]}",
            "dst": f"{args[2]}:{args[3]}",
            "ok": res.ok,
            "stderr": res.stderr,
        }
    )


# ---------- Comandi ----------


def _jh_run_command(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    timeout = _opt_int(opts, "--timeout", 60)
    res = ssh_adapter.run_tmux_action(h, args[1], app.ssh_cfg, timeout=timeout)
    _jprint(
        {
            "alias": h.alias,
            "ok": res.ok,
            "exit_code": 0 if res.ok else 1,
            "stdout": res.stdout or "",
            "stderr": res.stderr or "",
        }
    )


def _jh_run_command_all(app, args, opts) -> None:
    from .ssh import broadcast

    timeout = _opt_int(opts, "--timeout", 60)
    results = broadcast.run_snippet_on_hosts(
        app._config.hosts, args[0], app.ssh_cfg, timeout=timeout
    )
    _jprint(
        [
            {
                "alias": r.host_alias,
                "ok": r.ok,
                "exit_code": r.exit_code,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "error": r.error,
            }
            for r in results
        ]
    )


def _jh_run_command_many(app, args, opts) -> None:
    from .ssh import broadcast

    timeout = _opt_int(opts, "--timeout", 60)
    aliases = [a.strip() for a in args[0].split(",") if a.strip()]
    hosts = [_cli_resolve_host(app, a) for a in aliases]
    results = broadcast.run_snippet_on_hosts(hosts, args[1], app.ssh_cfg, timeout=timeout)
    _jprint(
        [
            {
                "alias": r.host_alias,
                "ok": r.ok,
                "exit_code": r.exit_code,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "error": r.error,
            }
            for r in results
        ]
    )


def _tmux_run_and_read(
    app, host: Host, command: str, timeout: int, prefix: str
) -> tuple[bool, str, str]:
    """Esegue un comando in tmux detached e ne legge l'output pulito dal file."""
    import time as _time

    from .ssh.broadcast import run_snippet_on_host, session_slug

    marker = f"__BRAVORIC_DONE_{int(_time.time() * 1000)}__"
    name = session_slug(prefix, host.alias)
    logfile = f"/tmp/{name}.log"
    q = ssh_adapter._sh_quote
    res = ssh_adapter.run_tmux_action(host, f"tmux new -d -s {q(name)}", app.ssh_cfg)
    if not res.ok:
        return False, "", (res.stderr or "").strip() or "creazione sessione fallita"
    wrapped = f"{{ {command} ; }} > {q(logfile)} 2>&1 ; echo {q(marker)}"
    ssh_adapter.run_tmux_action(host, f"tmux send-keys -t {q(name)} -l {q(wrapped)}", app.ssh_cfg)
    ssh_adapter.run_tmux_action(host, f"tmux send-keys -t {q(name)} Enter", app.ssh_cfg)
    done_line = marker.strip()
    deadline = _time.time() + int(timeout)
    while _time.time() < deadline:
        cap = ssh_adapter.tmux_capture_pane(host, name, app.ssh_cfg, lines=200)
        if any(ln.strip() == done_line for ln in (cap.stdout or "").splitlines()):
            ssh_adapter.run_tmux_action(
                host, f"tmux kill-session -t {q(name)} 2>/dev/null", app.ssh_cfg
            )
            read = run_snippet_on_host(
                host, f"cat {q(logfile)} 2>/dev/null ; rm -f {q(logfile)}", app.ssh_cfg, timeout=30
            )
            return True, (read.stdout or "").strip(), ""
        _time.sleep(1)
    ssh_adapter.run_tmux_action(host, f"tmux kill-session -t {q(name)} 2>/dev/null", app.ssh_cfg)
    run_snippet_on_host(host, f"rm -f {q(logfile)} 2>/dev/null", app.ssh_cfg, timeout=10)
    return False, "", f"timeout dopo {timeout}s"


def _jh_run_and_wait(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    timeout = _opt_int(opts, "--timeout", 300)
    ok, output, error = _tmux_run_and_read(app, h, args[1], timeout, "wait")
    _jprint({"alias": h.alias, "ok": ok, "error": error, "output": output})


# ---------- Audit / log ----------


def _jh_audit_list(app, args, opts) -> None:
    from .ssh.audit import cfg_path_logs_dir

    logs_dir = cfg_path_logs_dir(app._config)
    files = []
    try:
        for p in sorted(logs_dir.glob("*.log.gz")):
            st = p.stat()
            files.append(
                {"filename": p.name, "path": str(p), "size": st.st_size, "mtime": int(st.st_mtime)}
            )
    except OSError:
        pass
    _jprint(files)


def _jh_audit_list_remote(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    cmd = "ls -la ~/.bravoric-ssh-client/logs/*.log.gz 2>/dev/null || true"
    res = ssh_adapter.run_tmux_action(h, cmd, app.ssh_cfg, timeout=_opt_int(opts, "--timeout", 30))
    files = [
        ln.split()[-1] for ln in (res.stdout or "").splitlines() if ln.strip().endswith(".log.gz")
    ]
    _jprint({"alias": h.alias, "files": files})


def _jh_read_audit_log(app, args, opts) -> None:
    from .ssh.audit import cfg_path_logs_dir, read_log_gz

    logs_dir = cfg_path_logs_dir(app._config)
    path = Path(args[0])
    if not path.is_absolute():
        path = logs_dir / args[0]
    if not path.exists():
        _jfail(f"Log '{args[0]}' non trovato")
    content = read_log_gz(path, max_lines=_opt_int(opts, "--max-lines", 0))
    _jprint({"filename": path.name, "content": content})


def _jh_read_remote_audit_log(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    q = ssh_adapter._sh_quote
    max_lines = _opt_int(opts, "--max-lines", 0)
    fname = args[1]
    tail = f" | tail -n {max_lines}" if max_lines > 0 else ""
    cmd = (
        f"f=$(ls ~/.bravoric-ssh-client/logs/*{q(fname)}* 2>/dev/null | head -n1); "
        f'if [ -n "$f" ]; then zcat "$f"{tail}; fi'
    )
    proc = ssh_adapter.run_tmux_action(h, cmd, app.ssh_cfg, timeout=_opt_int(opts, "--timeout", 60))
    _jprint({"alias": h.alias, "filename": fname, "content": proc.stdout or ""})


def _jh_session_audit_log(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_session_audit_log(
                h,
                app.ssh_cfg,
                args[1],
                max_lines=_opt_int(opts, "--max-lines", 0),
                timeout=_opt_int(opts, "--timeout", 60),
            )
        )
    )


def _jh_find_in_sessions(app, args, opts) -> None:
    import re

    h = _cli_resolve_host(app, args[0])
    pattern = _opt_str(opts, "--pattern")
    if not pattern:
        _jfail("find-in-sessions richiede --pattern")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        _jfail(f"pattern non valido: {exc}")
    res = ssh_adapter.list_tmux_sessions(h, app.ssh_cfg)
    if not res.ok:
        _jfail(res.error or "list-sessions fallito")
    out = []
    for session in res.sessions:
        cap = ssh_adapter.tmux_capture_pane(h, session, app.ssh_cfg, lines=2000)
        if not cap.ok:
            continue
        hits = [
            {"line": i, "text": t[:500]}
            for i, t in enumerate((cap.stdout or "").splitlines(), start=1)
            if rx.search(t)
        ]
        if hits:
            out.append({"session": session, "matches": hits[:50], "total": len(hits)})
    _jprint(out)


# ---------- Sistema ----------


def _jh_packages(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    pkgs = [p.strip() for p in args[2].split(",") if p.strip()]
    _jprint(
        _unwrap(
            inspection.remote_manage_packages(
                h, app.ssh_cfg, args[1], pkgs, timeout=_opt_int(opts, "--timeout", 300)
            )
        )
    )


def _jh_list_services(app, args, opts) -> None:
    h = _cli_resolve_host(app, args[0])
    cmd = (
        "systemctl list-units --type=service --state=running --no-pager --no-legend "
        "2>/dev/null | awk '{print $1}' | head -n 200 || true"
    )
    res = ssh_adapter.run_tmux_action(h, cmd, app.ssh_cfg, timeout=_opt_int(opts, "--timeout", 30))
    services = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    _jprint({"alias": h.alias, "services": services})


def _jh_service(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_manage_service(
                h,
                app.ssh_cfg,
                args[1],
                action=_opt_str(opts, "--action", "status") or "status",
                manager=_opt_str(opts, "--manager", "systemd") or "systemd",
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_service_logs(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    _jprint(
        _unwrap(
            inspection.remote_read_service_logs(
                h,
                app.ssh_cfg,
                args[1],
                lines=_opt_int(opts, "--lines", 100),
                level=_opt_str(opts, "--level"),
                grep=_opt_str(opts, "--grep"),
                timeout=_opt_int(opts, "--timeout", 30),
            )
        )
    )


def _jh_sql(app, args, opts) -> None:
    from .ssh import inspection

    h = _cli_resolve_host(app, args[0])
    query = args[1]
    engine = _opt_str(opts, "--engine", "sqlite") or "sqlite"
    db = _opt_str(opts, "--db", "") or ""
    _jprint(
        _unwrap(
            inspection.remote_run_sql_query(
                h,
                app.ssh_cfg,
                engine,
                db,
                query,
                user=_opt_str(opts, "--user"),
                password=_opt_str(opts, "--password"),
                host_addr=_opt_str(opts, "--host-addr"),
                timeout=_opt_int(opts, "--timeout", 60),
            )
        )
    )


# ---------- Agenti ----------


def _jh_launch_agent(app, args, opts) -> None:
    from .mcp_server import BravoricMcp

    mcp = BravoricMcp(config=app._config)
    raw = mcp.launch_agent(
        agent=args[0],
        path=args[1],
        extra_args=_opt_str(opts, "--extra-args"),
        title=_opt_str(opts, "--title"),
        alias=_opt_str(opts, "--alias"),
        wait_timeout=_opt_int(opts, "--wait-timeout", 25),
        force=bool(opts.get("--force")),
        prompt=_opt_str(opts, "--prompt"),
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"ok": True, "raw": raw}
    _jprint(payload)
    if isinstance(payload, dict) and payload.get("ok") is False:
        sys.exit(1)


_JSON_HANDLERS = {
    "--list-hosts": _jh_list_hosts,
    "--host-info": _jh_host_info,
    "--tmux-present": _jh_tmux_present,
    "--ping": _jh_ping,
    "--hosts-summary": _jh_hosts_summary,
    "--status": _jh_status,
    "--host-health": _jh_host_health,
    "--host-network-ports": _jh_host_network_ports,
    "--host-top-processes": _jh_host_top_processes,
    "--list-sessions": _jh_list_sessions,
    "--session-details": _jh_session_details,
    "--create-session": _jh_create_session,
    "--rename-session": _jh_rename_session,
    "--kill-session": _jh_kill_session,
    "--detach-clients": _jh_detach_clients,
    "--kill-server": _jh_kill_server,
    "--session-history": _jh_session_history,
    "--pane-info": _jh_pane_info,
    "--pane-command": _jh_pane_command,
    "--pane-diff": _jh_pane_diff,
    "--capture-pane": _jh_capture_pane,
    "--copy-buffer": _jh_copy_buffer,
    "--list-windows": _jh_list_windows,
    "--new-window": _jh_new_window,
    "--select-window": _jh_select_window,
    "--rename-window": _jh_rename_window,
    "--kill-window": _jh_kill_window,
    "--send-text": _jh_send_text,
    "--send-raw": _jh_send_raw,
    "--send-file": _jh_send_file,
    "--snippet-list": _jh_snippet_list,
    "--snippet-add": _jh_snippet_add,
    "--snippet-remove": _jh_snippet_remove,
    "--snippet-run": _jh_snippet_run,
    "--broadcast": _jh_broadcast,
    "--broadcast-wait": _jh_broadcast_wait,
    "--tunnel-list": _jh_tunnel_list,
    "--tunnel-start": _jh_tunnel_start,
    "--tunnel-stop": _jh_tunnel_stop,
    "--stop-tunnels": _jh_stop_tunnels,
    "--tunnel-health": _jh_tunnel_health,
    "--rotation-list": _jh_rotation_list,
    "--rotation-add": _jh_rotation_add,
    "--rotation-remove": _jh_rotation_remove,
    "--read-file": _jh_read_file,
    "--write-file": _jh_write_file,
    "--edit-file": _jh_edit_file,
    "--replace-block": _jh_replace_block,
    "--project-tree": _jh_project_tree,
    "--search-files": _jh_search_files,
    "--git-status": _jh_git_status,
    "--sftp-list": _jh_sftp_list,
    "--sftp-download": _jh_sftp_download,
    "--sftp-upload": _jh_sftp_upload,
    "--sftp-get": _jh_sftp_get,
    "--sftp-put": _jh_sftp_put,
    "--sftp-mkdir": _jh_sftp_mkdir,
    "--sftp-rm": _jh_sftp_rm,
    "--sftp-rename": _jh_sftp_rename,
    "--sftp-batch": _jh_sftp_batch,
    "--transfer-file": _jh_transfer_file,
    "--transfer-file-direct": _jh_transfer_file_direct,
    "--run-command": _jh_run_command,
    "--run-command-all": _jh_run_command_all,
    "--run-command-many": _jh_run_command_many,
    "--run-and-wait": _jh_run_and_wait,
    "--audit-list": _jh_audit_list,
    "--audit-list-remote": _jh_audit_list_remote,
    "--read-audit-log": _jh_read_audit_log,
    "--read-remote-audit-log": _jh_read_remote_audit_log,
    "--session-audit-log": _jh_session_audit_log,
    "--find-in-sessions": _jh_find_in_sessions,
    "--packages": _jh_packages,
    "--list-services": _jh_list_services,
    "--service": _jh_service,
    "--service-logs": _jh_service_logs,
    "--sql": _jh_sql,
    "--launch-agent": _jh_launch_agent,
}


def _maybe_run_json_cli(argv: list[str]) -> bool:
    """Esegue il comando JSON se presente. True se gestito."""
    parsed = _parse_json_flags(argv)
    if parsed is None:
        return False
    flag, args, opts, config_path = parsed
    handler = _JSON_HANDLERS.get(flag)
    if handler is None:
        _jfail(f"Comando non implementato: {flag}")
    app = _cli_app(config_path)
    try:
        handler(app, args, opts)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - qualunque errore -> stderr + exit != 0
        _jfail(str(exc))
    return True


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if _maybe_run_json_cli(argv):
        return
    config_path = None
    attach_host = None
    attach_session = None
    attach_ro = False
    shell_host = None
    new_host = None
    new_name = None
    rotation_name = None
    sftp_host_a = None
    sftp_host_b = None
    launch_agent = False
    quick_launch = False
    list_hosts = False
    list_sessions_host = None
    ping_host = None
    pane_info_host = None
    pane_info_session = None
    copy_buffer_host = None
    copy_buffer_session = None
    send_text_host = None
    send_text_session = None
    send_text_body = None
    send_file_host = None
    send_file_session = None
    send_file_path = None
    snippet_run_name = None
    broadcast_cmd = None
    opt_hosts = None
    opt_tmux = False
    host_add_alias = None
    host_add_host = None
    host_edit_alias = None
    host_delete_alias = None
    opt_user = None
    opt_port = None
    opt_auth = None
    opt_group = None
    opt_jump_host = None
    opt_cred_key = None
    tunnel_start_host = None
    tunnel_start_tunnel = None
    tunnel_stop_host = None
    tunnel_stop_tunnel = None
    rotation_list = False
    rotation_add_name = None
    rotation_add_entries = []
    rotation_remove_name = None
    i = 0
    while i < len(argv):
        if argv[i] in ("-c", "--config") and i + 1 < len(argv):
            config_path = Path(argv[i + 1]).expanduser()
            i += 2
            continue
        if argv[i] == "--attach" and i + 2 < len(argv):
            attach_host = argv[i + 1]
            attach_session = argv[i + 2]
            i += 3
            continue
        if argv[i] == "--attach-ro" and i + 2 < len(argv):
            attach_host = argv[i + 1]
            attach_session = argv[i + 2]
            attach_ro = True
            i += 3
            continue
        if argv[i] == "--shell" and i + 1 < len(argv):
            shell_host = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--new" and i + 1 < len(argv):
            new_host = argv[i + 1]
            if i + 2 < len(argv) and not argv[i + 2].startswith("-"):
                new_name = argv[i + 2]
                i += 3
            else:
                i += 2
            continue
        if argv[i] == "--rotation" and i + 1 < len(argv):
            rotation_name = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--sftp" and i + 2 < len(argv):
            sftp_host_a = argv[i + 1]
            sftp_host_b = argv[i + 2]
            i += 3
            continue
        if argv[i] == "--launch-agent":
            launch_agent = True
            i += 1
            continue
        if argv[i] == "--quick-launch":
            quick_launch = True
            i += 1
            continue
        if argv[i] == "--list-hosts":
            list_hosts = True
            i += 1
            continue
        if argv[i] == "--list-sessions" and i + 1 < len(argv):
            list_sessions_host = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--ping" and i + 1 < len(argv):
            ping_host = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--pane-info" and i + 2 < len(argv):
            pane_info_host = argv[i + 1]
            pane_info_session = argv[i + 2]
            i += 3
            continue
        if argv[i] == "--copy-buffer" and i + 2 < len(argv):
            copy_buffer_host = argv[i + 1]
            copy_buffer_session = argv[i + 2]
            i += 3
            continue
        if argv[i] == "--send-text" and i + 3 < len(argv):
            send_text_host = argv[i + 1]
            send_text_session = argv[i + 2]
            send_text_body = argv[i + 3]
            i += 4
            continue
        if argv[i] == "--send-file" and i + 3 < len(argv):
            send_file_host = argv[i + 1]
            send_file_session = argv[i + 2]
            send_file_path = argv[i + 3]
            i += 4
            continue
        if argv[i] == "--snippet-run" and i + 1 < len(argv):
            snippet_run_name = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--broadcast" and i + 1 < len(argv):
            broadcast_cmd = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--hosts" and i + 1 < len(argv):
            opt_hosts = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--tmux":
            opt_tmux = True
            i += 1
            continue
        if argv[i] == "--host-add" and i + 2 < len(argv):
            host_add_alias = argv[i + 1]
            host_add_host = argv[i + 2]
            i += 3
            continue
        if argv[i] == "--host-edit" and i + 1 < len(argv):
            host_edit_alias = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--host-delete" and i + 1 < len(argv):
            host_delete_alias = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--user" and i + 1 < len(argv):
            opt_user = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--port" and i + 1 < len(argv):
            opt_port = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--auth" and i + 1 < len(argv):
            opt_auth = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--group" and i + 1 < len(argv):
            opt_group = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--jump-host" and i + 1 < len(argv):
            opt_jump_host = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--cred-key" and i + 1 < len(argv):
            opt_cred_key = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--tunnel-start" and i + 2 < len(argv):
            tunnel_start_host = argv[i + 1]
            tunnel_start_tunnel = argv[i + 2]
            i += 3
            continue
        if argv[i] == "--tunnel-stop" and i + 2 < len(argv):
            tunnel_stop_host = argv[i + 1]
            tunnel_stop_tunnel = argv[i + 2]
            i += 3
            continue
        if argv[i] == "--rotation-list":
            rotation_list = True
            i += 1
            continue
        if argv[i] == "--rotation-add" and i + 1 < len(argv):
            rotation_add_name = argv[i + 1]
            j = i + 2
            while j < len(argv) and not argv[j].startswith("-") and ":" in argv[j]:
                rotation_add_entries.append(argv[j])
                j += 1
            i = j
            continue
        if argv[i] == "--rotation-remove" and i + 1 < len(argv):
            rotation_remove_name = argv[i + 1]
            i += 2
            continue
        i += 1
    if list_hosts:
        _list_hosts_cli(config_path)
        return
    if list_sessions_host:
        _list_sessions_cli(config_path, list_sessions_host)
        return
    if ping_host:
        _ping_cli(config_path, ping_host)
        return
    if pane_info_host and pane_info_session:
        _pane_info_cli(config_path, pane_info_host, pane_info_session)
        return
    if copy_buffer_host and copy_buffer_session:
        _copy_buffer_cli(config_path, copy_buffer_host, copy_buffer_session)
        return
    if send_text_host and send_text_session and send_text_body is not None:
        _send_text_cli(config_path, send_text_host, send_text_session, send_text_body)
        return
    if send_file_host and send_file_session and send_file_path:
        _send_file_cli(config_path, send_file_host, send_file_session, send_file_path)
        return
    if snippet_run_name:
        _snippet_run_cli(config_path, snippet_run_name, opt_hosts, opt_tmux)
        return
    if broadcast_cmd:
        _broadcast_cli(config_path, broadcast_cmd, opt_hosts, opt_tmux)
        return
    if host_add_alias and host_add_host:
        _host_add_cli(
            config_path,
            host_add_alias,
            host_add_host,
            opt_user,
            opt_port,
            opt_auth,
            opt_group,
            opt_jump_host,
            opt_cred_key,
        )
        return
    if host_edit_alias:
        _host_edit_cli(
            config_path,
            host_edit_alias,
            opt_user,
            opt_port,
            opt_auth,
            opt_group,
            opt_jump_host,
            opt_cred_key,
        )
        return
    if host_delete_alias:
        _host_delete_cli(config_path, host_delete_alias)
        return
    if tunnel_start_host and tunnel_start_tunnel:
        _tunnel_start_cli(config_path, tunnel_start_host, tunnel_start_tunnel)
        return
    if tunnel_stop_host and tunnel_stop_tunnel:
        _tunnel_stop_cli(config_path, tunnel_stop_host, tunnel_stop_tunnel)
        return
    if rotation_list:
        _rotation_list_cli(config_path)
        return
    if rotation_add_name:
        _rotation_add_cli(config_path, rotation_add_name, rotation_add_entries)
        return
    if rotation_remove_name:
        _rotation_remove_cli(config_path, rotation_remove_name)
        return
    # Flag sconosciuto: errore su stderr + exit != 0 (NON avviare la TUI).
    unknown = [a for a in argv if a.startswith("-") and a not in _ALL_KNOWN_FLAGS]
    if unknown:
        print(f"Opzione sconosciuta: {unknown[0]}", file=sys.stderr)
        sys.exit(2)
    app = BravoricApp(
        config_path=config_path,
        start_rotation=rotation_name,
        launch_agent=launch_agent,
        quick_launch=quick_launch,
    )
    if sftp_host_a and sftp_host_b:
        # modalità CLI: apre Midnight Commander sui due host (o locale+remoto)
        if app._config is None:
            app._load_config()
        _sftp_cli(app._config, sftp_host_a, sftp_host_b)
        return
    if attach_host and attach_session:
        # modalità CLI: attach diretto (usata per "riapri tutte le recenti")
        if app._config is None:
            app._load_config()
        host = app._config.host(attach_host) if app._config else None
        if not host:
            print(f"Host '{attach_host}' non trovato", file=sys.stderr)
            return
        from .history import record_history

        record_history(app._config, host.alias, attach_session)
        password = app._password_for(host)
        jump, jump_password = app._jump_for(host)
        audit = app._audit_for(host, attach_session)
        if attach_ro:
            tmux_runner.tmux_attach_ro(
                host,
                attach_session,
                password,
                jump_host=jump,
                jump_password=jump_password,
                audit=audit,
            )
        else:
            tmux_runner.tmux_attach(
                host,
                attach_session,
                password,
                jump_host=jump,
                jump_password=jump_password,
                audit=audit,
            )
        return
    if shell_host:
        # modalità CLI: shell interattiva su un host
        if app._config is None:
            app._load_config()
        host = app._config.host(shell_host) if app._config else None
        if not host:
            print(f"Host '{shell_host}' non trovato", file=sys.stderr)
            return
        password = app._password_for(host)
        jump, jump_password = app._jump_for(host)
        tmux_runner.ssh_shell(
            host,
            password,
            jump_host=jump,
            jump_password=jump_password,
            audit=app._audit_for(host),
        )
        return
    if new_host:
        # modalità CLI: crea (o rientra in) una sessione tmux
        if app._config is None:
            app._load_config()
        host = app._config.host(new_host) if app._config else None
        if not host:
            print(f"Host '{new_host}' non trovato", file=sys.stderr)
            return
        from .history import record_history

        session_name = new_name or host.alias
        record_history(app._config, host.alias, session_name)
        password = app._password_for(host)
        jump, jump_password = app._jump_for(host)
        tmux_runner.tmux_new(
            host,
            new_name,
            password,
            jump_host=jump,
            jump_password=jump_password,
            audit=app._audit_for(host, session_name),
        )
        return
    result = app.run()
    if isinstance(result, LaunchAction):
        from .history import record_history

        if app._config is None:
            app._load_config()
        password = app._password_for(result.host)
        jump, jump_password = app._jump_for(result.host)
        if result.kind in ("attach", "attach_ro"):
            record_history(app._config, result.host.alias, result.session or "")
        # restart_after_ssh: la TUI non può riavviarsi da sola dopo l'exec (che sostituisce
        # il processo). Marchiamo il comando con cui riaprirla: _run_or_exec lo eseguirà in
        # una shell che sopravvive alla sessione ssh.
        if app._config and app._config.restart_after_ssh:
            tui = [sys.executable, "-m", "bravoric_ssh_client"]
            if app._config.path:
                tui += ["-c", str(app._config.path)]
            if result.rotation:
                tui += ["--rotation", result.rotation]
            os.environ["BRAVORIC_RESTART_COMMAND"] = " ".join(_sh_quote(a) for a in tui)
        if result.kind == "attach":
            tmux_runner.tmux_attach(
                result.host,
                result.session or "",
                password,
                jump_host=jump,
                jump_password=jump_password,
                audit=app._audit_for(result.host, result.session),
            )
        elif result.kind == "attach_ro":
            tmux_runner.tmux_attach_ro(
                result.host,
                result.session or "",
                password,
                jump_host=jump,
                jump_password=jump_password,
                audit=app._audit_for(result.host, result.session),
            )
        elif result.kind == "new":
            tmux_runner.tmux_new(
                result.host,
                result.name,
                password,
                jump_host=jump,
                jump_password=jump_password,
                audit=app._audit_for(result.host, result.name),
            )
        elif result.kind == "shell":
            tmux_runner.ssh_shell(
                result.host,
                password,
                jump_host=jump,
                jump_password=jump_password,
                audit=app._audit_for(result.host),
            )
        # NB: non c'è più un rilancio della TUI qui. L'exec in _run_or_exec sostituisce il
        # processo, quindi il codice dopo le chiamate tmux_runner.* non viene mai eseguito:
        # il rilancio è gestito da BRAVORIC_RESTART_COMMAND (impostato più sopra).


if __name__ == "__main__":
    main()
