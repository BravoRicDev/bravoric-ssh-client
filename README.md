# bravoric-ssh-client

A full-screen **SSH + tmux** TUI client.

I wrote this for myself. It is a **personal tool**, not a product: I'm publishing
it on GitHub mainly to show *how I reason* and how I build the tools I use every
day. It lists your hosts, shows the tmux sessions running on each of them, and
lets you attach, create, rename and kill sessions without leaving the terminal.
All network operations run in background threads, so the UI always stays
responsive. No external services, nothing to install on your servers.

[![CI](https://github.com/BravoRicDev/bravoric-ssh-client/actions/workflows/ci.yml/badge.svg)](https://github.com/BravoRicDev/bravoric-ssh-client/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

> Italiano? Vedi [README.it.md](README.it.md).

## About this project

> **This is a personal tool.** I built it to scratch my own itch and I use it
> every day. I'm sharing it as-is, for reference and curiosity, to show the way I
> think and how I assemble my own tooling. It is not a supported product: no
> roadmap, no SLA, no promise of feature parity. Issues and suggestions are
> welcome, but I only merge changes that fit how I use it.

<!-- TODO: add a screenshot or a short GIF of the TUI here, e.g. docs/assets/demo.gif -->

## Features

- **Host list → tmux sessions**, keyboard driven, with live text filter.
- **Host management**: add, edit, delete, duplicate, group, per-host credentials.
- **Connectivity**: TCP ping on the SSH port, mass ping of all visible hosts.
- **Session management**: attach, read-only attach (`tmux attach -r`), create,
  rename, kill, kill whole server, details, windows, new window, detach clients.
- **Credential providers**: system keyring (GNOME/KDE/macOS/Windows) or a local
  `plain` file with `0600` permissions; per-host override (`keyring`, `plain`,
  `key`, `prompt`).
- **Import**: from `~/.ssh/config` and from Remmina profiles, plus a helper to
  copy passwords from Remmina into the app keyring.
- **File exchange**: `scp` copy and a two-panel **Midnight Commander** session
  (local ↔ remote or remote ↔ remote) with automatic password handling.
- **SSH tunnels**: `-L` / `-R` / `-D` port forwards, started in the background
  and shared between the TUI and the MCP server.
- **Jump host / bastion** (`-J` / ProxyJump) with a multi-host askpass helper.
- **Broadcast snippets**: run a command on many hosts at once, either in a new
  detached tmux session per host or directly over SSH (parallel).
- **Rotation profiles**: auto-cycle through the panes of several sessions, with
  an optional interactive mode to send keys to the focused session.
- **Audit trail**: record interactive sessions to gzip logs (tmux `pipe-pane` or
  `script`).
- **Local tmux**: add an host with `host = "localhost"` to manage the tmux on
  the local machine without SSH.
- **MCP server**: the whole client is exposed as a Model Context Protocol server
  on stdio, so an agent (e.g. opencode) can drive hosts, sessions, commands,
  broadcasts, tunnels and audit logs.

## Requirements

- Python **3.11+**
- OpenSSH `ssh` / `scp` client
- `tmux` on the servers you manage (and locally for local hosts)
- Optional: `mc` (Midnight Commander) for the file-exchange view
- Optional: `script` (util-linux) to record plain SSH shells
- Optional: `ptyxis` / `gnome-terminal` to open sessions in separate windows

## Installation

```bash
git clone https://github.com/BravoRicDev/bravoric-ssh-client.git
cd bravoric-ssh-client
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # omit [dev] for a runtime-only install
```

Run it:

```bash
.venv/bin/bravoric-ssh
```

or, if installed on your `PATH`:

```bash
bravoric-ssh
```

## Configuration

Config file: `~/.config/bravoric-ssh-client/config.toml`
(override the path with the `BRAVORIC_CONFIG` environment variable).

```toml
[credentials]
provider = "keyring"   # or "plain" (local file, permissions 0600)

[[hosts]]
alias = "web-prod"
host  = "server.example.com"
user  = "deploy"
# group = "clients"          # group hosts together (g key in the list)
# jump_host = "bastion"      # alias of the bastion for ProxyJump (-J)
# auth = ""                  # default: use the global provider
# auth = "key"               # SSH key / agent (no password)
# auth = "prompt"            # ask every time
```

General options (`[general]`):

```toml
[general]
restart_after_ssh = false  # reopen the TUI after an ssh session ends
history_size = 10          # how many recent sessions to keep
audit_log = false          # record sessions to compressed logs (pipe-pane/script)
# snippets_file = "..."    # snippet catalog path (default: <config_dir>/snippets.json)
# tunnels_file = "..."     # tunnel state path (default: <config_dir>/tunnels.json)
```

### Import from your ssh config

```bash
.venv/bin/python scripts/import_ssh_config.py            # -> ~/.config/.../config.toml
.venv/bin/python scripts/import_ssh_config.py --merge    # add only new hosts
```

### Import from Remmina

Press `i` on the host screen to import both `~/.ssh/config` and Remmina SSH
profiles (flatpak and legacy) without duplicates. For a new Remmina host, set
the password the first time with `p`.

### Import credentials from Remmina / SSH Pilot

```bash
.venv/bin/python scripts/import_keyring_credentials.py   # copy passwords into the app keyring
```

## Using the TUI

- **Host screen**: arrows + `Enter` to open a host. `q` quits.
  - `a` add a host · `e` edit · `d` delete (with confirmation) · `D` duplicate
  - `p` set/remove the host password in the provider
  - `t` connectivity test (TCP ping on the SSH port) · `T` ping all visible hosts
  - `c` copy a file (scp) · `F` file exchange via Midnight Commander
  - `u` SSH tunnels for the selected host (forward `-L`/`-R`/`-D`, live status)
  - `B` broadcast a snippet to multiple hosts (default: a new tmux session per host)
  - `i` import hosts from `~/.ssh/config` and Remmina (merge)
  - `g` cycle groups · `h` recent sessions · `R` saved rotations · `o` observe a host
  - `r` reload config from disk · text field on top = live filter
  - a `🔑` next to a host means a password is stored
- **Session screen**:
  - `Enter` attach · `R` read-only attach (`tmux attach -r`)
  - `n` new session · `r` rename · `k` kill (with confirmation) · `K` kill all
  - `d` session details · `w` list/manage windows (rename `r`, close `k`)
  - `D` detach other clients · `W` new window · `g` refresh
  - `s` open a plain SSH shell · `Esc` back to hosts

**Window title**: when you attach to (or create) a session, the terminal window
title becomes `HOST - SESSION`. The client sets the remote tmux title string
during the attach and restores it afterwards, so it works even with
`set-titles on` on the server.

**Automatic restart**: with `restart_after_ssh = true` in `[general]`, the TUI
reopens automatically after you exit an ssh session.

### Rotation profiles

If several agents/processes work in distinct tmux sessions (possibly across
servers), create a **rotation profile**: the TUI shows each session's content
(`capture-pane`) and, after `cycle_interval` seconds of inactivity, moves to the
next one automatically. Any input stops the rotation on the current session.

- `R` on the host screen → saved rotations (`Enter` to observe, `n` to create,
  `a` to reopen all sessions of the profile in separate windows).
- Host config: `auto_cycle = true` and `cycle_interval = 120` to enable automatic
  selection; profiles are saved in `rotations.json`.

### Local tmux

Add a host with `host = "localhost"` (or `127.0.0.1`, or set `local = true`):
the TUI manages the local tmux directly without SSH. A `localhost` host is
already included in the default config.

Every host change is written to `config.toml` with an automatic backup
(`config.toml.bak.<timestamp>`).

### SSH tunnels (`u`)

From the selected host press `u`: the list of configured tunnels with their
status (🟢 active / 🔴 off). `n` adds one (kind `L`/`R`/`D`, port, destination),
`s`/`x` start/stop a single tunnel, `a`/`X` start/stop all, `d` removes one.
Tunnels run as background `ssh -N -T` processes (password from the keyring is
injected automatically) and their definitions are saved in the config:

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

### Jump host / bastion (`jump_host`)

A host that is not directly reachable can be reached through a bastion: set the
bastion alias in the config and the client injects `-J` (ProxyJump) into the
connections, using the multi-host `SSH_ASKPASS` helper for both passwords
(bastion and final host):

```toml
[[hosts]]
alias = "internal-app"
host  = "10.0.0.9"
user  = "deploy"
jump_host = "bastion"
```

### Broadcasting snippets (`B`)

Press `B` on the host screen: snippet catalog (`snippets.json` in the config
dir, `n` to add one). Pick a snippet, mark hosts with `Space` (or `a` for all)
and press `Enter`. Two modes (`t` toggles, **default: tmux**):

- **tmux session** (default): a detached tmux session named
  `bcast-<snippet>-<host>-<YYYYMMDD-HHMMSS>` is created on each host and runs the
  command. If tmux is not installed on a host, it falls back to direct mode.
- **Direct (ssh batch)**: the script runs in parallel (thread pool) and the
  results (stdout/exit code) appear immediately in the grid.

### MCP server (`bravoric-ssh-mcp`)

The whole client is exposed as an **MCP server** on stdio: an agent (e.g.
opencode) can control hosts, tmux sessions, commands, broadcasts, tunnels and
audit logs exactly like the TUI.

```bash
bravoric-ssh-mcp            # start the server (stdio)
```

Exposed tools (47), grouped:

- **Host**: `list_hosts`, `get_host`, `get_status`, `ping`, `ping_all`,
  `hosts_summary`, `tmux_present`
- **Overview**: `list_sessions_all`, `find_in_sessions`, `run_command_all`,
  `session_history`
- **tmux sessions**: `list_sessions`, `create_session`, `session_details`,
  `rename_session`, `kill_session`, `kill_server`, `detach_clients`,
  `list_windows`, `new_window`, `rename_window`, `kill_window`, `capture_pane`,
  `send_keys`, `send_enter`, `send_raw`
- **Run and wait**: `run_and_wait`, `broadcast_wait`
- **Commands**: `run_command`, `run_command_many`
- **Snippets/broadcast**: `list_snippets`, `add_snippet`, `remove_snippet`,
  `broadcast` (`tmux` | `direct`)
- **Tunnels**: `list_tunnels`, `list_tunnels_all`, `start_tunnel`, `stop_tunnel`,
  `stop_tunnels`, `tunnel_health`
- **Rotations**: `list_rotations`, `add_rotation`, `remove_rotation`
- **Audit**: `list_audit_logs`, `read_audit_log`, `list_remote_audit_logs`,
  `read_remote_audit_log`

Registration for opencode (global, `~/.config/opencode/opencode.jsonc`):

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

> Use the absolute path to the executable if it is not on your `PATH`, e.g.
> `/path/to/venv/bin/bravoric-ssh-mcp`.

### Agent skill

The repo ships a skill (`docs/skill/SKILL.md`) with the tool inventory, usage
patterns (create detached → send-keys → capture-pane → kill), naming
conventions and error handling.

```bash
# Claude Code
mkdir -p ~/.claude/skills/bravoric-ssh
cp docs/skill/SKILL.md ~/.claude/skills/bravoric-ssh/

# opencode
mkdir -p ~/.agents/skills/bravoric-ssh
cp docs/skill/SKILL.md ~/.agents/skills/bravoric-ssh/
```

### Headless usage (no GUI)

The MCP server is a Python process on stdio and does not require a desktop:

- **Credentials**: without a system keyring, use the `plain` provider
  (`secrets.tsv`, permissions 0600):
  ```toml
  [credentials]
  provider = "plain"
  ```
- **Config**: point `BRAVORIC_CONFIG` at the file before starting
  `bravoric-ssh-mcp` (or create `~/.config/bravoric-ssh-client/config.toml`).
- The `SSH_ASKPASS` helpers work without a display
  (`SSH_ASKPASS_REQUIRE=force`). Typical systemd daemon:
  ```ini
  [Service]
  Environment=BRAVORIC_CONFIG=/etc/bravoric-ssh-client/config.toml
  ExecStart=/opt/bravoric-ssh-client/.venv/bin/bravoric-ssh-mcp
  ```

### Audit trail (`audit_log = true`)

Interactive sessions are recorded to compressed logs:

- **attach / new tmux session**: `tmux pipe-pane -o 'gzip -c >> file'` is
  injected during the attach and closed on detach. For local hosts the file is
  on this machine (`<config_dir>/logs/YYYYMMDD_ALIAS_SESSION.log.gz`), for
  remote hosts it lives in `~/.bravoric-ssh-client/logs/` on the server.
- **interactive shell** (`s`): if `script` is installed, I/O is recorded
  locally; otherwise the shell starts without logging.

The `.log.gz` files are ready for offline parsing (`zgrep`, regex, ...).

> When you pick an action (attach/new/shell) the TUI exits *before* launching
> ssh, so the terminal is restored cleanly; ssh replaces the process and you
> return to the shell once it ends.

## Testing

```bash
.venv/bin/python -m pytest
```

With coverage and lint:

```bash
.venv/bin/python -m pytest --cov=bravoric_ssh_client
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

## Contributing

This is a personal project, shared to show how I build my own tools. See
[CONTRIBUTING.md](CONTRIBUTING.md): it's small and modular on purpose, so bug
reports and focused pull requests with tests are welcome — but I only take
changes that fit the spirit of the tool.

## License

[MIT](LICENSE) © Riccardo (bravoric) — Made by me, for me.
