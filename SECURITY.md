# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, report it
privately via GitHub's [Security advisories](https://github.com/BravoRicDev/bravoric-ssh-client/security/advisories/new)
or by contacting the maintainer through GitHub.

Include as much detail as possible: affected version/commit, a description of
the issue, and steps to reproduce.

## Scope

`bravoric-ssh-client` is a local client: it stores credentials in your OS
keyring (or a `0600` file if you choose the `plain` provider) and runs the
system `ssh`/`scp`. Relevant areas include:

- credential handling and the `plain` provider file;
- construction of shell commands sent to remote hosts (quoting);
- the `SSH_ASKPASS` helper files;
- the MCP server surface (it can run arbitrary commands on your hosts by
  design — run it only where you trust the connecting agent).

## Known design notes

- The `plain` provider stores passwords **unencrypted** in a `0600` file. Use it
  only on an encrypted home/disk or fall back to the keyring.
- Host key checking uses `StrictHostKeyChecking=accept-new`: a host key is
  accepted on first connection and verified afterwards. Manage
  `~/.ssh/known_hosts` if you need stricter behaviour.
