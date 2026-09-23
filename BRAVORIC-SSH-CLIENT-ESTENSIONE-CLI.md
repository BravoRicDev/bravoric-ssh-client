# BRAVORIC-SSH-CLIENT — Estensione CLI: analisi funzionalità e bug

> Data: 2026-09-23  
> Scope: inventario di ogni funzionalità oggi presente nella TUI e ogni operazione oggi lanciabile da CLI, più un audit bug.  
> **Nessuna modifica al codice** — documentazione sola.

---

## 1. Funzionalità presenti nella TUI (Textual)

### 1.1 HostScreen — Lista host configurati

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Apri sessioni host | `Enter` | Entra nella SessionScreen dell'host selezionato |
| Aggiungi host | `a` | Apre HostFormScreen (nuovo host) |
| Modifica host | `e` | Apre HostFormScreen pre-compilato |
| Elimina host | `d` | Conferma eliminazione (ConfirmScreen) |
| Duplica host | `D` | Clona l'host con suffisso "-copia" |
| Gestisci password | `p` | Apre PasswordScreen (salva/rimuovi password dal keyring) |
| Test connessione | `t` | TCP ping sull'host selezionato |
| Importa host | `i` | Importa da `~/.ssh/config` e Remmina (flatpak+legacy) |
| Cicla filtro gruppo | `g` | Tutti → gruppo1 → gruppo2 → … → tutti |
| Ping tutti gli host | `T` | TCP ping parallelo su tutti gli host visibili |
| Copia file (SCP) | `c` | Apre ScpScreen (upload/download) |
| Scambio file (MC) | `F` | Apre SftpTargetScreen → Midnight Commander |
| Sessioni recenti | `h` | Apre RecentScreen |
| Osserva sessioni | `o` | Apre ObserveScreen (mono-host) |
| Rotazioni | `R` | Apre RotationCatalogScreen |
| Tunnel SSH | `u` | Apre TunnelScreen (gestione tunnel host) |
| Broadcast snippet | `B` | Apre SnippetCatalogScreen |
| Avvia agente localhost | `Ctrl+Shift+N` | Apre LaunchAgentScreen per localhost |
| Ricarica config | `r` | Ricarica il TOML da disco |
| Esci | `q` | Chiude la TUI |

**Indicatori visivi nella lista host:**
- `✓` / `✗` / `·` — raggiungibilità TCP (ping)
- `🔑` / spazio — password presente nel keyring
- `🟢` / `🔴` / vuoto — tunnel attivi / configurati spenti / nessun tunnel

### 1.2 SessionScreen — Sessioni tmux di un host

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Attach sessione | `Enter` | Entra nella sessione tmux (sostituisce terminale) |
| Nuova sessione | `n` | InputScreen → nome → `tmux_new` |
| Rinomina sessione | `r` | InputScreen → nuovo nome → `tmux_rename_session` |
| Kill sessione | `k` | Conferma → `tmux_kill_session` |
| Kill server tmux | `K` | Conferma → `tmux_kill_server` |
| Shell interattiva | `s` | `ssh_shell` (sostituisce terminale) |
| Attach read-only | `R` | Attach senza audit log |
| Dettagli sessione | `d` | InfoScreen con dettagli tmux |
| Finestre | `w` | WindowsScreen |
| Nuova finestra | `W` | InputScreen → nome → `tmux_new_window` |
| Detach client | `D` | Stacca altri client dalla sessione |
| Refresh | `g` | Ricarica lista sessioni |
| Info pane | `i` | PaneInfoScreen (processo, CWD, PID, titolo, geometria) |
| Lancia agente AI | `a` | LaunchAgentScreen |
| Invia testo | `P` | SendTextScreen (multiriga, bracketed paste) |
| Invia file | `F` | InputScreen → percorso file → invia contenuto come bracketed paste |
| Copia buffer | `y` | Legge tmux show-buffer e copia negli appunti (`wl-copy`) |
| Esci | `q` | Torna a HostScreen |

### 1.3 HostFormScreen — Aggiungi/Modifica host

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Salva | `Ctrl+S` | Valida e salva host in config (con backup) |
| Annulla | `Esc` | Chiude senza salvare |

**Campi:** alias, host, user, porta, auth, cred_key, gruppo, jump host, auto-rotate, intervallo rotazione.

