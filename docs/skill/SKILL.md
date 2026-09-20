---
name: bravoric-ssh
description: "Use bravoric-ssh MCP server (tools bravoric-ssh_*) to control SSH hosts and tmux sessions managed by the user's bravoric-ssh-client: list hosts, create/drive detached tmux sessions (send-keys/capture-pane/paste), run batch commands in parallel, broadcast snippets, inspect remote files with token-efficient tools (search_files, read_file, git_status, host_health), manage SSH tunnels and read audit logs. Use when the user asks to check, control, or drive their servers/tmux sessions through the MCP server."
---

# bravoric-ssh MCP — Guida Operativa Ufficiale per gli Agenti

Il server MCP `bravoric-ssh` (stdio) espone l'intera infrastruttura di server SSH e sessioni tmux gestiti da `bravoric-ssh-client`.

---

## ⚠️ REGOLE AUREE DI INTERAZIONE (ANTI-ERRORE & RISPARMIO TOKEN)

### 1. NESSUN COMANDO IN LOCALE SU FILE/SERVER REMOTI!
- Non usare comandi shell locali (`find`, `cat`, `grep`, `docker`, `tmux`) su risorse che vivono sui server remoti (es. `lumon-principale`, `mioaruba`).
- Non assumere mai che il codice o le sessioni siano sul tuo `localhost`: consulta `.pi/TOPOLOGIA-SESSIONI.md` e usa **SEMPRE i tool MCP con il parametro `alias` esplicito**.

### 2. COMUNICAZIONE TMUX: MAI `send_keys` PER TESTI LUNGHI, USA SEMPRE `paste`!
- **`paste(alias, session, content, bracketed: true, enter: true)`**: È il metodo TASSATIVO per inviare prompt, codice o messaggi multi-riga. Gestisce il bracketed paste e previene la corruzione dei caratteri.
- **`capture_pane(alias, session, lines=30)`**: Leggi lo stato del terminale prima di inviare testo.
- **Riconoscimento stato agente**:
  - Se vedi `interrupt` o `esc interrupt` in basso a sinistra (in OpenCode/Pi): **l'agente STA ELABORANDO**. NON disturbare e NON inviare tasti!
  - Solo se `interrupt` è ASSENTE e il prompt è libero, invia input.

### 3. ISPEZIONE REMOTA AD ALTA EFFICIENZA (NON USARE `cat` O `run_command ls`)
Per non saturare la finestra di contesto con dump giganteschi, usa i **4 tool di efficienza nativi**:
1. **`read_file(alias, path, offset=1, limit=100, unit="lines")`**:
   - Legge solo il frammento di file che ti serve.
   - Supporta paginazione (`offset`, `limit`) sia per righe (`unit="lines"`) che per byte (`unit="bytes"`).
2. **`search_files(alias, path, pattern, mode="compact", text=..., max_results=50)`**:
   - `mode="compact"`: restituisce l'albero compatto dei file che matchano (default).
   - `mode="metadata"`: include dimensioni, permessi e timestamp.
   - `mode="grep"`: cerca testo dentro i file remoti e restituisce le righe con numero di riga (come `rg`/`grep`).
3. **`git_status(alias, path=".")`**:
   - Restituisce un JSON compatto con: branch corrente, commit hash, commit message, conteggio modificati/untracked e flag `clean: true/false`.
4. **`host_health(alias)`**:
   - Restituisce istantaneamente: CPU load average, RAM totale/usata/libera, spazio disco su `/` e su `/home`, stato dei container Docker attivi.

---

## CATALOGO DEI TOOL DISPONIBILI

### Host & Connettività
- `list_hosts`: elenca tutti gli alias configurati (es. `localhost`, `cubotto-di-legno-ssh-vpn`, `lumon-principale`, `mioaruba`).
- `get_status`: info sul demone locale, tmux locale e configurazione.
- `ping(alias)` / `ping_all`: verifica connettività e latenza SSH.
- `hosts_summary`: dashboard completa di tutti gli host (reachability, tmux attivo, conteggio sessioni).
- `host_health(alias)`: diagnostica hardware e container del server remoto.

