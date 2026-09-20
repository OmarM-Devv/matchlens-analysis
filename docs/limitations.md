# Limitations

MatchLens describes one team's season. It does not predict, rank or model
anything. This page lists what the data, schema, queries and running stack do
not cover, and the query-plan observations behind the performance notes.

## Scope of the data

- **One team, one season.** The loader keeps Leicester City's 38 Premier League
  2015/16 matches (StatsBomb competition 2, season 27, team 22). Opponents
  appear only in their matches against Leicester, so any opponent figure covers
  one or two games, not their season.
- **Counts are pinned to the current upstream snapshot.** The loader refuses a
  snapshot unless it has exactly 38 matches and 1,042 shot events, counting
  any shots set aside for missing xG (see below). 1,042 is the
  number of shot events in StatsBomb's open data for those 38 matches, counted on
  28 September 2026. StatsBomb revises open data from time to time. If a
  revision changes the count, imports fail until `MATCHLENS_EXPECTED_SHOTS` is
  updated. That is intended: the guard exists to catch silent upstream changes.
- **Snapshot scope.** Teams are upserted but never deleted. A team that
  disappears from a later snapshot keeps its `teams` row, with no matches
  attached.

## Metric definitions and their edges

- **xG is StatsBomb's value, not ours.** `shots.statsbomb_xg` is stored as
  provided. The provider's model version is not recorded, and a later data
  revision may change historical values.
- **Shots without xG are set aside.** A shot event with no `statsbomb_xg` is
  logged and not loaded, so it is missing from shots, shots on target, goals
  from shots and xG in every query. A goal set aside this way would appear as a
  "goal not from shots" in the match report. None of the 1,042 current shots
  lacks xG.
- **Penalties are included** in shots and xG. There is no non-penalty xG split.
- **Official goals vs shot goals.** Goals, results and points come from the
  official score in the match record. StatsBomb records own goals as separate
  `Own Goal For` / `Own Goal Against` events, not as shots, so "goals from
  shots" can be lower than official goals. The match report shows the
  difference as "goals not from shots". Own-goal events are not stored. The
  difference is derived, and in league data the only source of such goals is an
  own goal (there are no shoot-outs). The 2015/16 Leicester data has exactly one:
  Leicester City 2–2 West Bromwich Albion, 1 March 2016.
- **Shots on target** means outcome `Goal`, `Saved` or `Saved to Post`.
  `Saved Off Target`, `Post`, `Blocked`, `Off T` and `Wayward` count as off
  target.
- **Rolling windows** use `ROWS` frames ordered by match date, then match id.
  The first N−1 rows of an N-match window are partial; `games_in_window` shows
  how many matches each row covers.

## Schema

- The rule that a shot belongs to one of the two teams in its match is enforced
  by a trigger (`shots_team_in_match`) on shot insert and update. It is not
  re-checked if a match's home or away team is changed afterwards. The loader
  never does that, but a manual `UPDATE matches` could.
- `sql/schema.sql` is idempotent but is not a migration tool. Databases created
  by the pre-refactor layout (`team_match_views` table) are refused. Recreate the
  volume (`docker compose down -v`) and import again.

## Running stack

- Local only. The API and database ports are bound to `127.0.0.1`, there is no
  authentication, and nothing is deployed. GitHub Actions runs the tests and
  nothing else.
- The web app reads through a `SELECT`-only role with a 5-second statement
  timeout, and connects with a 3-second connect timeout.
- There is no caching or pagination. Every page load runs its queries, which is
  fine at this data size (see below) and would need revisiting for more data.
- The test suite runs on a small synthetic fixture (6 matches, 34 shot events,
  one without xG). The
  real 38-match snapshot is checked by the loader's count guard at import time,
  not by the tests.

## Query-plan notes

Measured with `EXPLAIN (ANALYZE)` on PostgreSQL 16.4 after loading the real
snapshot (38 matches, 1,042 shots) and running `VACUUM ANALYZE`:

- **The team filter reaches both halves of the view.** `team_match_view` is a
  `UNION ALL` of the home and away sides. `WHERE team_id = 22` is pushed into
  each branch and served by `ix_matches_home_team` and `ix_matches_away_team`
  (19 rows each).
- **Shots are aggregated once, before any join.** Each query groups `shots` by
  `(match_id, team_id)` in a CTE (76 rows for 38 matches) and then joins it to
  the 38 team-match rows, once for the team and once for the opponent. Joining
  raw shots first would repeat each match's goals and points once per shot. The
  test `test_home_away_aggregates_shots_before_joining` pins the correct totals.
- **Sequential scan at this size.** With statistics in place, the planner reads
  all 1,042 shots with a sequential scan and a `HashAggregate`. The home/away
  query executes in about 1 ms. Before `ANALYZE` it used a bitmap scan on the
  covering index `ix_shots_match_team` instead. The index is kept for larger
  volumes, but no benefit from it has been measured at this size.
- The `team_matches` CTE is referenced twice in `home_away` and `rolling_form`,
  so PostgreSQL materialises it. At 38 rows that costs nothing measurable.
