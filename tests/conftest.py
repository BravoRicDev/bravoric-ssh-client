"""Configurazione condivisa dei test: guardie di piattaforma.

Il prodotto ha due percorsi di esecuzione. Quello **locale**
(``Host(local=True)``) esegue davvero i comandi sull'host corrente, e lo fa con
una shell POSIX: ``adapter._run_local`` invoca letteralmente
``["/bin/sh", "-c", ...]`` e gli script generati chiamano binari POSIX
(``sh``, ``tmux``, ``ss``, ``journalctl``, ``ps``, ``systemctl``).

Su una piattaforma che non li fornisce il test non sta misurando il prodotto:
viene SALTATO dichiarando la capacita' mancante, invece di indebolire
l'asserzione (che nasconderebbe un guasto vero). Per i binari che possono
mancare anche dove la shell c'e' (``ss``/``journalctl`` su macOS) non si salta:
si usa l'asserzione tollerante, cosi' l'invariante verificabile resta sotto test
su ogni piattaforma.

Uso: ``@pytest.mark.posix_local`` sui test che richiedono quel percorso.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Il percorso locale del prodotto invoca letteralmente ``/bin/sh -c``.
HAS_POSIX_SHELL = os.name == "posix" and Path("/bin/sh").exists()

_SKIP_REASON = (
    "richiede il percorso locale POSIX: il prodotto esegue '/bin/sh -c' e script "
    "che invocano binari POSIX (sh, tmux, ss, journalctl, ps)"
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "posix_local: richiede una shell POSIX locale (/bin/sh); saltato altrove",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if HAS_POSIX_SHELL:
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if "posix_local" in item.keywords:
            item.add_marker(skip)