### File Remoti (Efficienza e Trasferimento)
- `search_files(alias, path, pattern, mode, text, max_results)`: cerca file e contenuti (grep remoto).
- `read_file(alias, path, offset, limit, unit)`: lettura selettiva a blocchi.
- `git_status(alias, path)`: stato git strutturato e leggero.
- `sftp_download(alias, remote_path, local_path)`: scarica da server a locale.
- `sftp_upload(alias, local_path, remote_path)`: carica da locale a server.
- `transfer_file(source_alias, source_path, dest_alias, dest_path)`: trasferimento server-to-server con verifica MD5 (usato quando due server non si raggiungono direttamente).

### Sessioni tmux & Controllo Agenti
- `list_sessions(alias)`: elenca le sessioni su un host.
- `list_sessions_all`: elenca tutte le sessioni di tutti gli host con un'unica chiamata.
- `create_session(alias, session, command)`: crea sessione detached con auto-massimizzazione.
- `capture_pane(alias, session, lines=30)`: legge il buffer del terminale (ultime N righe).
- `pane_diff(alias, session, max_lines=200, reset=False)`: **diff incrementale** dell'output della pane (restituisce SOLO le righe comparse dall'ultima lettura, azzerando lo spreco di token nei controlli periodici).
- `pane_info(alias, session)`: restituisce processo attivo, CWD, PID, titolo e geometria della pane.
- `paste(alias, session, content, bracketed: true, enter: true)`: incolla testo in modo sicuro con bracketed paste.
- `pane_command(alias, session)`: restituisce il comando attivo nella pane (es. `opencode`, `pi`, `node`, `bash`).
- `close_foreground(alias, session, method="auto")`: chiude in sicurezza la TUI/processo attivo (escalation ordinata `C-c` -> `C-d`).
- `restart_foreground(alias, session, fallback_command="")`: chiude e riavvia in modo pulito e deterministico il processo in primo piano (recupera automaticamente l'ultimo comando dalla history shell, o usa il fallback).
- `rename_session(alias, session, new_name)`: rinomina una sessione.
- `kill_session(alias, session)`: termina una sessione non più necessaria.
- `find_in_sessions(alias, pattern)`: cerca una regex all'interno di tutte le pane dell'host.
- `session_history(limit=50)`: cronologia delle sessioni recenti (parametro `limit` max 200).
- `list_windows_parsed(alias, session)`: finestre in formato strutturato (id, nome, attiva).
- `select_window(alias, session, window_index)`: seleziona e attiva una finestra della sessione.

### Esecuzione Comandi Batch
- `run_command(alias, command)`: esegue un comando bash sul server e torna exit code, stdout e stderr.
- `run_command_many(aliases, command)`: esegue in parallelo su più server.
- `run_and_wait(alias, command, timeout_sec)`: esegue un comando lungo in tmux detached e ne raccoglie l'output al termine.

### Tunnel SSH & Manutenzione
- `list_tunnels` / `start_tunnel` / `stop_tunnel`: apertura/chiusura tunnel TCP forward.
- `tunnel_health`: verifica porte locali in ascolto dei tunnel attivi.
- `read_audit_log` / `read_remote_audit_log`: ispezione registri operativi.

---

## PATTERN D'USO FREQUENTI

### 1. Ispezionare un file di codice o configurazione su un server remoto
```typescript
// NON lanciare run_command con "cat file.js"! Usa read_file:
await bravoric-ssh_read_file({
  alias: "lumon-principale",
  path: "/home/serverino/Serverino/crm-v2/src/app.js",
  offset: 1,
  limit: 80,
  unit: "lines"
});
```

### 2. Cercare dove è definita una funzione o variabile su un server
```typescript
await bravoric-ssh_search_files({
  alias: "lumon-principale",
  path: "/home/serverino/Serverino/crm-v2/src",
  pattern: "*.js",
  mode: "grep",
  text: "salvaChiamata"
});
```

### 3. Inviare un comando o prompt a una sessione agente
```typescript
// 1. Ispeziona prima lo stato per accertarti che sia idle (interrupt assente)
const pane = await bravoric-ssh_capture_pane({
  alias: "lumon-principale",
  session: "crm-v2-lumonboy - DEVELOPER-1",
  lines: 25
});

// 2. Invia il prompt con paste bracketed
await bravoric-ssh_paste({
  alias: "lumon-principale",
  session: "crm-v2-lumonboy - DEVELOPER-1",
  content: "Procedi con il montaggio del codice SPEC-010.",
  bracketed: true,
  enter: true
});
```
