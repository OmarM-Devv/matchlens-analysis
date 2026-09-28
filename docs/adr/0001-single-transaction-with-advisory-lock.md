# ADR 0001: Load each snapshot in one transaction under an advisory lock

| | |
|---|---|
| Date recorded | 28/09/2026 |
| Status | Accepted |
| Stakeholders consulted | None (personal project) |

## Context

An import is not a single insert. It upserts teams and matches, deletes
matches that are no longer in the snapshot, deletes and rewrites every shot
for the snapshot's matches, then re-counts what is in the database. If any
step fails part-way, the database could be left with new matches but old
shots, or with no shots at all.

The loader can also be started twice at once, for example by two
`docker compose run --rm loader` commands. Two imports interleaving their
deletes and inserts could leave duplicates or missing rows.

## Options considered

1. **A transaction per table.** Simple, but a failure between tables leaves a
   partial import that the API would serve.
2. **One transaction at `SERIALIZABLE` isolation.** Correct, but a clash
   between concurrent imports fails one of them with a serialisation error, so
   the loader would need retry logic.
3. **One transaction with `LOCK TABLE` on the three tables.** Serialises
   imports, but locks every season, not just the one being imported.
4. **One transaction holding `pg_advisory_xact_lock(competition_id, season_id)`.**

## Decision

Option 4. `load_dataset()` in [`loader/cleaner.py`](../../loader/cleaner.py)
opens one transaction with `engine.begin()` and takes an advisory lock keyed
on the competition and season before writing anything. Applying
`sql/schema.sql` takes a separate advisory lock (`SCHEMA_LOCK_KEY`) for the
same reason.

## Consequences

- An import either commits completely or not at all. A constraint failure
  part-way leaves the previous import exactly as it was.
- The API never sees a half-finished import, because other sessions only see
  committed data.
- A second import for the same season waits for the first to finish instead
  of failing. Imports for different seasons do not block each other.
- The lock is released automatically on commit or rollback, so a crashed
  loader cannot leave a stale lock behind.
- Advisory locks only coordinate code that takes them. A manual `INSERT` in
  `psql` is not serialised with an import.
- The whole import holds one transaction open, so a much larger load would
  hold its locks for longer and might need to be split into batches.

## Supporting links

- `test_database_error_rolls_back_the_entire_import` and
  `test_concurrent_imports_serialise_without_duplicates` in
  [`tests/test_pipeline.py`](../../tests/test_pipeline.py)
- [Ingestion](../../README.md#ingestion) in the README
