# Contributing

`bravoric-ssh-client` is a **personal tool**, shared on GitHub to show how I
reason and how I build the tools I use every day. It is not a supported product:
there is no roadmap and no SLA.

That said, it is small and modular on purpose, so bug reports and focused pull
requests with tests are welcome — as long as they fit the spirit of the tool. For
anything non-trivial, open an issue first to discuss the approach.

## Development setup

```bash
git clone https://github.com/BravoRicDev/bravoric-ssh-client.git
cd bravoric-ssh-client
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pre-commit install     # optional but recommended
```

Run the app:

```bash
.venv/bin/bravoric-ssh
```

## Before you push

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest
```

The same checks run in CI. To auto-fix style issues:

```bash
.venv/bin/ruff check --fix .
.venv/bin/ruff format .
```

## Guidelines

- Keep changes small and focused; prefer a clear module over a big one.
- Add or update tests for any behaviour change. The suite is pure Python and
  does not need real servers.
- Match the existing style. Code comments should be rare and in Italian, like
  the rest of the codebase; user-facing docs are bilingual (EN + IT).
- **Never commit secrets.** `secrets.tsv`, `config.toml`, runtime JSON files and
  logs are git-ignored — double-check before committing.
- For anything non-trivial, open an issue first to discuss the approach.

## Documentation

User-facing documentation is bilingual: keep `README.md` (English) and
`README.it.md` (Italian) in sync. Architecture notes go in
`docs/ARCHITECTURE.md`.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