### 1.4 PasswordScreen — Gestione password

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Salva password | `Ctrl+S` | Memorizza nel keyring |
| Rimuovi password | `Ctrl+D` | Cancella dal keyring |
| Annulla | `Esc` | Chiude |

### 1.5 ScpScreen — Copia file SCP

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Upload | `u` | Locale → server |
| Download | `d` | Server → locale |
| Annulla | `Esc` | Chiude |

### 1.6 SftpTargetScreen — Scelta target scambio file

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Apri commander | `Enter` | LaunchAgentScreen → Midnight Commander (host locale o altro host) |
| Indietro | `Esc` / `q` | Torna a HostScreen |

### 1.7 TunnelScreen — Gestione tunnel SSH

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Nuovo tunnel | `n` | TunnelFormScreen |
| Avvia tunnel | `s` | Avvia tunnel selezionato |
| Ferma tunnel | `x` | Ferma tunnel selezionato |
| Avvia tutti | `a` | Avvia tutti i tunnel dell'host |
| Ferma tutti | `X` | Ferma tutti i tunnel dell'host |
| Rimuovi tunnel | `d` | Conferma → ferma e rimuove |
| Indietro | `Esc` / `q` | Torna a HostScreen |

### 1.8 TunnelFormScreen — Form tunnel

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Salva | `Ctrl+S` | Valida e aggiunge tunnel all'host |
| Annulla | `Esc` | Chiude |

**Campi:** nome, tipo (L/R/D), porta locale, host destinazione, porta destinazione, bind.

### 1.9 WindowsScreen — Finestre tmux

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Rinomina finestra | `r` | InputScreen → nuovo nome → `tmux_rename_window` |
| Chiudi finestra | `k` | Conferma → `tmux_kill_window` |
| Indietro | `Esc` / `q` | Torna a SessionScreen |

### 1.10 RecentScreen — Sessioni recenti

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Rientra nella sessione | `Enter` | Apre SessionScreen dell'host |
| Riapri tutte | `a` | Apre tutte le sessioni recenti in finestre separate (ptyxis/gnome-terminal) |
| Indietro | `Esc` / `q` | Torna a HostScreen |

### 1.11 RotationCatalogScreen — Catalogo rotazioni

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Osserva rotazione | `Enter` | Apre ObserveScreen con le sessioni della rotazione |
| Nuova rotazione | `n` | RotationCreateScreen |
| Elimina rotazione | `d` | Conferma → rimuove dal file rotazioni |
| Riapri tutte | `a` | Apre tutte le sessioni di tutte le rotazioni in finestre separate |
| Indietro | `Esc` / `q` | Torna a HostScreen |

### 1.12 RotationCreateScreen — Crea rotazione

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Seleziona host | `Enter` | Toggle host → apre SessionPickScreen |
| Salva profilo | `Ctrl+S` | InputScreen → nome → salva Rotation |
| Avvia ora | `s` | Apre ObserveScreen con le sessioni selezionate |
| Indietro | `Esc` / `q` | Torna a RotationCatalogScreen |

### 1.13 SessionPickScreen — Selezione sessioni per rotazione

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Toggle selezione | `Enter` | Seleziona/deseleziona sessione |
| Indietro | `Esc` / `q` | Torna a RotationCreateScreen |

### 1.14 ObserveScreen — Osservazione sessioni (auto-rotation)

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Sessione successiva | `n` | Vai alla vista successiva |
| Sessione precedente | `p` | Vai alla vista precedente |
| Toggle rotazione | `s` | Attiva/disattiva auto-rotation |
| Refresh | `R` | Ricarica le viste |
| Vai a sessione | `g` | InputScreen → host/sessione → salta a quella vista |
| Attach | `Enter` | Entra nella sessione (LaunchAction attach) |
| Modalità interattiva | `i` | Toggle input per inviare comandi |
| Invia senza Enter | `Ctrl+S` | Invia testo senza conferma |
| Toggle info | `Ctrl+T` | Mostra/nascondi barra info |
| Esci | `Esc` / `q` | Torna allo schermo precedente |

**Auto-rotation:** ogni `interval` secondi (default 120) cambia automaticamente sessione.

### 1.15 InputScreen — Input testuale generico

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Conferma | `Ctrl+S` | Invia valore alla callback |
| Suggerimento precedente | `↑` | Naviga suggerimenti |
| Suggerimento successivo | `↓` | Naviga suggerimenti |
| Annulla | `Esc` | Chiude |

