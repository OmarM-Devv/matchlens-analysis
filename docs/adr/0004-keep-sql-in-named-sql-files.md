# ADR 0004: Keep the schema and queries in plain SQL files

| | |
|---|---|
| Date recorded | 28/09/2026 |
| Status | Accepted |
| Stakeholders consulted | None (personal project) |

## Context

The project leans on PostgreSQL features: generated columns, CHECK
constraints, a trigger, a covering index with `INCLUDE`, `FILTER` clauses and
window functions with `ROWS` frames. The queries also need to be read,
reviewed and profiled with `EXPLAIN ANALYZE` on their own.

## Options considered

1. **SQLAlchemy ORM models and ORM queries.** Type-checked access from Python,
   but several of the features above need raw SQL anyway, and the generated
   SQL is harder to review and profile.
2. **SQL strings inline in Python.** Nothing extra to load, but the SQL is
   scattered through the application code.
3. **A library such as aiosql.** Loads named queries from files, but adds a
   dependency for something a few lines of code can do.
4. **Plain `.sql` files, with each query under a `-- name:` header, loaded by
   name at startup.**

## Decision

Option 4. [`sql/schema.sql`](../../sql/schema.sql) holds the schema. The
loader applies it, and running it again changes nothing.
[`sql/queries.sql`](../../sql/queries.sql) holds the named queries.
`load_queries()` in [`app/main.py`](../../app/main.py) splits the file on
`-- name:` lines, and each query runs as a SQLAlchemy `text()` statement with
bound parameters.

## Consequences

- Every query can be read on its own, and run or profiled in `psql` by
  substituting its parameters.
- The application code contains no SQL, and the SQL contains no Python.
- Parameters are always bound, never formatted into the string.
- Nothing checks at build time that the Python code and the SQL columns
  agree. Mismatches surface when Pydantic validates a response, and the
  integration tests run every query.
- `schema.sql` is not a migration tool. It can create the schema but not
  change an existing one, so a database with the old layout must be recreated.
  A migration tool such as Alembic would be needed if the schema kept
  changing.

## Supporting links

- [Analytical Layer](../../README.md#analytical-layer) and
  [Data Model](../../README.md#data-model) in the README
