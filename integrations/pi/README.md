# Pi Agent integration — Bravoric SSH TUI

The [Pi Agent](https://github.com/earendil-works/pi) side of `bravoric-ssh-client`:
a status line and a set of commands that drive SSH hosts and tmux sessions
without leaving the agent, plus the Python bridge they call into.

| File | What it is |
|---|---|
| `bravoric-remote.ts` | The Pi Agent extension (TypeScript, ~1000 lines). |
| `bravoric-bridge.py` | The CLI bridge the extension shells out to; it speaks to `bravoric_ssh_client`'s MCP layer. |

## What it gives you

A always-visible line with the selected host, session, mode and window, and:

| Command | Shortcut | What it does |
|---|---|---|
| `/ssh` | `Ctrl+H` | Control menu (host, session, paste, windows, restart, diff, ping). |
| `/ssh-select` | | Host → session picker; never opens a terminal by force. |
| `/ssh-window` | `F6` | Opens the dedicated terminal window for the selected session. |
| `/ssh-toggle` | | Switches planning (LOCAL) ↔ remote execution (REMOTE). |
| `/ssh-close` | | Closes the foreground process in the current tmux session (`auto\|sigint\|sigint2\|eof\|exit`). |
| `/ssh-paste` | | Pastes text or `@file:<path>` into the session with bracketed paste. |
| `/ssh-info` | | Process, CWD, PID, title and geometry of the active pane. |
| `/ssh-windows` | | Lists / activates / creates tmux windows. |
| `/ssh-restart` | | Closes and restarts the foreground process (`/ssh-restart [command]`). |
| `/ssh-diff` | | Incremental pane diff: only new lines, to save tokens (`reset` to re-baseline). |
| `/ssh-host` | | Switches the active host: `/ssh-host <alias>`. |

LLM context is toggled LINKED/UNLINKED: when unlinked, the operations do not
consume prompt tokens.

## Requirements

- Pi Agent with `@earendil-works/pi-coding-agent`.
- `bravoric-ssh-client` installed in a virtualenv (the bridge imports
  `bravoric_ssh_client`).
- The `bravoric-ssh` executable available.
- Python 3 with `Pillow`-free dependencies of the client (see the parent repo).

## Install

Pi Agent loads every `.ts` file it finds in `~/.pi/agent/extensions/`. The
recommended install is a **symlink**, so there is exactly one copy of the code
and updates are a `git pull`:

```bash
ln -sfn "$PWD/integrations/pi/bravoric-remote.ts" ~/.pi/agent/extensions/bravoric-remote.ts
ln -sfn "$PWD/integrations/pi/bravoric-bridge.py" ~/.pi/agent/extensions/bravoric-bridge.py
```

Extensions are loaded **at process start**: restart Pi Agent for changes to take
effect (`/reload` does not re-evaluate extension code).

## Configuration

Every path is resolved at load time: first an explicit environment variable, then
a list of known locations. Nothing is pinned to one machine.

| Variable | Default (first that exists) |
|---|---|
| `BRAVORIC_SSH_CLIENT_DIR` | `~/Progetti/bravoric-ssh-client`, `~/progetti/bravoric-ssh-client` |
| `BRAVORIC_PYTHON` | `<client>/.venv/bin/python`, `<client>/.venv/bin/python3`, `/usr/bin/python3` |
| `BRAVORIC_BRIDGE` | `~/.pi/agent/extensions/bravoric-bridge.py`, `<client>/integrations/pi/bravoric-bridge.py` |
| `BRAVORIC_BIN` | `~/.local/bin/bravoric-ssh`, `/usr/local/bin/bravoric-ssh`, `/opt/homebrew/bin/bravoric-ssh` |

The bridge also honours `BRAVORIC_PANE_DIFF_CACHE` (default
`~/.cache/bravoric/pane_diff.json`) for the incremental pane diff state.

## Safety

- Hosts marked as **production** ask for an explicit confirmation before remote
  mode is activated or a command is sent.
- `/ssh-close` and `/ssh-restart` act on the **foreground process of the active
  tmux session**: a `node`/`python` TUI is not mistaken for a shell, but a
  session whose foreground is a real shell will be refused.
- No credentials are stored here: authentication is the client's job (SSH
  config, keys, keyring).

## Known limits

- No automated test suite: the extension is exercised manually against a live
  host. Changes should be typechecked (`tsc --noEmit` with the Pi types
  available) and tried on a non-production host first.
- The status line reads the state of the **selected** session only; a session
  closed outside Pi is detected on the next poll.

## Provenance

Imported on 2026-10-05 from the machine-local file
`~/.pi/agent/extensions/bravoric-remote.ts`, which until then was tracked by no
repository. The first commit contains the byte-identical original, so the file's
history is recoverable from git.

## Italiano (sintesi)

Integrazione lato Pi Agent del client SSH: riga di stato sempre visibile e
comandi (`/ssh`, `F6`, `/ssh-select`, `/ssh-close`, `/ssh-paste`, `/ssh-diff`,
`/ssh-restart`, …) per pilotare host e sessioni tmux restando nell'agente, con
conferma esplicita sugli host di produzione e toggle LOCALE/REMOTE.
L'installazione consigliata è un symlink in `~/.pi/agent/extensions/` verso
questi due file; i percorsi si configurano con variabili d'ambiente, così il
codice non dipende da una macchina sola.
