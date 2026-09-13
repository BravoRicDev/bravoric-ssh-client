# Roadmap

A high-level view of what is done and what could come next. For how the code is
organised, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Done

- **Core & config** — installable package, TOML config with validation and
  automatic backups, `Host`/`Tunnel` models.
- **Credentials** — abstract provider with `keyring` and `plain` backends and
  per-host override; importers from Remmina/SSH Pilot.
- **SSH adapter** — non-interactive tmux listing and actions; interactive
  `ssh -t` runner (attach/create/shell); password handling via `SSH_ASKPASS`
  (no `sshpass`).
- **TUI** — host screen with filter, groups, add/edit/delete/duplicate,
  password management, TCP ping and mass ping, recent history.
- **Sessions** — attach, read-only attach, create, rename, kill/kill-all,
  details, windows, new window, detach clients, local tmux, `HOST - SESSION`
  window title, optional TUI restart.
- **Import** — `~/.ssh/config` and Remmina, both as scripts and from the TUI.
- **File exchange** — `scp` copy and two-panel Midnight Commander with
  automatic passwords.
- **Advanced networking** — SSH tunnels (`-L`/`-R`/`-D`, shared state),
  jump host/bastion (`-J`), broadcast snippets (tmux or direct).
- **Monitoring** — rotation profiles and the observe screen (auto-cycle with an
  optional interactive mode).
- **Audit** — session recording to gzip logs (`pipe-pane` / `script`).
- **MCP server** — the client exposed on stdio (47 tools) with a bundled agent
  skill; headless/systemd usage documented.
- **CI** — GitHub Actions across Linux/macOS/Windows, Python 3.11+.

> The five founding principles and the full module map live in
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Next (ideas)

- **Themes / visual customization** for the Textual UI.
- **Per-host action menu** for quick commands.
- **Multiple profiles / VPN profiles.**
- **Optional metrics module** (kept separate, opt-in).
- **Notifications / webhooks** on broadcast or session events.

Ideas are welcome — open an issue to discuss before sending a large change.
