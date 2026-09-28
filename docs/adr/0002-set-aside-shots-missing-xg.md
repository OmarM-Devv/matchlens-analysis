# ADR 0002: Set aside shot events with no xG instead of rejecting the snapshot

| | |
|---|---|
| Date recorded | 28/09/2026 |
| Status | Accepted |
| Stakeholders consulted | None (personal project) |

## Context

`shots.statsbomb_xg` is `NOT NULL` with a CHECK that it lies between 0 and 1,
because every analytical query sums it. StatsBomb provides xG per shot, but
nothing guarantees that a future release will have it on every shot event.
Without special handling, one shot with a missing or null `statsbomb_xg`
fails parsing, and the whole snapshot is rejected.

The current data has xG on all 1,042 shots, so this is a guard against a
future upstream change, not a fix for a current problem.

## Options considered

1. **Reject the snapshot.** Safe, but one missing value in a
   provider-derived field blocks every other shot and match from loading.
2. **Make the column nullable and store the shot.** Keeps the event, but
   every query then has to decide how to treat a null, and "xG per shot"
   quietly changes meaning.
3. **Store 0 in place of the missing value.** Always wrong: it understates
   xG and is indistinguishable from a real near-zero chance.
4. **Set the shot aside, log it, and keep counting it.**

## Decision

Option 4. `extract_shots()` in [`loader/cleaner.py`](../../loader/cleaner.py)
splits each match's shot events into loadable shots and set-aside shots. A
shot is set aside only when `statsbomb_xg` is missing or null. Each one is
logged as a warning with its shot id, match, team and minute. Set-aside shots
still count towards the expected total of 1,042 shot events. Any other
malformed shot field still rejects the snapshot, because a broken id, team or
location points to corrupt data rather than a missing optional value.

## Consequences

- One missing xG value no longer blocks the rest of the snapshot.
- The count guard still checks how many shot events StatsBomb published, so a
  shot deleted upstream is still caught.
- The warning and the loader's summary line (`N set aside without xG`) make
  every set-aside shot visible.
- A set-aside shot is missing from shot counts, shots on target, goals from
  shots and xG totals. A set-aside goal would appear as a "goal not from
  shots" in the match report. This is documented in
  [`docs/limitations.md`](../limitations.md).
- Only xG gets this treatment. Extending it to other fields would need its own
  decision.

## Supporting links

- `test_first_import_loads_only_the_focus_teams_matches` and
  `test_count_mismatch_is_rejected_before_writing` in
  [`tests/test_pipeline.py`](../../tests/test_pipeline.py); the fixture in
  [`tests/conftest.py`](../../tests/conftest.py) contains one shot without xG
- [Figure 4](../../README.md#figure-4--missing-xg-fallback) in the README
