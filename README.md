# MatchLens Football Analysis

Descriptive match analysis of Leicester City's 2015/16 Premier League season, built on StatsBomb open data, PostgreSQL and FastAPI.

## Screenshot

![Match report: Manchester City 1–3 Leicester City, 6 February 2016](docs/screenshots/match-report.png)

The match-report page (`/matches/{match_id}`), captured from a local run against
the loaded 2015/16 data.

## Project Scope

- **Descriptive analysis only.** Every figure is a count, sum, average or
  window aggregate over recorded events and official results. Nothing is
  predicted, forecast or modelled.
- **xG is provided by StatsBomb.** The project stores StatsBomb's per-shot
  `statsbomb_xg` and aggregates it. It does not compute xG itself.
- **CI only, not deployed.** GitHub Actions runs the test suite. The stack runs
  locally under Docker Compose, bound to `127.0.0.1`. There is no hosted
  instance.

## Tech Stack

| Layer | Technology |
|---|---|
| Database | PostgreSQL 16 |
| Schema and queries | Plain SQL files: `sql/schema.sql`, `sql/queries.sql` |
| Loader | Python 3.12, SQLAlchemy 2.0 (Core `text()` statements), psycopg 3 |
| Web app | FastAPI 0.115, Jinja2 server-rendered templates, uvicorn |
| Tests | pytest, httpx (FastAPI `TestClient`) |
| Runtime | Docker Compose (db, loader, api, tests) |
| CI | GitHub Actions |

## Architecture

```
scripts/fetch_statsbomb.py     download the season match list + Leicester's 38 event files
        │                      → data/statsbomb/matches/2/27.json, data/statsbomb/events/<match_id>.json
        ▼
loader/cleaner.py              parse → validate (38 matches, 1,042 shots) → one transaction
        │                      applies sql/schema.sql, then writes teams · matches · shots
        ▼
PostgreSQL                     sql/schema.sql: 3 tables + team_match_view
        │
        ▼
app/main.py                    loads the named queries in sql/queries.sql
        │                      JSON: /api/matches · /api/matches/{id}/report · /api/form · /api/home-away · /health
        ▼
app/templates/                 pages: / (season overview) · /matches/{id} (match report)

tests/                         PostgreSQL integration suite (16 tests, 31 assertions)
docker/postgres/initdb/        creates the read-only role the app connects as
.github/workflows/ci.yml       runs the suite on every push to main and every pull request
docs/                          limitations.md · failure_diagnosis.md · screenshots/
```

## Data and Scope

