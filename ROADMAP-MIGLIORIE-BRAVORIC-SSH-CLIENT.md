# ROADMAP MIGLIORIE: bravoric-ssh-client & Bridge Pi Agent

Questo documento definisce l'architettura tecnica, le specifiche dei moduli e lo pseudocodice per le 5 funzionalità avanzate da integrare in `bravoric-ssh-client` (layer Adapter + MCP Server) e nella rispettiva estensione per `pi-coding-agent`.

---

## Indice Funzionalità

1. **FEATURE 1: `pane_info` Esteso (Processo, CWD, PID, Titolo, Geometria)**
2. **FEATURE 2: Invia Selezione / File / Buffer a Tmux con Bracketed Paste**
3. **FEATURE 3: Snapshot & Diff Output Pane (Incremental Polling a Basso Consumo Token)**
4. **FEATURE 4: Navigazione & Gestione Multi-Window nella Stessa Sessione**
5. **FEATURE 5: `restart_foreground` (Chiusura Pulita + Riavvio Deterministico)**

---

## FEATURE 1: `pane_info` Esteso

### Obiettivo
Espandere l'ispezione della pane attiva per ottenere in un'unica chiamata atomica:
- Nome del processo in primo piano (`pane_current_command`)
- Directory di lavoro corrente (`pane_current_path`)
- PID del processo (`pane_pid`)
- Titolo della finestra/pane (`pane_title`)
- Dimensioni attuali (`pane_width`, `pane_height`)
- Flag `is_shell` (booleano per sapere se la sessione è idle)

### 1.1 Pseudocodice Adapter (`ssh/adapter.py`)

```python
FUNCTION tmux_pane_info(host: Host, session: str, cfg: SshConfig) -> PaneInfoResult:
    # Formattatore con delimitatore univoco non presente nei path
    DELIM = "|||__BRAVORIC_DELIM__|||"
    FORMAT_STRING = (
        "#{pane_current_command}" + DELIM +
        "#{pane_current_path}" + DELIM +
        "#{pane_pid}" + DELIM +
        "#{pane_title}" + DELIM +
        "#{pane_width}" + DELIM +
        "#{pane_height}"
    )

    COMMAND = "tmux display-message -p -t " + sh_quote(session) + " '" + FORMAT_STRING + "'"
    RES = run_tmux_action(host, COMMAND, cfg)

    IF NOT RES.ok:
        RETURN PaneInfoResult(ok=False, error=RES.stderr)

    PARTS = RES.stdout.strip().split(DELIM)
    IF LEN(PARTS) < 6:
        RETURN PaneInfoResult(ok=False, error="Formato output tmux non valido")

    CMD = clean_cmd(PARTS[0])
    IS_SHELL = CMD IN SHELL_COMMANDS_SET

    RETURN PaneInfoResult(
        ok=True,
        command=CMD,
        cwd=PARTS[1],
        pid=INT(PARTS[2]),
        title=PARTS[3],
        width=INT(PARTS[4]),
        height=INT(PARTS[5]),
        is_shell=IS_SHELL
    )
END FUNCTION
```

### 1.2 Pseudocodice MCP (`mcp_server.py`)

```python
@mcp.tool(name="pane_info", description="Restituisce processo, CWD, PID e dimensioni della pane attiva.")
FUNCTION pane_info(alias: str, session: str) -> str:
    HOST = get_host(alias)
    INFO = adapter.tmux_pane_info(HOST, session, ssh_cfg())
    IF NOT INFO.ok:
        RETURN json_dump({"ok": False, "error": INFO.error})
    
    RETURN json_dump({
        "ok": True,
        "host": alias,
        "session": session,
        "command": INFO.command,
        "cwd": INFO.cwd,
        "pid": INFO.pid,
        "title": INFO.title,
        "size": f"{INFO.width}x{INFO.height}",
        "is_shell": INFO.is_shell
    })
END FUNCTION
```

---

## FEATURE 2: Invia Selezione / File con Bracketed Paste

### Obiettivo
Inviare blocchi multiriga di testo, file interi o snippet senza incorrere nei problemi tipici di `send-keys` (auto-indent errato della shell, auto-completamenti che scattano a metà riga, caratteri speciali interpretati).

### 2.1 Pseudocodice Adapter (`ssh/adapter.py`)

