# Architecture

How `bravoric-ssh-client` is put together. The goal is a deliberately small,
modular client: a Textual TUI and an MCP server share the same SSH/credentials
core, with no agents or services required on the servers.

## Design principles

1. **No external services.** The client talks to servers over standard SSH. It
   does not require agents, cloud accounts or third-party APIs.
2. **Cross-platform by design.** Linux, macOS and Windows. Credentials go
   through an abstract layer, so the app never depends on a single OS keyring.
3. **Configured, not hardcoded.** Hosts, credentials and behaviour come from a
   TOML file and importers, not from code.
4. **Delegate to OpenSSH.** Interactive sessions are `ssh -t`; tmux listing and
   actions are non-interactive `ssh` commands. No custom network stack.
5. **One core, two frontends.** The TUI and the MCP server are thin layers on
   top of `config`, `credentials` and `ssh`.

## Package layout

| Path | Responsibility |
|------|----------------|
| `bravoric_ssh_client/app.py` | Textual TUI: host screen, session screen, observe/rotation, dialogs, key bindings |
| `bravoric_ssh_client/config.py` | `Host`/`Tunnel`/`Config` dataclasses, TOML load/serialize, validation, backups |
| `bravoric_ssh_client/history.py` | Recent-session history (`history.json`) |
| `bravoric_ssh_client/rotation.py` | Rotation profiles (`rotations.json`) |
| `bravoric_ssh_client/snippets.py` | Snippet catalog (`snippets.json`) |
| `bravoric_ssh_client/importers.py` | Importers from `~/.ssh/config` and Remmina |
| `bravoric_ssh_client/mcp_server.py` | MCP server on stdio exposing the whole client as tools |
| `bravoric_ssh_client/credentials/` | `CredentialProvider` interface + `keyring` and `plain` backends and a per-host factory |
| `bravoric_ssh_client/ssh/adapter.py` | Non-interactive remote commands (tmux listing/actions), askpass plumbing |
| `bravoric_ssh_client/ssh/tmux_runner.py` | Interactive `ssh -t` sessions (attach/new/shell), exec on POSIX |
| `bravoric_ssh_client/ssh/tunnels.py` | Background SSH port forwards (`-L`/`-R`/`-D`) and their shared state |
| `bravoric_ssh_client/ssh/broadcast.py` | Parallel snippet execution (tmux or direct) |
| `bravoric_ssh_client/ssh/commander.py` | Midnight Commander launcher for two-panel file exchange |
| `bravoric_ssh_client/ssh/file_ops.py` | `scp` upload/download |
| `bravoric_ssh_client/ssh/audit.py` | Session recording (`tmux pipe-pane` / `script`) and log reading |
| `bravoric_ssh_client/ssh/shellutil.py` | Shared POSIX quoting and secure `SSH_ASKPASS` helper generation |
| `scripts/` | Standalone importers (`import_ssh_config.py`, `import_keyring_credentials.py`) |
| `docs/skill/SKILL.md` | Agent skill describing how to drive the MCP server |

## Credentials

`credentials/` defines a `CredentialProvider` (get/set/delete) with two
implementations:

- **`keyring`** — delegates to the OS secret store through the `keyring`
  library: SecretService (GNOME), KWallet (KDE), Keychain (macOS), Credential
  Manager (Windows). Recommended.
- **`plain`** — a local `secrets.tsv` file with `0600` permissions, for systems
  without a keyring and for headless use.

Each host can override the global provider via `auth` (`""` = global,
`keyring`, `plain`, `key`, `prompt`). The credential key defaults to
`user@host`.

## Authentication without sshpass

The client never depends on `sshpass`. Non-interactive commands get passwords
through the standard OpenSSH mechanism: a small `SSH_ASKPASS` helper (created
with `tempfile.mkstemp`, mode `0700`) plus `SSH_ASKPASS_REQUIRE=force`. For a
jump host, a **multi-host** helper matches the ssh prompt to the right password.
This works on Linux, macOS and recent Windows OpenSSH, and needs no display.

Flow for a non-interactive command:

1. Try `BatchMode=yes` (key/agent) — no password.
2. If that fails and a password is available, retry with the askpass helper.
3. If no password is configured, surface the error.

## State files

Everything lives under the config dir (`~/.config/bravoric-ssh-client/` unless
overridden by `BRAVORIC_CONFIG`/XDG):

- `config.toml` (+ `config.toml.bak.<ts>` backups)
- `history.json`, `rotations.json`, `snippets.json`, `tunnels.json`
- `secrets.tsv` (only with the `plain` provider)
- `logs/` (audit logs, `.log.gz`)

## MCP server

`mcp_server.py` wraps the same core in an MCP server on stdio. The interactive
`attach` is intentionally **not** exposed (incompatible with stdio); agents
instead create detached tmux sessions and drive them with `send-keys` /
`capture-pane`, while the human attaches from a terminal or the TUI. Tunnel
state is shared with the TUI through `tunnels.json`.
