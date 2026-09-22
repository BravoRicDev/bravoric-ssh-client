"""Applicazione TUI bravoric-ssh-client.

Avvio: ``bravoric-ssh`` oppure ``python -m bravoric_ssh_client``.
Flusso: lista host -> (Enter) schermata sessioni tmux -> attach/crea/shell.
"""

from __future__ import annotations

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
    ):
        super().__init__()
        self._config_path = config_path
        self._config = config
        self._start_rotation = start_rotation
        self.ssh_cfg: SshConfig | None = None
        self.tunnels = TunnelManager()

    def on_mount(self) -> None:
        self._load_config()
        if self._start_rotation:
            self._open_rotation(self._start_rotation)
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
        Binding("meta+n", "launch_agent_localhost", "Avvia agente su localhost"),
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
            "u: tunnel · B: broadcast · r: ricarica · q: esci · Meta+N: avvia agente su localhost",
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
                auth="none",
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
        import shutil
        import time

        unique: dict[str, tuple[str, str]] = {}
        for e in self._entries:
            if self._config.host(e.host):
                unique.setdefault(f"{e.host}/{e.session}", (e.host, e.session))
        if not unique:
            self.app.notify("Nessuna sessione da riaprire")
            return
        ptyxis = shutil.which("ptyxis") or shutil.which("gnome-terminal")
        wrap = str(Path.home() / ".local" / "bin" / "bravoric-ssh")
        launched = 0
        for host_alias, session in unique.values():
            if ptyxis:
                import subprocess

                try:
                    # ptyxis -x vuole il comando come UNA stringa; eseguita via shell
                    cmd = f"sh -c {_sh_quote(f'exec {_sh_quote(wrap)} --attach {_sh_quote(host_alias)} {_sh_quote(session)}')}"
                    subprocess.Popen(
                        [ptyxis, "-x", cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched += 1
                    # Piccolo delay per non sovraccaricare macchine lente o server
                    if launched < len(unique):
                        time.sleep(0.5)
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
        ptyxis = shutil.which("ptyxis") or shutil.which("gnome-terminal")
        wrap = str(Path.home() / ".local" / "bin" / "bravoric-ssh")
        if not ptyxis:
            self.app.notify("Nessun terminale GUI trovato; apri mc manualmente", severity="error")
            return
        # ptyxis -x esegue il PRIMO token come eseguibile: serve "sh -c '<comando unico>'".
        cmd = f"sh -c {_sh_quote(f'exec {_sh_quote(wrap)} --sftp {_sh_quote(host_a.alias)} {_sh_quote(host_b.alias)}')}"
        try:
            subprocess.Popen(
                [ptyxis, "-x", cmd],
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
        import time

        unique: dict[str, tuple[str, str]] = {}
        for r in self._rotations:
            for h, s in r.unique_entries():
                if self._config.host(h):
                    unique.setdefault(f"{h}/{s}", (h, s))
        if not unique:
            self.app.notify("Nessuna sessione da riaprire")
            return
        ptyxis = shutil.which("ptyxis") or shutil.which("gnome-terminal")
        wrap = str(Path.home() / ".local" / "bin" / "bravoric-ssh")
        launched = 0
        for host_alias, session in unique.values():
            if ptyxis:
                try:
                    # ptyxis -x vuole il comando come UNA stringa; eseguita via shell
                    cmd = f"sh -c {_sh_quote(f'exec {_sh_quote(wrap)} --attach {_sh_quote(host_alias)} {_sh_quote(session)}')}"
                    subprocess.Popen(
                        [ptyxis, "-x", cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched += 1
                    # Piccolo delay per non sovraccaricare macchine lente o server
                    if launched < len(unique):
                        time.sleep(0.5)
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

        while True:
            await asyncio.sleep(1.0)
            if not self._started:
                continue
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

        while True:
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


class LaunchAgentScreen(BravoricScreen):
    """Schermata per lanciare un agente AI (opencode, claude, pi) in tmux detached.

    Permette di configurare: agente, directory di lavoro, argomenti extra, titolo,
    prompt iniziale, e opzioni avanzate (force, timeout, capture lines).
    Il lancio avviene in background e la schermata mostra lo stato di avanzamento.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Annulla"),
        Binding("ctrl+s", "submit", "Lancia"),
        Binding("ctrl+r", "refresh", "Refresh"),
    ]

    CSS = """
    LaunchAgentScreen #agent-form Input { margin: 0 0 1 0; }
    LaunchAgentScreen #status-box { height: 3; width: 80%; }
    LaunchAgentScreen #output-box { height: 1fr; width: 80%; }
    """

    _AGENTS = ["opencode", "claude", "pi"]

    def __init__(self, host: Host, config: Config):
        super().__init__()
        self._host = host
        self._config = config
        self._status_text = Static("Pronto per il lancio", id="status-box", classes="hint")
        self._output = Static("", id="output-box")

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="agent-form", classes="box"):
            yield Label("[b]Lancia agente AI[/b]", classes="box-title")
            yield Label(f"Host: {self._host.alias}")
            yield Label("Agente:")
            yield Select(
                [(a, a) for a in self._AGENTS],
                value=self._AGENTS[0],
                id="agent",
                allow_blank=False,
            )
            yield Label("Directory di lavoro:")
            yield Input(id="path", placeholder="Lascia vuoto per home")
            yield Label("Titolo (opzionale):")
            yield Input(id="title", placeholder="nome sessione")
            yield Label("Argomenti extra (opzionale):")
            yield Input(id="extra", placeholder='--model gpt-5, run "prompt"')
            yield Label("Prompt iniziale (opzionale, multiriga con \\n):")
            yield Input(id="prompt", placeholder="prompt da inviare all'agente")
            yield Label("Timeout attesa (s):")
            yield Input(id="timeout", placeholder="25", value="25")
            yield Label("Force (ricrea se esiste):")
            yield Input(id="force", placeholder="true / false", value="false")
            yield Label("Ctrl+S: lancia · Ctrl+R: refresh · Esc: annulla", classes="hint")
        yield self._status_text
        yield self._output
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#agent", Select).focus()

    def action_refresh(self) -> None:
        self._status_text.update("Refresh...")
        self._output.update("")

    async def action_submit(self) -> None:
        agent = str(self.query_one("#agent", Select).value or "")
        path = self.query_one("#path", Input).value.strip()
        if not path:
            # default to home
            path = str(Path.home())
        title = self.query_one("#title", Input).value.strip()
        extra = self.query_one("#extra", Input).value.strip()
        prompt = self.query_one("#prompt", Input).value.strip()
        timeout_str = self.query_one("#timeout", Input).value.strip()
        force_str = self.query_one("#force", Input).value.strip().lower()

        if not agent:
            self.app.notify("Seleziona un agente", severity="error")
            return
        if agent not in self._AGENTS:
            self.app.notify(
                f"Agente non valido. Scegli tra: {', '.join(self._AGENTS)}", severity="error"
            )
            return

        try:
            timeout = int(timeout_str)
        except ValueError:
            timeout = 25
        force = force_str in ("true", "1", "yes")

        self._status_text.update("Lancio agente in corso...")
        self._output.update("")

        await self._launch_agent(agent, path, title, extra, prompt, timeout, force)

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

        # 1. Pre-check: directory e binario
        self._status_text.update("Controllo pre-requisiti...")
        pre = await _io(
            ssh_adapter.run_tmux_action,
            self._host,
            f"bash -lc 'if [ -d {q(path)} ]; then echo PATH_OK; else echo PATH_MISSING; fi ; "
            f"if command -v {q(agent)} >/dev/null 2>&1; then echo BIN_OK; "
            f"else echo BIN_MISSING; fi'",
            cfg,
        )
        pre_out = pre.stdout or ""
        if "PATH_MISSING" in pre_out:
            self._status_text.update(f"[red]Errore: directory inesistente: {path}[/]")
            return
        if "BIN_MISSING" in pre_out:
            self._status_text.update(
                f"[red]Errore: binario '{agent}' non trovato su {self._host.alias}[/]"
            )
            return

        # 2. Nome sessione
        sess_title = title or f"{agent}-{Path(path).name or 'agent'}"
        # slug sicuro
        import re

        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", sess_title.strip())
        slug = re.sub(r"-{2,}", "-", slug).strip("-_")[:40] or f"{agent}-{_time.time():.0f}"
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
            if not force:
                self._status_text.update(
                    f"[red]Sessione '{slug}' già esistente (usa Force=true per ricreare)[/]"
                )
                return
            await _io(
                ssh_adapter.run_tmux_action,
                self._host,
                f"tmux kill-session -t {q(slug)} 2>/dev/null",
                cfg,
            )

        # 4. Crea sessione detached
        self._status_text.update("Creazione sessione tmux...")
        res = await _io(
            ssh_adapter.run_tmux_action,
            self._host,
            f"tmux new -d -s {q(slug)} -c {q(path)} 'exec bash -l'",
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

        # 7. Invio comando agente
        cmd = f"{agent} {extra_args}".strip()
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
            "w: finestre · D: detach · g: aggiorna · s: shell · Esc: indietro",
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
    finally:
        if helper is not None:
            try:
                helper.unlink(missing_ok=True)
            except OSError:
                pass


def app_password_resolver(config):
    """Resolver password da usare per il commander (keyring)."""
    from .credentials.factory import resolve_password

    def resolver(host: Host) -> str | None:
        return resolve_password(config, host)

    return resolver


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = None
    attach_host = None
    attach_session = None
    rotation_name = None
    sftp_host_a = None
    sftp_host_b = None
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
        if argv[i] == "--rotation" and i + 1 < len(argv):
            rotation_name = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--sftp" and i + 2 < len(argv):
            sftp_host_a = argv[i + 1]
            sftp_host_b = argv[i + 2]
            i += 3
            continue
        i += 1
    app = BravoricApp(config_path=config_path, start_rotation=rotation_name)
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
        tmux_runner.tmux_attach(
            host,
            attach_session,
            password,
            jump_host=jump,
            jump_password=jump_password,
            audit=audit,
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
        # riavvia la TUI se richiesto (option restart_after_ssh)
        if app._config and app._config.restart_after_ssh:
            extra = []
            if result.rotation:
                extra = ["--rotation", result.rotation]
            main(["-c", str(app._config.path or ""), *extra])


if __name__ == "__main__":
    main()
