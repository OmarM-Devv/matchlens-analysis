# ADR 0003: Aggregate shots per match and team before joining

| | |
|---|---|
| Date recorded | 28/09/2026 |
| Status | Accepted |
| Stakeholders consulted | None (personal project) |

## Context

`team_match_view` has two rows per match, one per side, carrying goals,
result and points from the official score. `shots` has one row per shot
event. The reports need both: points and goals from the view, and shots and
xG from `shots`.

Joining raw shots to the view and then summing repeats each match's goals and
points once per shot. A match where Leicester took 15 shots would count its
3 points 15 times. The query still runs and returns plausible-looking
numbers, which makes the mistake easy to miss.

## Options considered

1. **Join raw shots, then correct with `SUM(DISTINCT ...)` or division.**
   Fragile: `SUM(DISTINCT)` silently drops genuinely equal values, such as two
   matches worth 3 points each.
2. **A view or materialised view of per-match shot totals.** Removes
   repetition across queries. A materialised view would also need refreshing
   after every import.
3. **Aggregate shots in a CTE inside each query, then join.**

## Decision

Option 3. Each analytical query in [`sql/queries.sql`](../../sql/queries.sql)
first groups `shots` to one row per `(match_id, team_id)`, then left-joins
that to the view, once for the team and once for the opponent, with
`COALESCE(..., 0)` for a side with no shots.

## Consequences

- Goals and points are counted once per match. The home and away rows add up
  to the official 23 wins, 12 draws, 3 defeats and 81 points.
- `shots` is scanned once per query. `EXPLAIN ANALYZE` on the real data showed
  a sequential scan and a hash aggregate, with the home/away query taking
  about 1 ms.
- The aggregation CTE is repeated in `match_report`, `rolling_form` and
  `home_away`. If more queries need it, a shared view (option 2) would be the
  next step.

## Supporting links

- `test_home_away_aggregates_shots_before_joining` in
  [`tests/test_pipeline.py`](../../tests/test_pipeline.py)
- Query-plan notes in [`docs/limitations.md`](../limitations.md)
- [Figure 8](../../README.md#figure-8--home-vs-away-through-the-api) in the README