### 1.16 InfoScreen — Visualizzazione testo

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Chiudi | `Esc` / `q` | Torna allo schermo precedente |

### 1.17 PaneInfoScreen — Info dettagliate pane

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Sessione successiva | `n` | Vai alla sessione successiva |
| Sessione precedente | `p` | Vai alla sessione precedente |
| Refresh | `r` | Ricarica info pane |
| Chiudi | `Esc` / `q` | Torna a SessionScreen |

**Mostra:** processo, CWD, PID, titolo, geometria, flag is_shell.

### 1.18 QuickLaunchScreen — Lancio rapido agente

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Seleziona agente | `Tab` / `Invio` | Bottone dell'agente |
| Apri terminale | `Tab` → "Terminale" | Bottone terminale |
| Indietro | `Esc` | Torna a HostScreen |

**Rilevamento automatico:** verifica quali agenti sono installati sull'host via SSH.

### 1.19 LaunchAgentScreen — Lancia agente AI

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Lancia agente | `Ctrl+S` | Crea sessione tmux detached, invia comando agente |
| Rileva agenti | `Ctrl+R` | Ririleva agenti installati |
| Annulla | `Esc` | Torna a SessionScreen |

**Campi:** agente (Select), directory di lavoro, modello (Select, opzionale), prompt iniziale, timeout, titolo sessione, force, argomenti extra (expert).

**Flusso:** rileva agenti → carica modelli → costruisce comando → crea sessione tmux → invia comando → polling caricamento TUI → attach.

### 1.20 SendTextScreen — Invia testo libero

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Invia | `Ctrl+S` | Invia testo multiriga (bracketed paste) |
| Indietro | `Esc` | Torna a SessionScreen |

### 1.21 SnippetCatalogScreen — Catalogo snippet

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Esegui snippet | `Enter` | BroadcastHostPickerScreen |
| Nuovo snippet | `n` | SnippetFormScreen |
| Elimina snippet | `d` | Conferma → rimuove snippet |
| Indietro | `Esc` / `q` | Torna a HostScreen |

### 1.22 SnippetFormScreen — Crea snippet

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Salva | `Ctrl+S` | Nome + comando obbligatori |
| Annulla | `Esc` | Chiude |

### 1.23 BroadcastHostPickerScreen — Seleziona host per broadcast

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Toggle host | `Space` | Marca/desmarcca host |
| Tutti | `a` | Seleziona/deseleziona tutti |
| Toggle modalità | `t` | tmux (nuova sessione) ↔ diretta (ssh batch) |
| Esegui | `Enter` | BroadcastResultScreen |
| Indietro | `Esc` | Torna a SnippetCatalogScreen |

### 1.24 BroadcastResultScreen — Risultati broadcast

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Indietro | `Esc` / `q` | Torna a BroadcastHostPickerScreen |

**Modalità tmux:** mostra sessione creata per host (da attachare).  
**Modalità diretta:** mostra stdout/stderr/exit code.

### 1.25 ConfirmScreen — Conferma generica

| Azione | Tasto | Descrizione |
|--------|-------|-------------|
| Conferma | `y` | Esegue la callback |
| Annulla | `n` / `Esc` | Chiude |

### 1.26 BravoricApp — Schermata iniziale e routing

| Comportamento | Descrizione |
|---------------|-------------|
| Avvio senza flag | HostScreen |
| `--quick-launch` | HostScreen + QuickLaunchScreen |
| `--launch-agent` | HostScreen + LaunchAgentScreen |
| `--rotation <nome>` | HostScreen + ObserveScreen (rotazione) |

---

## 2. Funzionalità lanciabili da CLI

### 2.1 Flag CLI del comando `bravoric-ssh`

| Flag | Sintassi | Descrizione | Post-azione |
|------|----------|-------------|-------------|
| `-c` / `--config` | `-c /percorso/config.toml` | Percorso configurazione | Prosegue con altri flag |
| `--attach` | `--attach <host> <session>` | Attach diretto a sessione tmux | Sostituisce terminale (exec) |
| `--attach-ro` | `--attach-ro <host> <session>` | Attach read-only (no audit) | Sostituisce terminale (exec) |
| `--shell` | `--shell <host>` | Shell interattiva SSH | Sostituisce terminale (exec) |
| `--new` | `--new <host> [nome]` | Crea/rientra in sessione tmux | Sostituisce terminale (exec) |
| `--rotation` | `--rotation <nome>` | Avvia rotazione salvata | TUI + ObserveScreen |
| `--sftp` | `----sftp <host_a> <host_b>` | Midnight Commander su due host | Esce (MC nel terminale) |
| `--launch-agent` | `--launch-agent` | Apri TUI + LaunchAgentScreen per localhost | TUI |
| `--quick-launch` | `--quick-launch` | Apri TUI + QuickLaunchScreen | TUI |

