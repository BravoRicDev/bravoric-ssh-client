# Workorder: flag CLI per GUI bravoric-ssh-client-GUI (contratto definitivo)

Repo backend: `/home/riccardo/Progetti/bravoric-ssh-client`
Repo GUI: `/home/riccardo/Progetti/bravoric-ssh-client-GUI/gui`

## Contratto (verificato sul campo)
- Output su **stdout**: JSON valido, **forma identica al tool MCP corrispondente**
  (nessun envelope `{"ok":...}`). Esempio: `--list-hosts` emette già una lista nuda.
- Su errore: messaggio su **stderr** + **exit code != 0**. La GUI mostra l'errore.
- Argomenti posizionali come indicato; le opzioni sono flag `--nome valore`.
- Implementare in `bravoric_ssh_client/app.py` riusando le funzioni già usate da
  `mcp_server.py` (`ssh/adapter.py`, `ssh/tunnels.py`, `ssh/audit.py`,
  `snippets.py`, `config.py`). NON toccare la TUI.
- Aggiungere i test in `tests/` e far passare `python -m pytest tests/`.

## Flag da implementare (sintassi esatta attesa dalla GUI)

### Host / diagnostica
- `--host-info <alias>` → dettagli host (stesso JSON di `get_host` MCP)
- `--tmux-present <alias>` → JSON presenza tmux
- `--hosts-summary` → lista dashboard (alias, reachable, detail, sessions)
- `--status` → diagnostica server
- `--host-health <alias>` → salute host
- `--host-network-ports <alias>` → porte in ascolto
- `--host-top-processes <alias> [--limit N] [--sort-by cpu|mem]` → top processi

### Sessioni tmux
- `--list-sessions <alias>` → **esiste già**: emette lista di nomi sessione
- `--session-details <alias> <session>` → dettagli sessione
- `--create-session <alias> [--name N] [--command C]` → crea sessione
- `--rename-session <alias> <old_name> <new_name>`
- `--kill-session <alias> <session>`
- `--detach-clients <alias> <session>`
- `--kill-server <alias>`
- `--session-history [--limit N]` → cronologia sessioni

### Pane / finestre
- `--pane-info <alias> <session>`
- `--pane-command <alias> <session>`
- `--pane-diff <alias> <session> [--max-lines N]`
- `--capture-pane <alias> <session> [--lines N]`
- `--copy-buffer <alias> <session>`
- `--list-windows <alias> <session>`
- `--new-window <alias> <session> [--name N]`
- `--select-window <alias> <session> <index>`
- `--rename-window <alias> <session> <window_id> <new_name>`
- `--kill-window <alias> <session> <window_id>`
- `--send-text <alias> <session> <text> [--enter]` → **esiste già**
- `--send-raw <alias> <session> <keys>`
- `--send-file <alias> <session> <path> [--bracketed]` → **esiste già**

### Snippet / broadcast
- `--snippet-list` → elenco snippet
- `--snippet-add <name> <command> [--description D]`
- `--snippet-remove <name>`
- `--snippet-run <name> <alias...>` → **esiste già**
- `--broadcast <command> <alias...> [--mode tmux|direct]` → **esiste già**
- `--broadcast-wait <command> <alias...> [--timeout N]`

### Tunnel
- `--tunnel-list [<alias>]` → tunnel (tutti se alias omesso)
- `--tunnel-start <alias> <kind> <local_port> [--remote-host H] [--remote-port N] [--name N] [--bind B]` → **esiste già**
- `--tunnel-stop <alias> <local_port>` → **esiste già**
- `--stop-tunnels <alias>` → ferma tutti i tunnel dell'host
- `--tunnel-health <alias>`

### Rotazioni
- `--rotation-list` → **esiste già**
- `--rotation-add <name> <entry...>` → **esiste già**
- `--rotation-remove <name>` → **esiste già**

### File
- `--read-file <alias> <path> [--offset N] [--limit N]` → `{"content":..., ...}`
- `--write-file <alias> <path> <content> [--mode overwrite|append]`
- `--edit-file <alias> <path> --pattern P --replacement R`
- `--replace-block <alias> <path> --old-text OLD --new-text NEW`
- `--project-tree <alias> [--path P] [--max-depth N]`
- `--search-files <alias> <query>`
- `--git-status <alias> [--path P]`

### SFTP / trasferimento
- `--sftp-list <alias> [<path>]`
- `--sftp-download <alias> <remote> <local>`
- `--sftp-upload <alias> <local> <remote>`
- `--sftp-get <alias> <remote> <local> [--recursive]`
- `--sftp-put <alias> <local> <remote> [--recursive]`
- `--sftp-mkdir <alias> <path>`
- `--sftp-rm <alias> <path>`
- `--sftp-rename <alias> <old_path> <new_path>`
- `--sftp-batch <alias> <cmd1> <cmd2> ...`
- `--transfer-file <src_alias> <src_remote> <dst_alias> <dst_remote>`
- `--transfer-file-direct <src_alias> <src_path> <dst_alias> <dst_path>`

### Comandi
- `--run-command <alias> <command> [--timeout N]`
- `--run-command-all <command> [--timeout N]`
- `--run-command-many <alias1,alias2> <command> [--timeout N]`
- `--run-and-wait <alias> <command> [--timeout N]`

### Audit / log
- `--audit-list` → log audit locali
- `--audit-list-remote <alias>` → log audit remoti
- `--read-audit-log <filename> [--max-lines N]`
- `--read-remote-audit-log <alias> <filename> [--max-lines N]`
- `--session-audit-log <alias> <session> [--max-lines N]`
- `--find-in-sessions <alias> --pattern P`

### Sistema
- `--packages <alias> <action> <packages_csv>`
- `--list-services <alias>`
- `--service <alias> <name> [--action status|start|stop|restart] [--manager systemd|docker]`
- `--service-logs <alias> <service> [--lines N] [--level L] [--grep G]`
- `--sql <alias> <query>`

### Agenti
- `--launch-agent <agent> <path> [--title T] [--extra-args A] [--wait-timeout N] [--force] [--prompt P] [--alias A]` → **esiste già**

## Al termine
Scrivere `/home/riccardo/Progetti/bravoric-ssh-client/WORKORDER-GUI-MIGRATION.DONE`
con: elenco flag implementati, esito `pytest`, eventuali flag non implementabili.
