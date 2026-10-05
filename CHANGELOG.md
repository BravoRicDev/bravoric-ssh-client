# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- First public release of the SSH + tmux TUI client.
- Credential providers (`keyring`, `plain`) with per-host override.
- Import from `~/.ssh/config` and Remmina.
- File exchange via `scp` and Midnight Commander.
- MCP file transfer tools: `sftp_download`, `sftp_upload`, `transfer_file`
  (host-to-host relay via local temp, with md5).
- SSH tunnels (`-L`/`-R`/`-D`), jump host/bastion, broadcast snippets.
- Rotation profiles and the observe screen.
- Session audit trail to gzip logs.
- MCP server (`bravoric-ssh-mcp`) and bundled agent skill.
- CI on Linux/macOS/Windows with pytest, ruff and coverage.
- Remote tmux sessions (attach, new session, plain shell — from the session
  screen, the observe screen, the quick-launch panel and the agent launcher) now
  open in a **new window** of a terminal chosen from a list at launch time,
  instead of taking over the terminal running the TUI. Handy when a session was
  resized from elsewhere and needs a terminal that can zoom out.
  `BRAVORIC_TERMINAL` pins one terminal and skips the prompt; the prompt is also
  skipped when only one graphical terminal is installed.

### Fixed

- The TUI no longer freezes at startup when a system keyring is locked or
  unavailable (e.g. headless SecretService): keyring calls are bounded by a
  timeout and password markers are computed off the UI thread.

[Unreleased]: https://github.com/BravoRicDev/bravoric-ssh-client/commits/main
