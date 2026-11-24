# Contributing

Issues and pull requests are welcome. For anything bigger than a small fix, please open an issue first so we can agree on the approach.

## Set up

```sh
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Before you push

```sh
ruff check .
ruff format --check .
pytest
```

- `tests/test_core.py` covers the parsing helpers and needs no Docker.
- `tests/test_app.py` drives the real dashboard headlessly against your Docker engine. It starts a throwaway `alpine` container (in a fake `dm-test` compose project) and removes it afterwards. It's skipped when no engine is reachable.
- To try your change by hand against a known stack, use `docker compose -f demo/compose.yaml up -d`.

## Guidelines

- Keep it a single file that only talks to the `docker` CLI: no Docker SDK, no extra services.
- Never print secrets: anything that shows container configuration must keep hiding environment values.
- Destructive actions (stop, remove) must ask first.
- CI runs on Linux, macOS and Windows, so avoid platform-specific behaviour, or guard it.