```python
FUNCTION tmux_paste_buffer(host: Host, session: str, content: str, bracketed: bool, cfg: SshConfig) -> TmuxActionResult:
    # 1. Carica il testo in un buffer temporaneo dedicato di tmux
    BUFFER_NAME = "bravoric_transfer_" + timestamp()
    
    IF host.is_local():
        # Scrittura su buffer locale via pipe stdin a tmux load-buffer
        EXECUTE "tmux load-buffer -b " + BUFFER_NAME + " -" CON STDIN = content
    ELSE:
        # Host remoto: invio via SSH sicuro o file temp remoto
        REMOTE_TMP = "/tmp/" + BUFFER_NAME + ".txt"
        UPLOAD_TEXT(host, content, REMOTE_TMP)
        run_tmux_action(host, "tmux load-buffer -b " + BUFFER_NAME + " " + REMOTE_TMP, cfg)
        run_remote_action(host, "rm -f " + REMOTE_TMP)
    END IF

    # 2. Incolla con flag -p (bracketed paste mode) se supportato dalla TUI ricevente
    PASTE_CMD = "tmux paste-buffer " + ("-p " IF bracketed ELSE "") + "-b " + BUFFER_NAME + " -t " + sh_quote(session)
    RES = run_tmux_action(host, PASTE_CMD, cfg)

    # 3. Pulizia buffer tmux
    run_tmux_action(host, "tmux delete-buffer -b " + BUFFER_NAME, cfg)

    RETURN RES
END FUNCTION
```

### 2.2 Pseudocodice Estensione Pi Agent (`bravoric-remote.ts`)

```typescript
FUNCTION registerPasteCommand(pi: ExtensionAPI):
    pi.registerCommand("ssh-paste", {
        description: "Incolla il file corrente o testo selezionato nella sessione tmux attiva",
        handler: async (args, ctx) => {
            SESSION = state.selectedSession;
            HOST = state.selectedHost;

            TEXT_TO_SEND = "";
            IF args.startsWith("@file:"):
                FILEPATH = args.replace("@file:", "").trim();
                TEXT_TO_SEND = readFile(FILEPATH);
            ELSE:
                TEXT_TO_SEND = args || getActiveEditorSelection();
            END IF;

            CONFIRM = await ctx.ui.confirm(
                "Inviare a Tmux",
                "Inviare " + TEXT_TO_SEND.length + " caratteri a " + HOST + ":" + SESSION + "?"
            );
            IF NOT CONFIRM: RETURN;

            RES = await runBridge("paste", [HOST, SESSION, TEXT_TO_SEND, "--bracketed"]);
            IF RES.ok:
                ctx.ui.notify("Testo incollato con successo!", "info");
            ELSE:
                ctx.ui.notify("Errore: " + RES.error, "error");
            END IF;
        }
    });
END FUNCTION
```

---

## FEATURE 3: Snapshot & Diff Output Pane (Token Saver)

### Obiettivo
Permettere all'agente di monitorare comandi a lungo termine o TUI (OpenCode, build, server log) ricevendo **solo le nuove righe aggiunte**, con calcolo di hashing e diff incrementale per evitare il ricarico continuo di centinaia di token.

### 3.1 Pseudocodice MCP (`mcp_server.py`)

```python
CLASS PaneDiffTracker:
    # Memoria cache: (alias, session) -> hash delle ultime righe e timestamp
    CACHE = {}

    FUNCTION capture_diff(host: Host, session: str, max_lines: int, cfg: SshConfig) -> dict:
        # Cattura le ultime N righe grezze
        RAW_RES = adapter.tmux_capture_pane(host, session, cfg, lines=max_lines, escape=False)
        IF NOT RAW_RES.ok:
            RETURN {"ok": False, "error": RAW_RES.stderr}

        CURRENT_LINES = RAW_RES.stdout.splitlines()
        KEY = (host.alias, session)

        IF KEY NOT IN CACHE:
            # Primo campionamento: baseline iniziale
            CACHE[KEY] = CURRENT_LINES
            RETURN {
                "ok": True,
                "is_first_sample": True,
                "total_lines": LEN(CURRENT_LINES),
                "new_lines": CURRENT_LINES[-15:], # Preview prime 15
                "diff_count": 0
            }
        END IF

        OLD_LINES = CACHE[KEY]
        CACHE[KEY] = CURRENT_LINES

        # Calcolo diff incrementale a scorrimento
        NEW_DELTA = compute_sliding_window_delta(OLD_LINES, CURRENT_LINES)

        RETURN {
            "ok": True,
            "is_first_sample": False,
            "has_changes": LEN(NEW_DELTA) > 0,
            "diff_count": LEN(NEW_DELTA),
            "new_content": "\n".join(NEW_DELTA)
        }
    END FUNCTION
END CLASS
```

---

## FEATURE 4: Navigazione & Gestione Multi-Window

### Obiettivo
Permettere di gestire sessioni tmux strutturate (es. sessione `Pi` con Window 0: `opencode`, Window 1: `npm run dev`, Window 2: `bash`) direttamente dal bridge senza doversi limitare a una finestra per sessione.

### 4.1 Struttura Modello Dati

```python
STRUCTURE TmuxWindowInfo:
    index: int          # Es. 0, 1, 2
    name: str           # Es. "opencode", "shell"
    active: bool        # Se è la finestra attualmente visibile
    layout: str         # Geometria / split
    pane_count: int     # Numero di pane nella finestra
END STRUCTURE
```

### 4.2 Pseudocodice Adapter & MCP (`ssh/adapter.py`, `mcp_server.py`)

