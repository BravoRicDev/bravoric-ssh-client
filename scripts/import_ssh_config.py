#!/usr/bin/env python3
"""Import esterno: ``~/.ssh/config`` -> ``config.toml`` per bravoric-ssh-client.

CLI attorno a ``bravoric_ssh_client.importers.import_from_ssh_config``.

Uso:
    python scripts/import_ssh_config.py [--out ~/.config/bravoric-ssh-client/config.toml]
                                        [--dry-run]
                                        [--merge]
                                        [--ssh-config ~/.ssh/config]
                                        [--auth keyring|plain|key]
                                        [--exclude 'host1 host2']
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    default_out = Path.home() / ".config" / "bravoric-ssh-client" / "config.toml"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(default_out), help="percorso output config.toml")
    parser.add_argument("--ssh-config", default=str(Path.home() / ".ssh" / "config"))
    parser.add_argument("--dry-run", action="store_true", help="stampa su stdout senza scrivere")
    parser.add_argument(
        "--merge", action="store_true", help="aggiungi solo host nuovi al file esistente"
    )
    parser.add_argument("--provider", default="keyring", choices=["keyring", "plain"])
    parser.add_argument(
        "--auth",
        default="keyring",
        help="auth di default per gli host importati (keyring|plain|key|prompt)",
    )
    parser.add_argument("--exclude", default="", help="alias da escludere, separati da spazio")
    args = parser.parse_args(argv)

    ssh_path = Path(args.ssh_config).expanduser()
    if not ssh_path.exists():
        print(f"File ssh config non trovato: {ssh_path}", file=sys.stderr)
        return 1

    from bravoric_ssh_client.config import serialize_config
    from bravoric_ssh_client.importers import import_from_ssh_config, parse_ssh_config

    if args.dry_run:
        entries = parse_ssh_config(ssh_path)
        excluded = set(args.exclude.split())
        entries = [e for e in entries if e.alias not in excluded]
        from bravoric_ssh_client.config import Config
        from bravoric_ssh_client.importers import entries_to_hosts

        cfg = Config(
            credential_provider=args.provider,
            hosts=entries_to_hosts(entries, args.auth),
        )
        sys.stdout.write(serialize_config(cfg))
        return 0

    total, added = import_from_ssh_config(
        ssh_path,
        Path(args.out).expanduser(),
        provider=args.provider,
        default_auth=args.auth,
        merge=args.merge,
        exclude=set(args.exclude.split()),
    )
    if total == 0:
        print("Nessun host importabile.", file=sys.stderr)
        return 1
    print(f"Scritti {total} host in {Path(args.out).expanduser()} ({added} nuovi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
