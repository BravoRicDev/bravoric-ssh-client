---
name: bravoric-ssh
description: "Use bravoric-ssh MCP server (tools bravoric-ssh_*) to control SSH hosts and tmux sessions managed by the user's bravoric-ssh-client: list hosts, create/drive detached tmux sessions (send-keys/capture-pane), run batch commands in parallel, broadcast snippets, manage SSH tunnels and read audit logs. Use when the user asks to check, control, or drive their servers/tmux sessions through the MCP server."
---

# bravoric-ssh MCP — Guida per gli agenti

Il server MCP `bravoric-ssh` (stdio) espone i server SSH e le sessioni tmux
dell'utente. Guida l'esecuzione creando sessioni **detached** e leggendone
l'output: l'**attach interattivo resta all'utente** (dalla TUI o dal terminale).

## Inventario dei tool

- **Host**: `list_hosts`, `get_host`, `get_status`, `ping`, `ping_all`,
  `hosts_summary` (dashboard: raggiungibilità, tmux, #sessioni per host),
  `tmux_present`
- **Panoramica**: `list_sessions_all` (sessioni di tutti gli host),
  `find_in_sessions` (regex nelle pane delle sessioni di un host),
  `run_command_all` (comando su tutti gli host), `session_history`
- **Sessioni tmux**: `list_sessions`, `create_session` (detached, comando opzionale),
  `session_details`, `rename_session`, `kill_session`, `kill_server`, `detach_clients`,
  `list_windows`, `new_window`, `rename_window`, `kill_window`, `capture_pane`,
  `send_keys`, `send_enter`, `send_raw`, `pane_command` (processo in primo piano),
  `close_foreground` (chiude la TUI/processo attivo nella pane)
- **Esecuzione con attesa**: `run_and_wait` (comando in tmux detached, attende il
  completamento e ritorna l'output), `broadcast_wait` (idem su più host)
- **Comandi**: `run_command` (batch su un host), `run_command_many` (parallelo)
- **Trasferimento file (scp/SFTP)**: `sftp_download` (host→locale),
  `sftp_upload` (locale→host), `transfer_file` (host→host via temp locale,
  riporta `bytes` e `md5`; usalo quando i due host non si raggiungono)
- **Ispezione & file ad alta efficienza (risparmio token)**:
  - `search_files(alias, path, pattern, mode="compact"|"metadata"|"grep", text=..., max_results=50)`: ricerca file nativa remota (3 modalità in un unico tool).
  - `read_file(alias, path, offset=1, limit=100, unit="lines"|"bytes")`: lettura parziale strutturata con offset e limit (evita dump giganti).
  - `git_status(alias, path=".")`: stato compatto di un repo git in JSON (branch, commit, numero modificati/untracked, clean flag).
  - `host_health(alias)`: panoramica sintetica istantanea di risorse (CPU load avg, RAM usata/libera, spazio disco / e ~, container Docker attivi).
- **Snippet/broadcast**: `list_snippets`, `add_snippet`, `remove_snippet`,
  `broadcast` (`mode=tmux|direct`)
- **Tunnel**: `list_tunnels`, `list_tunnels_all`, `start_tunnel`, `stop_tunnel`,
  `stop_tunnels`, `tunnel_health` (verifica porte in ascolto)
- **Rotazioni**: `list_rotations`, `add_rotation` (voci `host/sessione`),
  `remove_rotation`
- **Audit**: `list_audit_logs`, `read_audit_log` (locali);
  `list_remote_audit_logs`, `read_remote_audit_log` (sui server)

## Pattern d'uso standard

1. **Scopri il contesto**: `get_status` (provider credenziali, tmux locale,
   config) poi `list_hosts` per gli alias.
2. **Verifica la raggiungibilità** prima di lavorarci: `ping <alias>` (o `ping_all`,
   o `hosts_summary` per una dashboard completa con anche tmux e #sessioni).
   Host non raggiungibili: report secco, non rilanciare all'infinito.
3. **Per un comando one-shot**: `run_command` o `run_command_many` (parallelo).
4. **Per lavoro che deve restare vivo / essere monitorato**: crea una sessione
   **detached**:
   - `create_session(alias, name=..., command=...)` la crea e avvia il comando;
   - in alternativa creala vuota, poi `send_keys` + `send_enter`;
   - leggi il progresso con `capture_pane(alias, session, lines=N)`;
   - a fine lavoro `kill_session` (non lasciare sessioni inutili, salvo richiesta).
5. **Quando vuoi il risultato completo di un comando lungo**: `run_and_wait`
   (un solo host) o `broadcast_wait` (più host) — creano una sessione detached,
   eseguono il comando con l'output rediretto su file e restituiscono l'output
   pulito a completamento. Ideali per script/aggiornamenti.
6. **Broadcast parallelo**: `broadcast(aliases=[...], command=..., mode="tmux")`
   crea una sessione `bcast-<snippet>-<host>-<ts>` per host; in `mode="direct"`
   restituisce subito stdout/exit code. Per controllare i risultati tmux, polla
   `capture_pane` sulla sessione indicata nel risultato.
7. **Chiudere un processo/TUI in una sessione** (es. opencode, vim, un comando lungo):
   prima `pane_command(alias, session)` per sapere *cosa* gira — se è solo una shell
   non c'è nulla da chiudere; poi `close_foreground(alias, session, method="auto")`,
   che invia una sequenza di chiusura (default: escalation `C-c` → `C-c` → `C-d`) e
   ricontrolla dopo ogni passo. Metodi: `auto | sigint | sigint2 | eof | exit`.
   Usalo invece di indovinare i tasti con `send_raw`.
7. **Cerchi qualcosa in una sessione?**: `find_in_sessions(alias, pattern)` cerca
   una regex nelle pane di tutte le sessioni di un host.
8. **Attach dell'utente**: informa l'utente del nome sessione così può
   agganciarsi dalla TUI (es. `SessionScreen`) o con `tmux attach -t <nome>`.

## Naming e convenzioni

- Usa gli **alias host** dalla config (es. `web-prod`, `db-host`).
- Nomi sessione broadcast: `bcast-<snippet>-<host>-<YYYYMMDD-HHMMSS>`.
- Preferisci nomi sessione brevi e descrittivi e ripulisci le sessioni che non
  servono più con `kill_session`.

## Errori tipici

- **Permission denied / Authentication failed**: chiave o password mancante o
  errata; non ripetere all'infinito, segnala l'host e passa oltre.
- **`No route to host` / `Connection timed out`**: host spento o
  irraggiungibile: annota e passa oltre.
- **`errore: host 'X' non trovato`**: alias sbagliato, ricontrolla con `list_hosts`.

## Sicurezza

- Non esporre mai password o chiavi (cambiali/omettile nei risultati).
- `read_audit_log`/`read_remote_audit_log` accettano solo nomi file `.log.gz`
  sicuri: se serve altro, chiedi all'utente.