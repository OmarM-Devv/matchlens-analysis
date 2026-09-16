# Failure exercise: controlled PostgreSQL outage

The exercise stops the database while the web app is serving requests, records
how the failure shows up, restores the database, and confirms recovery without
restarting the app. It also runs the loader during the outage.

## Recorded runs

All runs were on 28 September 2026 on Windows 11, with the real snapshot loaded
(38 matches, 1,042 shots). The first two used PostgreSQL 16.4 from portable
binaries (started with `pg_ctl` on port 55432) and the app under `uvicorn` on
port 8765, while Docker Desktop was broken. The third used the compose stack
(Docker Desktop 4.93.0, `postgres:16-alpine`), following the runbook below.

### First run: finding a 130-second hang

| Step | Observation |
|---|---|
| Baseline | `GET /health` → 200 `{"status":"ok"}` |
| Stop PostgreSQL (`pg_ctl stop -m fast`) | server stopped |
| `GET /health` during outage | 503 `{"detail":"database unavailable"}` after **130.06 s** |
| App log | `database error on /health: connection timeout expired` |

The 503 was correct, but each request held a worker for more than two minutes.
Two things combined. `pool_pre_ping` found the pooled connection dead and
discarded it. The replacement connection attempt then had no `connect_timeout`,
so psycopg waited 130 seconds, its default connection deadline, before giving
up. With two uvicorn workers, a few concurrent requests during an outage would
leave the app unresponsive even after the database came back.

**Fix:** the app's engine now sets `connect_timeout=3` (`CONNECT_TIMEOUT_SECONDS`
in `app/main.py`). The loader sets `connect_timeout=10`
(`CONNECT_TIMEOUT_SECONDS` in `loader/cleaner.py`).

### Second run, with the fix

| Step | Observation |
|---|---|
| Baseline | `GET /health` → 200 in 0.01 s |
| Stop PostgreSQL | server stopped |
| `GET /health` | 503 `{"detail":"database unavailable"}` in 3.04 s |
| `GET /` | 503 HTML error page in 3.05 s |
| `GET /matches/3754290` | 503 HTML error page in 3.01 s |
| `GET /api/home-away` | 503 JSON in 3.03 s |
| App log, one line per request | `database error on <path>: connection timeout expired` |
| `python -m loader.cleaner` during outage | `ERROR matchlens.loader database error: connection timeout expired`, exit code 1 after 12 s |
| Start PostgreSQL (`pg_ctl start`) | server started |
| `GET /health`, app **not** restarted | 200 in 0.12 s |
| `GET /matches/3754290` | 200 |
| `GET /api/home-away` | 200, Home 19 played, 12 W, 6 D, 1 L |
| Row counts | 38 matches, 1,042 shots, unchanged by the failed import |
| `python -m loader.cleaner` after restore | 0 inserted, 38 updated, 1,042 shots replaced; exit code 0 |

The app recovered on its own: `pool_pre_ping` discards the dead connections and
opens new ones on the next request. The failed import wrote nothing, because it
never got a connection and every import is a single transaction.

### Third run: compose stack

| Step | Observation |
|---|---|
| Baseline | `GET /health` → 200 in 0.004 s |
| `docker compose stop db` | `db: Exited (0)` |
| `GET /health` | 503 `{"detail":"database unavailable"}` in 4.02 s |
| `GET /` | 503 HTML error page in 3.98 s |
| `GET /matches/3754290` | 503 HTML error page in 3.98 s |
| `GET /api/home-away` | 503 JSON in 4.00 s |
| `docker compose logs api`, one line per request | `database error on <path>: [Errno -2] Name or service not known` |
| `docker compose run --rm --no-deps loader` during outage | `ERROR matchlens.loader database error: [Errno -2] Name or service not known`, exit code 1 after 6 s |
| `docker compose start db` | `db: Up 6 seconds (healthy)` |
| `GET /health`, `api` **not** restarted | 200 in 0.01 s |
| `GET /matches/3754290` | 200 |
| Row counts | 38 matches, 1,042 shots |

The failure looks different under compose. A stopped container leaves the
compose network, so the hostname `db` stops resolving. The app gets a DNS
error, after about 4 seconds spent in Docker's resolver, instead of a connect
timeout. The outcome is the same: fast 503s, no writes, and recovery without
restarting `api`.

The same behaviour is covered in CI by `test_database_outage_returns_503`, which
points the app at an unreachable database and expects 503 from `/health` and
from `/`.

## Runbook (compose stack)

### 1. Confirm the healthy state

```bash
docker compose ps
curl -s http://127.0.0.1:8000/health
```

Expect `db` healthy, `api` running, and `{"status":"ok"}`.

### 2. Cause the outage

```bash
docker compose stop db
```

### 3. Diagnose

The symptoms, in the order you are likely to meet them:

```bash
curl -s -w ' [%{http_code}] %{time_total}s\n' http://127.0.0.1:8000/health
```

Expect `{"detail":"database unavailable"} [503]` within about 4 seconds. Pages
return a 503 HTML page saying "The database is unavailable."

```bash
docker compose logs --tail 20 api
```

Look for `database error on <path>: ...`. The text after the path points at
the cause. The first two rows were seen in the recorded runs; the others are
typical PostgreSQL client messages for other causes:

| Message | Meaning |
|---|---|
| `[Errno -2] Name or service not known` | The `db` container is stopped, so its hostname no longer resolves (seen in the compose run) |
| `connection timeout expired` | Nothing answered within 3 s: host down or network partition (seen in the local runs) |
| `Connection refused` | The host is reachable but PostgreSQL is not listening, for example while it restarts |
| `password authentication failed for user "matchlens_ro"` | Credentials changed; the init script only runs on a new volume |
| `canceling statement due to statement timeout` | Database is up but a query ran past the read-only role's 5 s limit |

```bash
docker compose ps db
docker compose logs --tail 50 db
```

`docker compose ps` shows `exited` for a stopped container. For a crash, the
database log shows the reason, such as a full disk or bad configuration.

### 4. Restore

```bash
docker compose start db
docker compose ps db
```

Wait until `db` reports `healthy`, then check the app. It should recover without
a restart:

```bash
curl -s http://127.0.0.1:8000/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
```

Expect `{"status":"ok"}` and `200`.

### 5. Verify the data

The app never writes, so an outage cannot damage data through it. An import
interrupted by the outage rolls back as a single transaction. To confirm:

```bash
docker compose exec db psql -U matchlens -d matchlens -c "SELECT (SELECT count(*) FROM matches) AS matches, (SELECT count(*) FROM shots) AS shots"
```

Expect 38 matches and 1,042 shots. If an import was running when the database
stopped, run it again. Imports are idempotent:

```bash
docker compose run --rm loader
```

## What the exercise does not cover

- A long outage under real traffic. Each request still holds a worker for up to
  3 seconds, so heavy traffic during an outage would still queue.
- Data loss or corruption inside PostgreSQL. Recovering from that needs backups,
  and the project has none.
- Failover. There is a single database instance.