| | |
|---|---|
| Source | [StatsBomb open data](https://github.com/statsbomb/open-data) |
| Competition / season | Premier League 2015/16 (StatsBomb competition 2, season 27) |
| Team | Leicester City (StatsBomb team 22) |
| Matches | 38 (19 home, 19 away) |
| Shot events | 1,042 in total: 525 by Leicester, 517 by opponents |
| Teams | 20 (Leicester and its 19 opponents) |

The season file lists all 380 league matches. The loader keeps the 38 Leicester
played. Loaded, the data reproduces the official record: 23 wins, 12 draws,
3 defeats, 68 goals for, 36 against, 81 points.

## Data Model

Defined in [`sql/schema.sql`](sql/schema.sql):

| Object | Grain | Notes |
|---|---|---|
| `teams` | one row per club | `team_id` is StatsBomb's id |
| `matches` | one row per fixture | official `home_score` / `away_score`; FKs to `teams`; CHECKs on distinct teams, non-negative scores, match week range |
| `shots` | one row per StatsBomb shot event | `statsbomb_xg`, outcome, body part, technique, location; generated columns `is_goal` and `is_on_target`; `ON DELETE CASCADE` from `matches`; CHECKs on pitch coordinates, clock, period and xG in [0, 1]; unique `(match_id, event_index)` |
| `team_match_view` | two rows per match, one per side | `UNION ALL` of the home and away perspective, with `goals_for`, `goals_against`, `result` and `points` derived from the official score |

A trigger, `shots_team_in_match`, rejects any shot credited to a team that did
not play in that match. A foreign key cannot express "home or away team", so a
trigger does it.

## Ingestion

`python -m loader.cleaner` (the `loader` service in compose) reads the files
written by `scripts/fetch_statsbomb.py`.

Before any database write, the snapshot is validated. It is rejected unless it
has exactly 38 matches and 1,042 shots, no duplicate match or shot ids, every
match in the expected competition-season, every shot credited to a team in its
match, an events file for every match, and one name per team id.

The import then runs as **one PostgreSQL transaction** holding
`pg_advisory_xact_lock(competition_id, season_id)`:

| Situation | Result |
|---|---|
| Same snapshot imported again | Matches updated in place, shots replaced; row counts unchanged |
| Upstream correction (score changed, shot removed) | New values replace old; removed shots disappear |
| Match missing from a new snapshot | Match pruned; its shots cascade away |
| Any database error mid-import | Full rollback; the previous import is untouched |
| Count or consistency check fails | Rejected before a transaction is opened |
| Two imports at once | Serialised by the advisory lock; no duplicates |
| Database unreachable | Gives up after the 10 s connect timeout with exit code 1; nothing written |

After writing, the loader counts the matches and shots in the database and rolls
back if they differ from the snapshot. The expected counts can be overridden
with `--expected-matches` / `--expected-shots` (or `MATCHLENS_EXPECTED_*`), or
turned off with `--no-count-check`.

## Analytical Layer

[`sql/queries.sql`](sql/queries.sql) holds three analytical queries, each under
a `-- name:` header that `app/main.py` loads by name:

| Query | Output | Served at |
|---|---|---|
| `match_report` | One match from both sides: official goals, result, points, shots, shots on target, goals from shots, goals not from shots, xG, xG per shot | `/matches/{id}`, `/api/matches/{id}/report` |
| `rolling_form` | Per match: rolling N-match points, goal difference, average xG and xGA, and form string (`ROWS` window functions), plus cumulative points | `/`, `/api/form?window=5` |
| `home_away` | Home vs away: played, W/D/L, points, points per match, goals, shots, xG and xGA totals and per match | `/`, `/api/home-away` |

A fourth statement, `team_matches`, is a plain fixture lookup for navigation.

**Aggregate before join.** Each analytical query first groups `shots` to one
row per `(match_id, team_id)` in a CTE and only then joins that to
`team_match_view`. Joining raw shots to the view before summing would repeat
each match's goals and points once per shot. The test
`test_home_away_aggregates_shots_before_joining` pins the correct totals.

## Metric Definitions

| Metric | Definition |
|---|---|
| Goals, result, points | From the official score in the match record: 3 for a win, 1 for a draw, 0 for a defeat |
| xG / xGA | Sum of StatsBomb's `statsbomb_xg` over the team's / opponent's shots, as provided |
| Shots | StatsBomb events of type `Shot`, penalties included |
| Shots on target | Shots with outcome `Goal`, `Saved` or `Saved to Post` |
| Goals from shots | Shots with outcome `Goal` |
| Goals not from shots | Official goals minus goals from shots. StatsBomb records own goals as separate events, not shots, so in league data this equals own goals scored in the team's favour. 2015/16 Leicester has one: Leicester City 2–2 West Bromwich Albion, 1 March 2016 |
| xG per shot | xG divided by shots |

## Setup

Requires Git, Docker with Compose, and Python 3 on the host (the fetch script
uses only the standard library).

```bash
git clone https://github.com/OmarM-Devv/matchlens-analysis.git
```

```bash
cd matchlens-analysis
```

```bash
cp .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD` and `MATCHLENS_RO_PASSWORD`.

```bash
python scripts/fetch_statsbomb.py
```

This downloads about 105 MB into `data/statsbomb/`.

```bash
docker compose up --build
```

Compose starts `db`, runs `loader` to completion, then starts `api`.

| Service | Address |
|---|---|
| Web app, season overview | http://127.0.0.1:8000/ |
| Match report | http://127.0.0.1:8000/matches/{match_id} |
| OpenAPI docs | http://127.0.0.1:8000/docs |
| Health check | http://127.0.0.1:8000/health |
| PostgreSQL | 127.0.0.1:5432 (database and user `matchlens`) |

Both ports can be changed with `API_PORT` and `POSTGRES_PORT` in `.env`. Both
bind to `127.0.0.1` only. The app connects as the read-only role
`MATCHLENS_RO_USER`, which has `SELECT` grants only.

Re-running the import is safe:

```bash
docker compose run --rm loader
```

A database created before this layout (with a `team_match_views` table) is
refused by the loader. Reset it with `docker compose down -v`, which deletes all
loaded data.

## Testing

```bash
docker compose --profile test run --rm tests
```

Or, against any PostgreSQL where the role can `CREATE SCHEMA`:

```bash
TEST_DATABASE_URL=postgresql+psycopg://matchlens:<password>@127.0.0.1:5432/matchlens pytest
```

The suite has 16 tests containing 31 assertions. Each session creates a
throwaway schema and drops it afterwards. The fixture is a synthetic 6-match
season (33 shots, one own goal, and one match the focus team did not play).

| Area | What is asserted |
|---|---|
| First import | Only the focus team's matches load; counts for teams, matches, shots and the team view |
| Repeat import | Idempotent: counts and shot rows unchanged, `loaded_at` advances |
| Upstream corrections | Changed score updates the result and points; removed shot disappears |
| Pruning | A match dropped from the snapshot is deleted along with its shots |
| Rollback | A constraint failure mid-import leaves matches and shots exactly as before |
| Validation | Wrong shot count, shot for a team not in the match, and missing events file are rejected with nothing written |
| Schema | The team-in-match trigger, the xG range CHECK and the distinct-teams CHECK reject bad rows; the pre-refactor layout is refused |
| Concurrency | Two simultaneous imports serialise with no duplicates |
| Match report | Official goals, goals from shots, goals not from shots, shots, shots on target and xG for both sides |
| Rolling form | Window sizes, rolling points and form string for a 5-match window |
| Home vs away | Played, points, goals and shots per venue (aggregate-before-join) |
| Pages | Season overview and match report render |
| Outage | `/health` and `/` return 503 when the database is unreachable |

The real 38-match snapshot is not part of the suite. Its counts are enforced by
the loader at import time.

## CI Configuration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push to
`main` and every pull request. It starts a `postgres:16-alpine` service
container, installs `requirements-dev.txt` on Python 3.12 and runs `pytest`.
When `CI` is set and no database is configured, the database fixture fails
instead of skipping, so the job cannot pass without running the integration
tests. For the job to block merges, it also has to be marked as a required
status check in the repository's branch protection settings. The workflow has
no build, publish or deploy steps.

## Failure Exercise

[`docs/failure_diagnosis.md`](docs/failure_diagnosis.md) records a controlled
PostgreSQL outage. PostgreSQL was stopped while the app was serving requests,
the symptoms were observed, and the database was restored.

- **Found:** with the database down, each request hung for 130 seconds before
  returning 503. That is psycopg's default connect deadline, because the engine
  had no `connect_timeout`.
- **Fixed:** the app now uses a 3-second connect timeout and the loader a
  10-second one. With the fix, `/health`, pages and JSON endpoints returned 503
  in about 3 seconds, and the loader exited with code 1 after 12 seconds,
  writing nothing.
- **Recovered:** after PostgreSQL restarted, the app returned 200 again without
  a restart. Row counts were unchanged at 38 matches and 1,042 shots.

The document also has a compose runbook for diagnosing and restoring an outage.
The recorded run used a local PostgreSQL 16.4 under `pg_ctl` rather than the
compose stack.

## Limitations

Summarised from [`docs/limitations.md`](docs/limitations.md), which also has the
query-plan notes:

- One team and one season. Opponent figures cover only their games against
  Leicester.
- The 1,042-shot guard is tied to the current StatsBomb release. An upstream
  revision will make imports fail until the expected count is updated.
- xG is StatsBomb's value as provided, penalties included. The model version is
  not recorded.
- Own goals are derived (official goals minus goals from shots), not stored as
  events.
- The team-in-match rule is enforced on shot writes only. It is not re-checked
  if a match's teams are changed afterwards.
- The schema file is not a migration tool. Older databases must be recreated.
- Local only: no authentication, no caching or pagination, not deployed.
- Tests use a synthetic fixture. The real snapshot is checked only by the
  loader's count guard.
- At 1,042 shots, PostgreSQL chooses a sequential scan over the covering index;
  the home/away query executed in about 1 ms.

Data: [StatsBomb open data](https://github.com/statsbomb/open-data), used under
its licence terms with attribution.
