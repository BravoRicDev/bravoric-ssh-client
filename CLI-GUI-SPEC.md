# Spec flag CLI per GUI — bravoric-ssh-client

Obiettivo: la GUI usa la CLI invece di MCP. Ogni flag emette **JSON nudo** su
stdout (nessun envelope `{"ok": ...}`), in **forma identica al tool MCP**
corrispondente. Su errore: messaggio su **stderr** + **exit code != 0**.
Argomenti posizionali come indicato; le opzioni sono flag `--nome valore`.
I flag sconosciuti escono con errore (stderr + exit 2) invece di avviare la TUI.

Implementati in `bravoric_ssh_client/app.py` (dispatch JSON in `main()`, prima
dell'avvio della TUI). Riusano le funzioni di `ssh/adapter.py`,
`ssh/tunnels.py`, `ssh/audit.py`, `ssh/inspection.py`, `ssh/file_ops.py`,
`snippets.py`, `rotation.py`, `history.py`, `config.py`. La TUI non è toccata.

## Host / diagnostica
- `--list-hosts` → array di `{alias, host, user, port, auth, group, jump_host, local}`
- `--host-info <alias>` → `{alias, host, user, port, auth, group, jump_host, local, tunnels:[...]}`
- `--tmux-present <alias>` → `{alias, present}`
- `--hosts-summary [--timeout N]` → array `{alias, reachable, detail, tmux, sessions, session_count}`
- `--status` → `{version, config_path, provider, keyring_available, plain_file, hosts, tmux_local, logs_dir, audit_log, snippets_file, tunnels_file, restart_after_ssh}`
- `--host-health <alias> [--timeout N]` → salute host (load, RAM, disco, docker)
- `--host-network-ports <alias> [--timeout N]` → porte in ascolto
- `--host-top-processes <alias> [--limit N] [--sort-by cpu|mem]` → top processi

## Sessioni tmux
- `--list-sessions <alias>` → array di nomi sessione
- `--session-details <alias> <session>` → `{session, details}`
- `--create-session <alias> [--name N] [--command C]` → `{alias, session, created}`
- `--rename-session <alias> <old> <new>` → `{alias, old, new}`
- `--kill-session <alias> <session>` → `{alias, session, killed}`
- `--detach-clients <alias> <session>` → `{alias, session, detached}`
- `--kill-server <alias>` → `{alias, killed}`
- `--session-history [--limit N]` → array `{host, session}`

## Pane / finestre
- `--pane-info <alias> <session>` → `{pane_id, command, cwd, pid, title, width, height, is_shell}`
- `--pane-command <alias> <session>` → `{alias, session, command}`
- `--pane-diff <alias> <session> [--max-lines N]` → diff incrementale della pane
- `--capture-pane <alias> <session> [--lines N]` → `{alias, session, content}`
- `--copy-buffer <alias> <session>` → `{alias, session, text}`
- `--list-windows <alias> <session>` → `{alias, session, count, windows:[{index,name,active,pane_count,layout}]}`
- `--new-window <alias> <session> [--name N]` → `{alias, session, name, created}`
- `--select-window <alias> <session> <index>` → `{alias, session, index}`
- `--rename-window <alias> <session> <wid> <new>` → `{alias, session, window, name}`
- `--kill-window <alias> <session> <wid>` → `{alias, session, window, killed}`
- `--send-text <alias> <session> <text> [--enter]` → `{alias, session, sent}`
- `--send-raw <alias> <session> <keys>` → `{alias, session, sent}`
- `--send-file <alias> <session> <path> [--bracketed]` → `{alias, session, sent}`

## Snippet / broadcast
- `--snippet-list` → array `{name, command, description}`
- `--snippet-add <name> <command> [--description D]` → `{name, added}`
- `--snippet-remove <name>` → `{name, removed}`
- `--snippet-run <name> <alias...>` → array `{alias, ok, exit_code, stdout, stderr, error}`
- `--broadcast <command> <alias...> [--mode tmux|direct]` → array `{alias, ok, exit_code, stdout, stderr, error}`
- `--broadcast-wait <command> <alias...> [--timeout N]` → array `{alias, ok, error, output}`

## Tunnel
- `--tunnel-list [<alias>]` → array `{name, kind, local_port, remote_host, remote_port, bind, active}` (o `[{alias, tunnels:[...]}]` senza alias)
- `--tunnel-start <alias> <kind> <local_port> [--remote-host H] [--remote-port N] [--name N] [--bind B]` → tunnel JSON
- `--tunnel-stop <alias> <local_port>` → `{alias, local_port, stopped}`
- `--stop-tunnels <alias>` → `{alias, stopped}`
- `--tunnel-health <alias>` → `{alias, configured, active, tunnels:[...]}`

## Rotazioni
- `--rotation-list` → array `{name, entries:[{host, session}]}`
- `--rotation-add <name> <host:session...>` → `{name, entries, saved}`
- `--rotation-remove <name>` → `{name, removed}`

## File
- `--read-file <alias> <path> [--offset N] [--limit N]` → contenuto mirato
- `--write-file <alias> <path> <content> [--mode overwrite|append]` → esito scrittura
- `--edit-file <alias> <path> --pattern P --replacement R` → esito sostituzione regex
- `--replace-block <alias> <path> --old-text OLD --new-text NEW` → esito sostituzione blocco
- `--project-tree <alias> [--path P] [--max-depth N]` → albero directory
- `--search-files <alias> <query> [--path P]` → file trovati
- `--git-status <alias> [--path P]` → stato repo git

## SFTP / trasferimento
- `--sftp-list <alias> [<path>]`
- `--sftp-download <alias> <remote> <local>`
- `--sftp-upload <alias> <local> <remote>`
- `--sftp-get <alias> <remote> <local> [--recursive]`
- `--sftp-put <alias> <local> <remote> [--recursive]`
- `--sftp-mkdir <alias> <path>`
- `--sftp-rm <alias> <path>`
- `--sftp-rename <alias> <old> <new>`
- `--sftp-batch <alias> <cmd...>`
- `--transfer-file <src> <src_remote> <dst> <dst_remote>`
- `--transfer-file-direct <src> <src_path> <dst> <dst_path>`

## Comandi
- `--run-command <alias> <command> [--timeout N]` → `{alias, ok, exit_code, stdout, stderr}`
- `--run-command-all <command> [--timeout N]` → array risultati
- `--run-command-many <alias1,alias2> <command> [--timeout N]` → array risultati
- `--run-and-wait <alias> <command> [--timeout N]` → `{alias, ok, error, output}`

## Audit / log
- `--audit-list` → array `{filename, path, size, mtime}`
- `--audit-list-remote <alias>` → `{alias, files}`
- `--read-audit-log <filename> [--max-lines N]` → `{filename, content}`
- `--read-remote-audit-log <alias> <filename> [--max-lines N]` → `{alias, filename, content}`
- `--session-audit-log <alias> <session> [--max-lines N]` → log di sessione
- `--find-in-sessions <alias> --pattern P` → array `{session, matches, total}`

## Sistema
- `--packages <alias> <action> <packages_csv>` → install/update/upgrade
- `--list-services <alias>` → `{alias, services}`
- `--service <alias> <name> [--action status|start|stop|restart] [--manager systemd|docker]`
- `--service-logs <alias> <service> [--lines N] [--level L] [--grep G]`
- `--sql <alias> <query> [--engine sqlite|psql|mysql] [--db D] [--user U] [--password P] [--host-addr H]`

## Agenti
- `--launch-agent <agent> <path> [--title T] [--extra-args A] [--wait-timeout N] [--force] [--prompt P] [--alias A]`

## Diagnostica TUI (invariati)
- `--ping <alias>` → testo + exit code
- `--attach <alias> <session>` / `--attach-ro` / `--shell <alias>` / `--new <alias> [name]`
- `--sftp <hostA> <hostB>` (Midnight Commander)

## Note
- Output: JSON nudo, **identico al tool MCP** corrispondente.
- Su errore: messaggio su stderr + exit code != 0 (nessun JSON di errore).
- Flag sconosciuto: `Opzione sconosciuta: <flag>` su stderr + exit 2 (non avvia la TUI).
- I flag che lanciano operazioni bloccanti (attach/shell) restano in `terminal.py`
  lato GUI; la CLI non li espone in modalità JSON.
