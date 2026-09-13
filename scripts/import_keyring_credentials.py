#!/usr/bin/env python3
"""Importa credenziali SSH da altri programmi nel keyring di bravoric-ssh-client.

Riconosce gli item del gnome-keyring/Secret Service scritti da:
  - Remmina     (schema org.gnome.keyring.NetworkPassword, attrs user/server)
  - SSH Pilot   (schema io.github.mfat.sshpilot,      attrs username/host)
  - qualsiasi item con label "user@host"

Per ogni host copia la password nello spazio ``bravoric-ssh-client`` usando la
libreria ``keyring`` (cross-platform: su KDE/macOS/Windows funziona allo stesso
modo con il rispettivo backend).

Uso:
    python3 scripts/import_keyring_credentials.py [--dry-run] [--force]
                            [--only host1 host2 ...]

La libreria ``keyring`` è già una dipendenza del progetto; per la lettura dei
segreti di sistema si usa pygobject (Secret Service) quando disponibile, altrimenti
si limita a ri-etichettare gli item a label "user@host" attraverso la lib ``keyring``.
"""

from __future__ import annotations

import argparse
import sys
from importlib.util import find_spec

SERVICE = "bravoric-ssh-client"


def discover_from_secret_service() -> dict[str, str]:
    """Legge gli item SSH esistenti (Remmina/SSH Pilot) via Secret Service."""
    if find_spec("gi") is None:
        return {}
    import gi  # type: ignore

    gi.require_version("Secret", "1")
    from gi.repository import Secret  # type: ignore

    svc = Secret.Service.get_sync(Secret.ServiceFlags.LOAD_COLLECTIONS)
    out: dict[str, str] = {}
    for col in svc.get_collections():
        if col.get_locked():
            continue
        for it in col.get_items():
            attrs = {k: v for k, v in it.get_attributes().items()}
            schema = attrs.get("xdg:schema", "")
            # solo item di connessione SSH (Remmina/NetworkPassword o SSH Pilot)
            if schema not in ("org.gnome.keyring.NetworkPassword", "io.github.mfat.sshpilot"):
                continue
            protocol = (attrs.get("protocol") or "").lower()
            if schema == "org.gnome.keyring.NetworkPassword" and protocol not in ("ssh", "sftp"):
                continue
            user = attrs.get("user") or attrs.get("username")
            server = attrs.get("server") or attrs.get("host")
            if not (user and server):
                continue
            sec = it.retrieve_secret_sync()
            if sec is None:
                continue
            text = sec.get_text()
            if text:
                out[f"{user}@{server}"] = text
    return out


def discover_from_labels() -> dict[str, str]:
    """Fallback: itera item del keyring tramite libreria 'keyring' per label user@host."""
    if find_spec("gi") is None:
        return {}
    import gi  # type: ignore

    gi.require_version("Secret", "1")
    from gi.repository import Secret  # type: ignore

    svc = Secret.Service.get_sync(Secret.ServiceFlags.LOAD_COLLECTIONS)
    out: dict[str, str] = {}
    for col in svc.get_collections():
        if col.get_locked():
            continue
        for it in col.get_items():
            label = (it.get_label() or "").strip()
            if ":" in label:
                label = label.split(":", 1)[1].strip()
            if "@" not in label:
                continue
            sec = it.retrieve_secret_sync()
            if sec is None:
                continue
            text = sec.get_text()
            if text:
                out[label] = text
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="mostra cosa copierebbe senza scrivere"
    )
    parser.add_argument(
        "--force", action="store_true", help="sovrascrivi password già presenti nell'app"
    )
    parser.add_argument("--only", nargs="*", default=[], help="solo questi user@host")
    args = parser.parse_args(argv)

    creds = discover_from_secret_service()
    if not creds:
        creds = discover_from_labels()
    if not creds:
        print("Nessuna credenziale trovata nel keyring di sistema.", file=sys.stderr)
        return 1

    if args.only:
        wanted = {x.strip().lower() for x in args.only}
        creds = {k: v for k, v in creds.items() if k.lower() in wanted}

    import keyring  # dipendenza del progetto

    copied = 0
    for key, value in sorted(creds.items()):
        existing = keyring.get_password(SERVICE, key)
        if existing is not None and not args.force:
            print(f"  = {key}: già presente (usa --force per sovrascrivere)")
            continue
        if args.dry_run:
            print(f"  + {key} (dry-run)")
            copied += 1
            continue
        keyring.set_password(SERVICE, key, value)
        print(f"  + {key}")
        copied += 1

    print(
        f"Fatto: {copied} credenziali {'simulate' if args.dry_run else 'copiate'} nello spazio '{SERVICE}'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
