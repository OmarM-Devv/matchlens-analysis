# MatchLens Football Analysis

Local backend for the StatsBomb open-data Premier League 2003/04 season
(competition 2, season 44): 38 matches and their shot events, loaded into
PostgreSQL and served through FastAPI.

```
db/models.py        matches · team_match_views · shots (composite FK shots → team view)
db/loader.py        transactional, idempotent snapshot import  (python -m db.loader)
db/engine.py        engine + schema helpers
api/main.py         /api/teams/{id}/form · /api/shots/analysis · / (dashboard) · /health
tests/              PostgreSQL integration suite (31 assertions)
docker/postgres/    init script creating the read-only API role
scripts/            StatsBomb open-data downloader
```

## Run with Docker

```bash
cp .env.example .env                 # then set both passwords
python scripts/fetch_statsbomb.py    # writes data/statsbomb/...
docker compose up --build            # db → loader → api on http://127.0.0.1:8000
```

The loader runs to completion before the API starts. Re-running it is safe:

```bash
docker compose run --rm loader
```

Run the integration tests against the compose database (they use a throwaway schema):

```bash
docker compose --profile test run --rm tests
```

## Run without Docker

```bash
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql+psycopg://matchlens:<password>@localhost:5432/matchlens
python -m db.loader --data-dir data/statsbomb
uvicorn api.main:app --reload
TEST_DATABASE_URL=$DATABASE_URL pytest
```

## Import guarantees

Each import is one PostgreSQL transaction holding `pg_advisory_xact_lock(competition, season)`:

| Situation | Result |
|---|---|
| Same snapshot imported again | Matches updated in place, children replaced; row counts unchanged |
| Upstream correction (score, shot removed) | New values replace old; removed shots disappear |
| Match missing from new snapshot | Match pruned, its views and shots cascade away |
| Any DB error mid-import | Full rollback; previous import untouched |
| Count/consistency check fails | Rejected before a transaction is opened |
| Two imports at once | Serialised by the advisory lock; no duplicates |

The CLI refuses snapshots that do not contain exactly 38 matches and 1,086
shots (override with `--expected-matches`, `--expected-shots` or `--no-count-check`).

## Security

The API connects as `MATCHLENS_RO_USER`, created by
`docker/postgres/initdb/10-readonly-role.sh` with `SELECT`-only grants and
`default_transaction_read_only = on`. The init script runs only when the data
volume is first created; `docker compose down -v` resets it.

Data: [StatsBomb open data](https://github.com/statsbomb/open-data).