### 2.2 Modalità CLI (senza TUI)

1. **`--attach` / `--attach-ro`**: esegue `tmux_attach` / `tmux_attach_ro` → sostituisce il terminale con la sessione tmux via `ssh -t`.
2. **`--shell`**: esegue `ssh_shell` → shell interattiva sull'host.
3. **`--new`**: esegue `tmux_new` → crea nuova sessione tmux → attach.
4. **`--sftp`**: esegue `_sftp_cli` → Midnight Commander sui due host.
5. **`--rotation`**: carica la rotazione e apre ObserveScreen nella TUI.

### 2.3 Comportamento post-exec

- Le modalità `--attach`, `--attach-ro`, `--shell`, `--new` usano `os.execv*` (POSIX) o `subprocess` (Windows): **sostituiscono il processo corrente**, il terminale rimane agganciato alla sessione tmux/ssh.
- La modalità `--sftp` lancia `mc` e **esce** (il processo bravoric-ssh termina).
- Le modalità `--rotation`, `--launch-agent`, `--quick-launch` aprono la TUI e **restano**.

### 2.4 Entry point alternativi

- `python -m bravoric_ssh_client` — stesso comportamento di `bravoric-ssh`
- `bravoric-ssh-mcp` — server MCP (non CLI interattiva)

### 2.5 Funzionalità TUI-ONLY (non raggiungibili da CLI)

Le seguenti operazioni **non** hanno un flag CLI corrispondente e sono accessibili solo dalla TUI:

| Funzionalità | Schermata TUI |
|-------------|---------------|
| Gestione host (CRUD) | HostScreen + HostFormScreen |
| Gestione password | PasswordScreen |
| SCP upload/download | ScpScreen |
| Tunnel SSH (CRUD, start/stop) | TunnelScreen + TunnelFormScreen |
| Finestre tmux (rinomina, kill) | WindowsScreen |
| Dettagli sessione | InfoScreen |
| Info pane | PaneInfoScreen |
| Broadcast snippet | SnippetCatalogScreen → BroadcastHostPickerScreen → BroadcastResultScreen |
| Snippet (CRUD) | SnippetCatalogScreen + SnippetFormScreen |
| Rotazioni (CRUD) | RotationCatalogScreen + RotationCreateScreen |
| Invia testo libero | SendTextScreen |
| Invia file alla pane | SessionScreen `F` |
| Copia buffer negli appunti | SessionScreen `y` |
| Ping tutti gli host | HostScreen `T` |
| Import host | HostScreen `i` |
| RecentScreen con "riapri tutte" | RecentScreen `a` |
| Osservazione multi-host | ObserveScreen (multi-host) |
| Selezione sessioni per rotazione | SessionPickScreen |

---

## 3. Audit Bug

### 3.1 Critici / High

| # | Severità | Categoria | Localizzazione | Descrizione |
|---|----------|-----------|----------------|-------------|
| 1 | **High** | Race condition | `ObserveScreen._rotate_check` (app.py ~1900) | La rotazione automatica controlla `time.monotonic() - self._last_input >= self._interval` ma `_last_input` viene aggiornata solo in `_reset_timer()` che è chiamata da `on_key`. Se l'utente preme un tasto durante la cattura (`_capture`), il timer si resetta ma la cattura potrebbe essere sostituita da una più vecchia (token mismatch protegge solo parzialmente). |
| 2 | **High** | Resource leak | `ScpScreen._run_in_thread` (app.py ~1080) | Il thread executor non viene mai terminato dopo l'uso. Thread accumulati per ogni upload/download. |
| 3 | **High** | Logic bug | `SessionScreen._copy_buffer` (app.py ~3650) | Usa `wl-copy` che è specifico Linux Wayland. Su X11 serve `xclip`, su macOS `pbcopy`. Nessun fallback. |
| 4 | **High** | Security | `LaunchAgentScreen._launch_agent` (app.py ~2800) | Il comando agente è costruito con string concatenation (`agent_cmd = " ".join(cmd_parts)`), poi inviato via `tmux_send_input`. Se il modello o gli argomenti contengono caratteri speciali, potrebbe esserci injection nel tmux command. |
| 5 | **High** | Missing error handling | `BroadcastResultScreen._execute` (app.py ~3200) | L'eccezione generica `except Exception as exc` cattura tutto ma non distingue errori SSH da errori locali. L'utente non sa se è un problema di rete o di configurazione. |

