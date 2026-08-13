# Hades V2

Quantitative research system for **Pump.fun tokens on Solana**. Paper trading only.

The goal is not a complex bot. It is a system that can answer, with real data:

> *What observable conditions in the first minutes of a Pump.fun token have predictive
> value and produce a positive expectancy after slippage, fees and risk?*

This is a **greenfield rebuild**. Hades V1 (`../Hades`) is reference material for
mistakes and reusable decisions — not a codebase to port. See `docs/DECISIONS.md`.

## Status

**Phase 0 — Foundation. Complete.** Phases advance only when the current one passes
its gate; nothing beyond Phase 0 is built.

| Phase | Scope | State |
|---|---|---|
| 0 | Repo, config, PostgreSQL, Alembic, Docker, logging, FastAPI, health/status, tests | ✅ |
| 1 | Verify real data sources → `docs/DATA_SOURCES.md` | ⬜ next |
| 2 | Token discovery | ⬜ |
| 3 | Snapshot tracking | ⬜ |
| 4 | Feature engine | ⬜ |
| 5 | Signal research (one hypothesis) | ⬜ |
| 6 | Paper trading | ⬜ |
| 7 | Outcome + analytics | ⬜ |

## Run it

```bash
cp .env.example .env   # set POSTGRES_PASSWORD and HADES_DATABASE_URL
docker compose up -d   # migrate runs to head, then the API starts
curl -s localhost:8000/status
```

Locally, without Docker:

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m hades
```

## Quality gate

Every phase ends here. It must be green before the next one starts.

```bash
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check . && .venv/Scripts/mypy src && .venv/Scripts/pytest
```

## Safety

No private keys, no signer, no transaction submission — enforced by an AST scan in
`tests/test_safety.py` that fails the build if a signing library or key-material
identifier reaches `src/`. See `docs/SAFETY.md`.

## Layout

```
src/hades/
  config.py       Settings (env-driven, frozen, rejects sync DB drivers)
  logging.py      stdlib JSON-lines logging
  db/             engine + session lifecycle, ORM models
  api/            FastAPI app, /health, /status
migrations/       Alembic
docs/             DECISIONS.md, SAFETY.md, DEPLOYMENT.md
tests/
```
