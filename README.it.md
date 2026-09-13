# bravoric-ssh-client

Client **SSH + tmux** a TUI.

L'ho scritto per me. È uno **strumento personale**, non un prodotto: lo pubblico
su GitHub soprattutto per mostrare *come ragiono* e come costruisco gli strumenti
che uso ogni giorno. Mostra i tuoi host, elenca le sessioni tmux attive su
ciascuno e permette di agganciarsi, creare, rinominare e terminare le sessioni
senza lasciare il terminale. Tutte le operazioni di rete girano in thread di
background, quindi l'interfaccia resta sempre reattiva. Nessun servizio esterno,
niente da installare sui server.

[![CI](https://github.com/BravoRicDev/bravoric-ssh-client/actions/workflows/ci.yml/badge.svg)](https://github.com/BravoRicDev/bravoric-ssh-client/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

> English? See [README.md](README.md).

## Su questo progetto

> **Questo è uno strumento personale.** L'ho scritto per risolvere un mio
> problema e lo uso ogni giorno. Lo condivido così com'è, per riferimento e
> curiosità, per mostrare come ragiono e come metto insieme i miei strumenti. Non
> è un prodotto supportato: nessuna roadmap, nessun SLA, nessuna promessa di
> *feature parity*. Issue e suggerimenti sono ben accetti, ma integro solo le
> modifiche che si adattano a come lo uso io.

<!-- TODO: aggiungere uno screenshot o una GIF della TUI, es. docs/assets/demo.gif -->

## Funzionalità

- **Lista host → sessioni tmux**, da tastiera, con filtro testuale live.
- **Gestione host**: aggiungi, modifica, elimina, duplica, raggruppa, credenziali
  per host.
- **Connettività**: ping TCP sulla porta SSH, ping massivo di tutti gli host
  visibili.
- **Gestione sessioni**: attach, attach in sola lettura (`tmux attach -r`), crea,
  rinomina, termina, termina l'intero server, dettagli, finestre, nuova finestra,
  stacca gli altri client.
- **Provider credenziali**: keyring di sistema (GNOME/KDE/macOS/Windows) oppure
  file locale `plain` con permessi `0600`; override per host (`keyring`, `plain`,
  `key`, `prompt`).
- **Import**: da `~/.ssh/config` e da profili Remmina, più un helper per copiare
  le password da Remmina nel keyring dell'app.
- **Scambio file**: copia `scp` e sessione **Midnight Commander** a due pannelli
  (locale ↔ remoto o remoto ↔ remoto) con gestione automatica delle password.
- **Tunnel SSH**: port forwarding `-L` / `-R` / `-D` in background, condivisi tra
  TUI e server MCP.
- **Jump host / bastion** (`-J` / ProxyJump) con helper askpass multi-host.
- **Broadcast di snippet**: esegui un comando su più host insieme, in una nuova
  sessione tmux detached per host oppure direttamente via SSH (in parallelo).
- **Profili di rotazione**: auto-cycle tra le pane di più sessioni, con modalità
  interattiva opzionale per inviare tasti alla sessione a fuoco.
- **Audit trail**: registra le sessioni interattive in log gzip (`pipe-pane` di
  tmux o `script`).
- **tmux locale**: aggiungi un host con `host = "localhost"` per gestire il tmux
  della macchina locale senza SSH.
- **Server MCP**: l'intero client è esposto come server Model Context Protocol su
  stdio, così un agente (es. opencode) può controllare host, sessioni, comandi,
  broadcast, tunnel e log audit.

## Requisiti

- Python **3.11+**
- client OpenSSH `ssh` / `scp`
- `tmux` sui server gestiti (e in locale per gli host locali)
- opzionale: `mc` (Midnight Commander) per la vista di scambio file
- opzionale: `script` (util-linux) per registrare le shell SSH
- opzionale: `ptyxis` / `gnome-terminal` per aprire le sessioni in finestre separate

## Installazione

```bash
git clone https://github.com/BravoRicDev/bravoric-ssh-client.git
cd bravoric-ssh-client
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # ometti [dev] per la sola esecuzione
```

Avvio:

```bash
.venv/bin/bravoric-ssh
```

oppure, se installato nel `PATH`:

```bash
bravoric-ssh
```

## Configurazione

File: `~/.config/bravoric-ssh-client/config.toml`
(il percorso si può forzare con la variabile d'ambiente `BRAVORIC_CONFIG`).

```toml
[credentials]
provider = "keyring"   # oppure "plain" (file locale, permessi 0600)

[[hosts]]
alias = "web-prod"
host  = "server.example.com"
user  = "deploy"
# group = "clienti"          # raggruppa gli host (tasto g nella lista)
# jump_host = "bastion"      # alias del bastion per ProxyJump (-J)
# auth = ""                  # default: usa il provider globale
# auth = "key"               # chiave SSH / agent (niente password)
# auth = "prompt"            # chiede ogni volta
```

Opzioni generali (`[general]`):

```toml
[general]
restart_after_ssh = false  # riapre la TUI dopo una sessione ssh
history_size = 10          # quante sessioni recenti conservare
audit_log = false          # registra le sessioni in log compressi (pipe-pane/script)
# snippets_file = "..."    # percorso del catalogo snippet (default: <config_dir>/snippets.json)
# tunnels_file = "..."     # percorso dello stato dei tunnel (default: <config_dir>/tunnels.json)
```

### Import rapido dal tuo ssh config

```bash
.venv/bin/python scripts/import_ssh_config.py            # -> ~/.config/.../config.toml
.venv/bin/python scripts/import_ssh_config.py --merge    # aggiunge solo host nuovi
```

### Import da Remmina

Premi `i` nella schermata host per importare sia `~/.ssh/config` sia i profili
SSH di Remmina (flatpak e legacy) senza duplicati. Per un host Remmina nuovo, la
password va impostata la prima volta con `p`.

### Import credenziali da Remmina / SSH Pilot

```bash
.venv/bin/python scripts/import_keyring_credentials.py   # copia le password nel keyring dell'app
```

## Uso della TUI

- **Schermata host**: frecce + `Enter` per aprire un host. `q` esce.
  - `a` aggiungi un host · `e` modifica · `d` elimina (con conferma) · `D` duplica
  - `p` imposta/rimuovi la password dell'host nel provider
  - `t` test di connettività (ping TCP sulla porta SSH) · `T` ping di tutti gli host visibili
  - `c` copia file (scp) · `F` scambio file via Midnight Commander
  - `u` tunnel SSH dell'host selezionato (forward `-L`/`-R`/`-D`, stato in lista)
  - `B` broadcast di uno snippet su più host (default: una nuova sessione tmux per host)
  - `i` importa host da `~/.ssh/config` e Remmina (merge)
  - `g` cicla i gruppi · `h` sessioni recenti · `R` rotazioni salvate · `o` osserva un host
  - `r` ricarica la config da disco · campo in alto = filtro live
  - un `🔑` accanto a un host indica una password salvata
- **Schermata sessioni**:
  - `Enter` aggancia · `R` aggancia in sola lettura (`tmux attach -r`)
  - `n` nuova sessione · `r` rinomina · `k` termina (con conferma) · `K` termina tutte
  - `d` dettagli sessione · `w` elenca/gestisci finestre (rinomina `r`, chiudi `k`)
  - `D` stacca gli altri client · `W` nuova finestra · `g` aggiorna
  - `s` apre una shell SSH semplice · `Esc` torna agli host

**Titolo finestra**: quando ti agganci a (o crei) una sessione, il titolo della
finestra del terminale diventa `HOST - SESSIONE`. Il client imposta la stringa di
titolo del tmux remoto durante l'attach e la ripristina al termine, quindi
funziona anche con `set-titles on` sul server.

**Riavvio automatico**: con `restart_after_ssh = true` in `[general]`, la TUI si
riapre automaticamente dopo essere usciti da una sessione ssh.

### Profili di rotazione

Se più agenti/processi lavorano in sessioni tmux distinte (anche su più server),
puoi creare un **profilo di rotazione**: la TUI mostra il contenuto di ogni
sessione (`capture-pane`) e, dopo `cycle_interval` secondi di inattività, passa
automaticamente alla successiva. Ogni input ferma la rotazione sulla sessione
corrente.

- `R` nella schermata host → rotazioni salvate (`Enter` per osservare, `n` per
  creare, `a` per riaprire tutte le sessioni del profilo in finestre separate).
- Config host: `auto_cycle = true` e `cycle_interval = 120` per abilitare la
  scelta automatica; i profili vengono salvati in `rotations.json`.

### Tmux locale

Aggiungi un host con `host = "localhost"` (o `127.0.0.1`, oppure `local = true`):
la TUI gestisce direttamente il tmux del PC senza SSH. Un host `localhost` è già
incluso nella config di default.

Ogni modifica agli host viene salvata su `config.toml` con **backup automatico**
(`config.toml.bak.<timestamp>`).

### Tunnel SSH (`u`)

Dall'host selezionato premi `u`: elenco dei tunnel configurati con stato
(🟢 attivo / 🔴 spento). `n` per aggiungerne uno (tipo `L`/`R`/`D`, porta,
destinazione), `s`/`x` per avviare/fermare il singolo, `a`/`X` per avviare/fermare
tutti, `d` per rimuoverlo. I tunnel girano come processi `ssh -N -T` in background
(password dal keyring in automatico) e le definizioni vengono salvate in config:

```toml
[[hosts]]
alias = "db-host"
host  = "server.example.com"
[[hosts.tunnels]]
name = "postgres"
kind = "L"
local_port = 5432
remote_host = "localhost"
remote_port = 5432
[[hosts.tunnels]]
kind = "D"          # SOCKS
local_port = 1080
```

### Jump host / Bastion (`jump_host`)

Un host non esposto direttamente si raggiunge tramite un bastion: in config si
imposta l'alias del bastion e il client inietta automaticamente `-J` (ProxyJump)
nelle connessioni, usando l'helper `SSH_ASKPASS` multi-host per le password di
entrambi (bastion e host finale):

```toml
[[hosts]]
alias = "app-interna"
host  = "10.0.0.9"
user  = "deploy"
jump_host = "bastion"
```

### Broadcast di snippet (`B`)

Premi `B` nella lista host: catalogo snippet (`snippets.json` nella dir config,
`n` per aggiungerne). Scegli uno snippet, marca gli host con `Space` (o `a` per
tutti) e premi `Enter`. Due modalità (tasto `t` per alternare, **default: tmux**):

- **Sessione tmux** (default): per ogni host viene creata una sessione tmux
  *detached* di nome `bcast-<snippet>-<host>-<YYYYMMDD-HHMMSS>` che esegue il
  comando. Se tmux non è installato sull'host, ripiega sulla modalità diretta.
- **Diretta (ssh batch)**: lo script gira in parallelo (thread pool) e i risultati
  (stdout/exit code) compaiono subito in griglia.

### Server MCP (`bravoric-ssh-mcp`)

L'intero client è esposto come **server MCP** su stdio: un agente (es. opencode)
può controllare host, sessioni tmux, comandi, broadcast, tunnel e log audit
esattamente come dalla TUI.

```bash
bravoric-ssh-mcp            # avvia il server (stdio)
```

Tool esposti (47), per gruppi:

- **Host**: `list_hosts`, `get_host`, `get_status`, `ping`, `ping_all`,
  `hosts_summary`, `tmux_present`
- **Panoramica**: `list_sessions_all`, `find_in_sessions`, `run_command_all`,
  `session_history`
- **Sessioni tmux**: `list_sessions`, `create_session`, `session_details`,
  `rename_session`, `kill_session`, `kill_server`, `detach_clients`,
  `list_windows`, `new_window`, `rename_window`, `kill_window`, `capture_pane`,
  `send_keys`, `send_enter`, `send_raw`
- **Esecuzione con attesa**: `run_and_wait`, `broadcast_wait`
- **Comandi**: `run_command`, `run_command_many`
- **Snippet/broadcast**: `list_snippets`, `add_snippet`, `remove_snippet`,
  `broadcast` (`tmux` | `direct`)
- **Tunnel**: `list_tunnels`, `list_tunnels_all`, `start_tunnel`, `stop_tunnel`,
  `stop_tunnels`, `tunnel_health`
- **Rotazioni**: `list_rotations`, `add_rotation`, `remove_rotation`
- **Audit**: `list_audit_logs`, `read_audit_log`, `list_remote_audit_logs`,
  `read_remote_audit_log`

Registrazione per opencode (globale, `~/.config/opencode/opencode.jsonc`):

```jsonc
"mcp": {
  "bravoric-ssh": {
    "type": "local",
    "command": ["bravoric-ssh-mcp"],
    "enabled": true,
    "environment": {}
  }
}
```

> Usa il percorso assoluto dell'eseguibile se non è nel `PATH`, es.
> `/path/to/venv/bin/bravoric-ssh-mcp`.

### Skill per gli agenti

Il repo include una skill (`docs/skill/SKILL.md`) con l'inventario dei tool, i
pattern d'uso (create detached → send-keys → capture-pane → kill), le convenzioni
di naming e la gestione degli errori.

```bash
# Claude Code
mkdir -p ~/.claude/skills/bravoric-ssh
cp docs/skill/SKILL.md ~/.claude/skills/bravoric-ssh/

# opencode
mkdir -p ~/.agents/skills/bravoric-ssh
cp docs/skill/SKILL.md ~/.agents/skills/bravoric-ssh/
```

### Uso headless (server senza GUI)

Il server MCP è un processo Python su stdio e non richiede un desktop:

- **Credenziali**: senza un keyring di sistema, usa il provider `plain`
  (`secrets.tsv`, permessi 0600):
  ```toml
  [credentials]
  provider = "plain"
  ```
- **Config**: punta `BRAVORIC_CONFIG` al file prima di avviare `bravoric-ssh-mcp`
  (o crea `~/.config/bravoric-ssh-client/config.toml`).
- Gli helper `SSH_ASKPASS` funzionano anche senza display
  (`SSH_ASKPASS_REQUIRE=force`). Tipico daemon systemd:
  ```ini
  [Service]
  Environment=BRAVORIC_CONFIG=/etc/bravoric-ssh-client/config.toml
  ExecStart=/opt/bravoric-ssh-client/.venv/bin/bravoric-ssh-mcp
  ```

### Audit trail (`audit_log = true`)

Le sessioni interattive vengono registrate in log compressi:

- **attach / nuova sessione tmux**: viene iniettato
  `tmux pipe-pane -o 'gzip -c >> file'` durante l'aggancio e chiuso al detach. Per
  gli host locali il file è su questa macchina
  (`<config_dir>/logs/YYYYMMDD_ALIAS_SESSION.log.gz`), per quelli remoti su
  `~/.bravoric-ssh-client/logs/` del server.
- **shell interattiva** (`s`): se `script` è installato, l'I/O viene registrato
  localmente; se assente la shell parte comunque senza log.

I file `.log.gz` sono pronti per parsing offline (`zgrep`, regex, ecc.).

> Quando scegli un'azione (attach/nuova/shell) la TUI esce *prima* di lanciare
> ssh, così il terminale viene ripristinato pulito; ssh prende il posto del
> processo e al suo termine torni al prompt.

## Test

```bash
.venv/bin/python -m pytest
```

Con coverage e lint:

```bash
.venv/bin/python -m pytest --cov=bravoric_ssh_client
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

## Contribuire

Questo è un progetto personale, condiviso per mostrare come costruisco i miei
strumenti. Vedi [CONTRIBUTING.md](CONTRIBUTING.md): è piccolo e modulare di
proposito, quindi segnalazioni di bug e pull request mirate con test sono ben
accette — ma integro solo le modifiche che si adattano allo spirito dello
strumento.

## Licenza

[MIT](LICENSE) © Riccardo (bravoric) — Fatto da me, per me.