### 3.2 Medium

| # | Severità | Categoria | Localizzazione | Descrizione |
|---|----------|-----------|----------------|-------------|
| 6 | **Medium** | Inconsistency | `HostScreen.action_duplicate_host` (app.py ~640) | Il duplicato non copia `group`, `jump_host`, `auto_cycle`, `cycle_interval`, `tunnels`. L'utente si aspetta una copia completa. |
| 7 | **Medium** | Dead code / unreachable | `SendTextScreen` binding (app.py ~2950) | `Binding("escape", "app.pop_screen", ...)` — il binding chiama `app.pop_screen` direttamente invece di `action_cancel`. Funziona ma è un pattern inconsistente con le altre schermate. |
| 8 | **Medium** | Test gap | `tests/test_features_tui.py` | Le schermate `SendTextScreen`, `InfoScreen`, `PaneInfoScreen`, `QuickLaunchScreen`, `BroadcastResultScreen` non hanno test dedicati. |
| 9 | **Medium** | Test gap | `tests/test_features_ssh.py` | Le funzioni `tmux_send_input`, `tmux_paste_buffer`, `tmux_new_window` non sono testate. |
| 10 | **Medium** | Logic bug | `RotationCreateScreen._populate_hosts` (app.py ~1600) | Quando un host è selezionato e ha già sessioni, `_load_sessions` è async ma `_populate_hosts` è sync — il toggle dell'checkbox avviene prima che le sessioni siano caricate, mostrando stati inconsistenti. |
| 11 | **Medium** | UX bug | `TunnelScreen._describe` (app.py ~1180) | Per tunnel di tipo "D" (SOCKS) mostra `SOCKS {local_addr}` ma il formato è diverso da L/R che mostra `L local→remote`. Inconsistente. |
| 12 | **Medium** | Potential crash | `ObserveScreen._goto_named` (app.py ~2050) | Se `suggestions` è vuoto e l'utente preme `g`, `suggestions[self._current]` crasha con IndexError. |

### 3.3 Low / Info

| # | Severità | Categoria | Localizzazione | Descrizione |
|---|----------|-----------|----------------|-------------|
| 13 | **Low** | Code smell | `app.py` — `_io` helper | `_io` è definita come `async def` ma usa `asyncio.to_thread`. Per operazioni molto frequenti (polling ObserveScreen ogni 0.4s) crea overhead di thread creation. Meglio un pool riutilizzabile. |
| 14 | **Low** | Code smell | `app.py` — `BravoricApp._load_config` | Catch generico `except Exception as exc` — nasconde errori di permessi, TOML malformato, ecc. senza distinguere. |
| 15 | **Low** | Inconsistency | `HostScreen` hint label | Il hint mostra `F: scambio file` ma la binding è `action_file_exchange` (lettera `F` maiuscola). Coerente, ma il testo hint dice "Scambio file" mentre la funzione è `SftpTargetScreen`. |
| 16 | **Low** | Unused import | `app.py` — `shutil` importato a livello module | Usato solo in `RecentScreen._reopen_all_worker` e `SftpTargetScreen._launch`. Potrebbe essere importato localmente. |
| 17 | **Low** | Hardcoded paths | `app.py` — `wrap = str(Path.home() / ".local" / "bin" / "bravoric-ssh")` | Il path dello script wrapper è hardcoded. Se installato in un altro posto (es. `/usr/local/bin`), il "riapri tutte" non funziona. |
| 18 | **Low** | Magic number | `ObserveScreen.POLL_SECONDS = 1.5` | Valore magic. Non documentato perché 1.5s e non 1s o 2s. |
| 19 | **Low** | Potential leak | `LaunchAgentScreen._launch_agent` | La sessione tmux creata resta sul server se l'utente non la pulisce. Nessun timeout di auto-terminazione. |
| 20 | **Low** | Missing validation | `HostFormScreen.action_save` | Il campo `alias` non è validato per caratteri speciali che potrebbero causare problemi nei comandi tmux/ssh. |