```python
FUNCTION tmux_list_windows_parsed(host: Host, session: str, cfg: SshConfig) -> list[TmuxWindowInfo]:
    FORMAT = "#{window_index}|||#{window_name}|||#{window_active}|||#{window_panes}"
    RES = run_tmux_action(host, f"tmux list-windows -t {sh_quote(session)} -F '{FORMAT}'", cfg)
    
    WINDOWS = []
    FOR LINE IN RES.stdout.strip().splitlines():
        idx, name, active_flag, panes = LINE.split("|||")
        WINDOWS.append(TmuxWindowInfo(
            index=INT(idx),
            name=name,
            active=(active_flag == "1"),
            pane_count=INT(panes)
        ))
    RETURN WINDOWS
END FUNCTION

FUNCTION tmux_select_window(host: Host, session: str, window_index: int, cfg: SshConfig) -> bool:
    TARGET = f"{session}:{window_index}"
    RES = run_tmux_action(host, f"tmux select-window -t {sh_quote(TARGET)}", cfg)
    RETURN RES.ok
END FUNCTION
```

### 4.3 Integrazione Menu Pi (`bravoric-remote.ts`)
Nel menu `Ctrl+H`:
- Sotto-menu `🪟 Finestre della Sessione`:
  - `● 0: opencode (attiva)`
  - `○ 1: dev server (in esecuzione)`
  - `➕ Nuova Finestra nella Sessione`

---

## FEATURE 5: `restart_foreground` (Chiusura + Riavvio Deterministico)

### Obiettivo
Unire `close_foreground` e il rilancio del processo in una sola operazione sicura per ripristinare processi incastrati (es. un agent crashato, un server bloccato).

### 5.1 Pseudocodice Logica (`mcp_server.py`)

```python
FUNCTION restart_foreground(alias: str, session: str, fallback_command: str = "") -> dict:
    HOST = get_host(alias)
    CFG = ssh_cfg()

    # 1. Rileva il processo attuale prima della chiusura
    PANE_INFO = adapter.tmux_pane_info(HOST, session, CFG)
    IF NOT PANE_INFO.ok:
        RETURN {"ok": False, "error": "Impossibile analizzare la pane"}

    PREVIOUS_CMD = PANE_INFO.command
    COMMAND_TO_RELAUNCH = fallback_command OR PREVIOUS_CMD

    IF PANE_INFO.is_shell:
        # Se era già shell, lancia direttamente il comando specificato
        IF NOT COMMAND_TO_RELAUNCH:
            RETURN {"ok": False, "error": "Nessun processo attivo e nessun comando specificato per il riavvio"}
        adapter.tmux_send_keys(HOST, session, COMMAND_TO_RELAUNCH, CFG)
        adapter.tmux_send_enter(HOST, session, CFG)
        RETURN {"ok": True, "restarted": COMMAND_TO_RELAUNCH, "method": "direct_launch"}
    END IF

    # 2. Chiudi con la funzione verificata
    CLOSE_RES = close_foreground(alias, session, method="auto")
    
    # 3. Attesa attiva di rilascio prompt shell (max 5 secondi)
    DEADLINE = now() + 5.0
    PROMPT_READY = False
    WHILE now() < DEADLINE:
        CURRENT = adapter.tmux_pane_info(HOST, session, CFG)
        IF CURRENT.ok AND CURRENT.is_shell:
            PROMPT_READY = True
            BREAK
        sleep(0.5)
    END WHILE

    IF NOT PROMPT_READY:
        RETURN {"ok": False, "error": "Il processo precedente non ha liberato la shell in tempo"}

    # 4. Rilancio del comando
    adapter.tmux_send_keys(HOST, session, COMMAND_TO_RELAUNCH, CFG)
    adapter.tmux_send_enter(HOST, session, CFG)

    RETURN {
        "ok": True,
        "closed": PREVIOUS_CMD,
        "restarted": COMMAND_TO_RELAUNCH,
        "status": "success"
    }
END FUNCTION
```

---

## Tabella di Priorità e Impatto

| Feature | File Coinvolti | Beneficio per Utente | Beneficio per Agente (MCP) |
|---|---|---|---|
| **1. `pane_info` esteso** | `adapter.py`, `mcp_server.py`, `bravoric-remote.ts` | Widget con CWD e info complete | Sa in che repo si trova la sessione |
| **2. Invia con Bracketed Paste** | `adapter.py`, `bravoric-bridge.py`, `bravoric-remote.ts` | Passaggio immediato di prompt e file da Pi a Tmux | Incolla file e patch senza corrompere l'indentazione |
| **3. Snapshot & Diff Output** | `mcp_server.py`, `bravoric-bridge.py` | Ispezione cronologica di cosa accade | Risparmio fino all'80% di token su polling lunghi |
| **4. Gestione Multi-Window** | `adapter.py`, `mcp_server.py`, `bravoric-remote.ts` | Navigazione tra schede senza uscire dalla sessione | Controllo granulare dei processi di supporto |
| **5. `restart_foreground`** | `mcp_server.py`, `bravoric-bridge.py`, `bravoric-remote.ts` | Riavvio TUI/server con un solo comando/shortcut | Auto-guarigione se un agent remoto si blocca |