### 3.4 Potenziali bug nei moduli SSH

| # | Severità | Localizzazione | Descrizione |
|---|----------|----------------|-------------|
| 21 | **Medium** | `ssh/adapter.py` — `run_tmux_action` | Se `remote_cmd` contiene apici o caratteri speciali, non viene quotato prima dell'esecuzione. Possibile injection nel comando tmux remoto. |
| 22 | **Medium** | `ssh/file_ops.py` — `_run_scp` | Il file locale non viene verificato per esistenza prima della chiamata scp. L'errore di scp è ambiguo (file non trovato vs permission denied). |
| 23 | **Low** | `ssh/tunnels.py` — `TunnelManager.start` | Se un tunnel è già attivo sulla stessa porta, il restart non ferma prima quello vecchio. Possibile conflitto di porte. |
| 24 | **Low** | `ssh/audit.py` — `audit_log_path` | Il percorso del log di audit è calcolato ad ogni chiamata. Se `audit_log` è `True` ma la directory non esiste, l'audit fallisce silenziosamente. |
| 25 | **Info** | `ssh/broadcast.py` — `run_snippet_on_hosts_tmux` | In modalità tmux, se la creazione della sessione fallisce per un host, gli altri host continuano. Non c'è rollback. |

---

## 4. Riepilogo: cosa manca alla CLI

Le seguenti funzionalità TUI-ONLY potrebbero beneficiare di un flag CLI dedicato:

| Funzionalità TUI | Flag CLI proposto | Descrizione |
|------------------|-------------------|-------------|
| Gestione host CRUD | `--host-add`, `--host-edit`, `--host-delete` | Aggiungere/modificare/eliminare host dalla config |
| Gestione tunnel | `--tunnel-start <host> <tunnel>`, `--tunnel-stop` | Avviare/fermare tunnel senza TUI |
| Gestione rotazioni | `--rotation-list`, `--rotation-add`, `--rotation-remove` | CRUD rotazioni da CLI |
| Gestione snippet | `--snippet-run <nome> [--host <alias>]` | Eseguire snippet su host specifico |
| Broadcast | `--broadcast <comando> --hosts <alias1,alias2>` | Eseguire comando broadcast da CLI |
| Invia testo | `--send-text <host> <session> <testo>` | Inviare testo a una sessione |
| Invia file | `--send-file <host> <session> <percorso>` | Inviare file a una sessione |
| Info pane | `--pane-info <host> <session>` | Mostrare info pane |
| Lista sessioni | `--list-sessions <host>` | Elencare sessioni tmux (JSON output) |
| Lista host | `--list-hosts` | Elencare host configurati (JSON output) |
| Test connessione | `--ping <host>` | TCP ping (già esiste come `t` in TUI) |
| Copia buffer | `--copy-buffer <host> <session>` | Copiare buffer negli appunti |

---

## 5. File analizzati

- `bravoric_ssh_client/app.py` — applicazione TUI principale (4861 righe)
- `bravoric_ssh_client/config.py` — modello configurazione
- `bravoric_ssh_client/ssh/adapter.py` — adapter SSH/tmux
- `bravoric_ssh_client/ssh/tmux_runner.py` — runner tmux
- `bravoric_ssh_client/ssh/commander.py` — Midnight Commander launcher
- `bravoric_ssh_client/ssh/file_ops.py` — operazioni file SCP/SFTP
- `bravoric_ssh_client/ssh/broadcast.py` — broadcast snippet
- `bravoric_ssh_client/ssh/tunnels.py` — tunnel manager
- `bravoric_ssh_client/ssh/inspection.py` — ispezione pane
- `bravoric_ssh_client/ssh/audit.py` — audit logging
- `bravoric_ssh_client/ssh/shellutil.py` — utility shell
- `bravoric_ssh_client/rotation.py` — gestione rotazioni
- `bravoric_ssh_client/history.py` — storia sessioni
- `bravoric_ssh_client/snippets.py` — gestione snippet
- `bravoric_ssh_client/importers.py` — import Remmina/SSH config
- `bravoric_ssh_client/credentials/` — gestione credenziali
- `tests/test_features_tui.py` — test TUI
- `tests/test_features_ssh.py` — test SSH
- `tests/test_tmux_actions_ui.py` — test azioni tmux
