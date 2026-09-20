"""Transactional, idempotent loader for one team's season from StatsBomb open data.

Default scope: Leicester City (team 22) in the Premier League 2015/16
(competition 2, season 27): 38 matches and 1,042 shot events.

Expected layout under ``data_dir`` (mirrors github.com/statsbomb/open-data/data;
``scripts/fetch_statsbomb.py`` writes it):

    matches/<competition_id>/<season_id>.json   every match of the season
    events/<match_id>.json                      one file per match of the focus team

The loader keeps only the matches the focus team played. Each import treats
that set as a snapshot and runs in exactly one PostgreSQL transaction:

1. take a transaction-scoped advisory lock for the competition-season, so
   concurrent imports serialise instead of racing;
2. upsert ``teams`` and ``matches`` (``INSERT ... ON CONFLICT DO UPDATE``);
3. delete the focus team's matches in this competition-season that the
   snapshot no longer contains (their shots cascade away);
4. replace every ``shots`` row for the snapshot's matches;
5. re-count what is now in the database and compare it to the snapshot.

Any failure rolls the whole import back, leaving the previous successful
import untouched. Snapshots that fail validation (including the exact 38-match
and 1,042-shot counts) are rejected before a transaction is opened.

Shot events without a ``statsbomb_xg`` value are set aside and logged instead
of rejecting the snapshot. They are not written to ``shots``, but they still
count towards the expected shot total, so the 1,042 guard keeps checking the
number of shot events StatsBomb published.

CLI:  python -m loader.cleaner --data-dir data/statsbomb
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger("matchlens.loader")

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"

# Premier League 2015/16, Leicester City, in the StatsBomb open-data catalogue.
DEFAULT_COMPETITION_ID = 2
DEFAULT_SEASON_ID = 27
DEFAULT_TEAM_ID = 22
DEFAULT_EXPECTED_MATCHES = 38
DEFAULT_EXPECTED_SHOTS = 1042

MAX_REPORTED_ERRORS = 20
CONNECT_TIMEOUT_SECONDS = 10
# Arbitrary constant distinguishing the schema lock from per-season import locks.
SCHEMA_LOCK_KEY = 0x4D4C5343


class LoaderError(Exception):
    """Base class for loader failures."""


class DataValidationError(LoaderError):
    """The source data is malformed or inconsistent; nothing was written."""


class LoadError(LoaderError):
    """The database rejected the import; the transaction was rolled back."""


@dataclass(frozen=True)
class Dataset:
    competition_id: int
    season_id: int
    team_id: int
    teams: list[dict[str, Any]]
    matches: list[dict[str, Any]]
    shots: list[dict[str, Any]]
    # Shot events without statsbomb_xg: not loaded, but counted and logged.
    skipped_shots: list[dict[str, Any]] = field(default_factory=list)

    @property
    def match_ids(self) -> list[int]:
        return [m["match_id"] for m in self.matches]

    @property
    def shot_events(self) -> int:
        """Every shot event in the snapshot, loaded or set aside."""
        return len(self.shots) + len(self.skipped_shots)


@dataclass(frozen=True)
class LoadReport:
    competition_id: int
    season_id: int
    team_id: int
    matches_inserted: int
    matches_updated: int
    matches_pruned: int
    shots_written: int
    shots_replaced: int
    shots_skipped: int = 0


# --------------------------------------------------------------------------- engine and schema


def create_db_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Engine from ``url`` or $DATABASE_URL (psycopg 3 driver:
    ``postgresql+psycopg://user:pass@host:5432/matchlens``)."""
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("No database URL supplied and $DATABASE_URL is not set")
    kwargs.setdefault("pool_pre_ping", True)
    # Fail within seconds when the database is down instead of psycopg's ~130 s default.
    kwargs.setdefault("connect_args", {"connect_timeout": CONNECT_TIMEOUT_SECONDS})
    return create_engine(url, **kwargs)


def apply_schema(engine: Engine) -> None:
    """Apply sql/schema.sql. Idempotent; refuses the pre-refactor layout."""
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
        legacy = conn.execute(text("SELECT to_regclass('team_match_views')")).scalar_one()
        if legacy is not None:
            raise LoadError(
                "database has the pre-refactor layout (table team_match_views);"
                " recreate it, e.g. `docker compose down -v`, then import again"
            )
        # Raw cursor with no parameters, so the '%' in RAISE formats is not a placeholder.
        conn.connection.cursor().execute(ddl)


# --------------------------------------------------------------------------- parsing


def _parse_time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def parse_match(raw: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return {
            "match_id": int(raw["match_id"]),
            "competition_id": int(raw["competition"]["competition_id"]),
            "season_id": int(raw["season"]["season_id"]),
            "season_name": str(raw["season"]["season_name"]),
            "match_date": date.fromisoformat(raw["match_date"]),
            "kick_off": _parse_time(raw.get("kick_off")),
            "match_week": raw.get("match_week"),
            "home_team_id": int(raw["home_team"]["home_team_id"]),
            "home_team_name": str(raw["home_team"]["home_team_name"]),
            "away_team_id": int(raw["away_team"]["away_team_id"]),
            "away_team_name": str(raw["away_team"]["away_team_name"]),
            "home_score": int(raw["home_score"]),
            "away_score": int(raw["away_score"]),
            "stadium_name": (raw.get("stadium") or {}).get("name"),
            "referee_name": (raw.get("referee") or {}).get("name"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise DataValidationError(
            f"match {raw.get('match_id', '?')}: malformed match record ({exc!r})"
        ) from exc


def parse_shot(raw: Mapping[str, Any], match_id: int) -> dict[str, Any]:
    try:
        shot = raw["shot"]
        location = raw["location"]
        end_location = shot.get("end_location") or []
        return {
            "shot_id": uuid.UUID(raw["id"]),
            "match_id": match_id,
            "team_id": int(raw["team"]["id"]),
            "player_id": int(raw["player"]["id"]),
            "player_name": str(raw["player"]["name"]),
            "event_index": int(raw["index"]),
            "period": int(raw["period"]),
            "minute": int(raw["minute"]),
            "second": int(raw["second"]),
            "location_x": float(location[0]),
            "location_y": float(location[1]),
            "end_location_x": float(end_location[0]) if len(end_location) > 0 else None,
            "end_location_y": float(end_location[1]) if len(end_location) > 1 else None,
            "statsbomb_xg": float(shot["statsbomb_xg"]),
            "outcome": str(shot["outcome"]["name"]),
            "body_part": str(shot["body_part"]["name"]),
            "technique": str(shot["technique"]["name"]),
            "shot_type": str(shot["type"]["name"]),
            "play_pattern": str(raw["play_pattern"]["name"]),
            "under_pressure": bool(raw.get("under_pressure", False)),
            "first_time": bool(shot.get("first_time", False)),
        }
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise DataValidationError(
            f"match {match_id}: malformed shot event {raw.get('id', '?')} ({exc!r})"
        ) from exc


def extract_shots(
    events: Iterable[Mapping[str, Any]], match_id: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a match's shot events into loadable shots and shots set aside.

    A shot is set aside only when ``statsbomb_xg`` is missing or null. Any
    other malformed field still rejects the snapshot in ``parse_shot``.
    """
    shots: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for event in events:
        if (event.get("type") or {}).get("name") != "Shot":
            continue
        if (event.get("shot") or {}).get("statsbomb_xg") is None:
            skipped.append(
                {
                    "shot_id": event.get("id", "?"),
                    "match_id": match_id,
                    "team_id": (event.get("team") or {}).get("id"),
                    "minute": event.get("minute"),
                    "reason": "missing statsbomb_xg",
                }
            )
            continue
        shots.append(parse_shot(event, match_id))
    return shots, skipped


def involves_team(raw: Mapping[str, Any], team_id: int) -> bool:
    home = (raw.get("home_team") or {}).get("home_team_id")
    away = (raw.get("away_team") or {}).get("away_team_id")
    return team_id in (home, away)


def build_dataset(
    competition_id: int,
    season_id: int,
    team_id: int,
    raw_matches: Sequence[Mapping[str, Any]],
    events_by_match: Mapping[int, Sequence[Mapping[str, Any]]],
) -> Dataset:
    """Parse the focus team's matches and their shots. Team names are taken
    from the match records; a team id seen with two names is rejected."""
    team_names: dict[int, set[str]] = {}
    matches, shots, skipped = [], [], []
    for raw in raw_matches:
        if not involves_team(raw, team_id):
            continue
        match = parse_match(raw)
        if match["match_id"] not in events_by_match:
            raise DataValidationError(f"match {match['match_id']}: no events file")
        for side in ("home", "away"):
            team_names.setdefault(match[f"{side}_team_id"], set()).add(match[f"{side}_team_name"])
        matches.append(match)
        match_shots, match_skipped = extract_shots(events_by_match[match["match_id"]], match["match_id"])
        shots.extend(match_shots)
        skipped.extend(match_skipped)

    conflicting = {tid: sorted(names) for tid, names in team_names.items() if len(names) > 1}
    if conflicting:
        raise DataValidationError(f"team ids with more than one name: {conflicting}")
    teams = [{"team_id": tid, "team_name": next(iter(names))} for tid, names in sorted(team_names.items())]
    return Dataset(competition_id, season_id, team_id, teams, matches, shots, skipped)


def read_statsbomb(data_dir: Path | str, competition_id: int, season_id: int, team_id: int) -> Dataset:
    root = Path(data_dir)
    matches_file = root / "matches" / str(competition_id) / f"{season_id}.json"
    raw_matches = _read_json(matches_file)
    if not isinstance(raw_matches, list):
        raise DataValidationError(f"{matches_file}: expected a JSON array of matches")

    events_by_match: dict[int, Sequence[Mapping[str, Any]]] = {}
    for raw in raw_matches:
        if not isinstance(raw, dict) or not involves_team(raw, team_id):
            continue
        match_id = raw.get("match_id")
        if not isinstance(match_id, int):
            continue  # parse_match reports the malformed record
        events_file = root / "events" / f"{match_id}.json"
        if events_file.exists():
            events_by_match[match_id] = _read_json(events_file)
    return build_dataset(competition_id, season_id, team_id, raw_matches, events_by_match)


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise DataValidationError(f"{path}: file not found") from exc
    except json.JSONDecodeError as exc:
        raise DataValidationError(f"{path}: invalid JSON ({exc})") from exc


# --------------------------------------------------------------------------- validation


def validate_dataset(
    dataset: Dataset,
    *,
    expected_matches: int | None = None,
    expected_shots: int | None = None,
) -> None:
    """Reject inconsistent snapshots before a transaction is opened."""
    errors: list[str] = []

    if not dataset.matches:
        errors.append(f"snapshot contains no matches for team {dataset.team_id}")
    if expected_matches is not None and len(dataset.matches) != expected_matches:
        errors.append(f"expected {expected_matches} matches, found {len(dataset.matches)}")
    # Shots set aside for missing xG still count: the guard checks how many shot
    # events the source published, not how many had an xG value.
    if expected_shots is not None and dataset.shot_events != expected_shots:
        errors.append(
            f"expected {expected_shots} shots, found {dataset.shot_events}"
            f" ({len(dataset.shots)} with xG, {len(dataset.skipped_shots)} set aside without xG)"
        )

    for match_id, n in Counter(dataset.match_ids).items():
        if n > 1:
            errors.append(f"match {match_id} appears {n} times")
    for shot_id, n in Counter(s["shot_id"] for s in dataset.shots).items():
        if n > 1:
            errors.append(f"shot {shot_id} appears {n} times")

    teams_by_match: dict[int, set[int]] = {}
    for m in dataset.matches:
        if (m["competition_id"], m["season_id"]) != (dataset.competition_id, dataset.season_id):
            errors.append(
                f"match {m['match_id']} belongs to competition {m['competition_id']}"
                f" season {m['season_id']}, not {dataset.competition_id}/{dataset.season_id}"
            )
        teams_by_match[m["match_id"]] = {m["home_team_id"], m["away_team_id"]}
    for s in dataset.shots:
        if s["team_id"] not in teams_by_match.get(s["match_id"], set()):
            errors.append(f"shot {s['shot_id']} credited to team {s['team_id']}, which is not in match {s['match_id']}")

    if errors:
        shown = errors[:MAX_REPORTED_ERRORS]
        more = f"\n  ... and {len(errors) - len(shown)} more" if len(errors) > len(shown) else ""
        raise DataValidationError("snapshot rejected:\n  " + "\n  ".join(shown) + more)


# --------------------------------------------------------------------------- persistence

_MATCH_COLUMNS = [
    "match_id", "competition_id", "season_id", "season_name", "match_date", "kick_off",
    "match_week", "home_team_id", "away_team_id", "home_score", "away_score",
    "stadium_name", "referee_name",
]
_SHOT_COLUMNS = [
    "shot_id", "match_id", "team_id", "player_id", "player_name", "event_index", "period",
    "minute", "second", "location_x", "location_y", "end_location_x", "end_location_y",
    "statsbomb_xg", "outcome", "body_part", "technique", "shot_type", "play_pattern",
    "under_pressure", "first_time",
]


def _insert_sql(table: str, columns: Sequence[str], suffix: str = "") -> Any:
    cols = ", ".join(columns)
    binds = ", ".join(f":{c}" for c in columns)
    return text(f"INSERT INTO {table} ({cols}) VALUES ({binds}) {suffix}")


UPSERT_TEAM = _insert_sql(
    "teams", ["team_id", "team_name"],
    "ON CONFLICT (team_id) DO UPDATE SET team_name = EXCLUDED.team_name",
)
UPSERT_MATCH = _insert_sql(
    "matches", _MATCH_COLUMNS,
    "ON CONFLICT (match_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _MATCH_COLUMNS if c != "match_id")
    + ", loaded_at = now()",
)
INSERT_SHOT = _insert_sql("shots", _SHOT_COLUMNS)


def load_dataset(engine: Engine, dataset: Dataset) -> LoadReport:
    """Write ``dataset`` in a single transaction; roll back everything on any error."""
    try:
        with engine.begin() as conn:
            return _load_in_transaction(conn, dataset)
    except LoaderError:
        raise
    except SQLAlchemyError as exc:
        raise LoadError(
            f"import of competition {dataset.competition_id} season {dataset.season_id}"
            f" rolled back: {getattr(exc, 'orig', exc)}"
        ) from exc


def _load_in_transaction(conn: Connection, ds: Dataset) -> LoadReport:
    conn.execute(
        text("SELECT pg_advisory_xact_lock(:competition_id, :season_id)"),
        {"competition_id": ds.competition_id, "season_id": ds.season_id},
    )
    match_ids = ds.match_ids
    scope = {"competition_id": ds.competition_id, "season_id": ds.season_id, "team_id": ds.team_id}

    existing = conn.execute(
        text("SELECT COUNT(*) FROM matches WHERE match_id = ANY(:ids)"), {"ids": match_ids}
    ).scalar_one()

    conn.execute(UPSERT_TEAM, ds.teams)
    conn.execute(UPSERT_MATCH, ds.matches)

    pruned = conn.execute(
        text(
            """
            DELETE FROM matches
            WHERE competition_id = :competition_id AND season_id = :season_id
              AND :team_id IN (home_team_id, away_team_id)
              AND match_id <> ALL(:ids)
            """
        ),
        {**scope, "ids": match_ids},
    ).rowcount

    # Shots are replaced wholesale so events removed upstream disappear too.
    shots_replaced = conn.execute(
        text("DELETE FROM shots WHERE match_id = ANY(:ids)"), {"ids": match_ids}
    ).rowcount
    if ds.shots:
        conn.execute(INSERT_SHOT, ds.shots)

    _verify_counts(conn, ds)

    report = LoadReport(
        competition_id=ds.competition_id,
        season_id=ds.season_id,
        team_id=ds.team_id,
        matches_inserted=len(match_ids) - existing,
        matches_updated=existing,
        matches_pruned=pruned,
        shots_written=len(ds.shots),
        shots_replaced=shots_replaced,
        shots_skipped=len(ds.skipped_shots),
    )
    log.info("import committed: %s", report)
    return report


def _verify_counts(conn: Connection, ds: Dataset) -> None:
    n_matches, n_shots = conn.execute(
        text(
            """
            SELECT
                (SELECT COUNT(*) FROM matches AS m
                 WHERE m.competition_id = :competition_id AND m.season_id = :season_id
                   AND :team_id IN (m.home_team_id, m.away_team_id)),
                (SELECT COUNT(*) FROM shots AS s JOIN matches AS m USING (match_id)
                 WHERE m.competition_id = :competition_id AND m.season_id = :season_id
                   AND :team_id IN (m.home_team_id, m.away_team_id))
            """
        ),
        {"competition_id": ds.competition_id, "season_id": ds.season_id, "team_id": ds.team_id},
    ).one()
    expected = (len(ds.matches), len(ds.shots))
    if (n_matches, n_shots) != expected:
        raise LoadError(
            f"post-load verification failed: database has matches/shots ="
            f" {(n_matches, n_shots)}, snapshot has {expected}"
        )


def import_statsbomb(
    engine: Engine,
    data_dir: Path | str,
    competition_id: int = DEFAULT_COMPETITION_ID,
    season_id: int = DEFAULT_SEASON_ID,
    team_id: int = DEFAULT_TEAM_ID,
    *,
    expected_matches: int | None = None,
    expected_shots: int | None = None,
) -> LoadReport:
    """Read, validate and load one team's competition-season snapshot."""
    dataset = read_statsbomb(data_dir, competition_id, season_id, team_id)
    validate_dataset(dataset, expected_matches=expected_matches, expected_shots=expected_shots)
    for shot in dataset.skipped_shots:
        log.warning(
            "shot %s (match %s, team %s, minute %s) set aside: %s",
            shot["shot_id"], shot["match_id"], shot["team_id"], shot["minute"], shot["reason"],
        )
    return load_dataset(engine, dataset)


# --------------------------------------------------------------------------- CLI


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load one team's StatsBomb season into MatchLens.")
    parser.add_argument("--data-dir", default=os.environ.get("MATCHLENS_DATA_DIR", "data/statsbomb"))
    parser.add_argument("--competition-id", type=int, default=_env_int("MATCHLENS_COMPETITION_ID", DEFAULT_COMPETITION_ID))
    parser.add_argument("--season-id", type=int, default=_env_int("MATCHLENS_SEASON_ID", DEFAULT_SEASON_ID))
    parser.add_argument("--team-id", type=int, default=_env_int("MATCHLENS_TEAM_ID", DEFAULT_TEAM_ID))
    parser.add_argument("--expected-matches", type=int, default=_env_int("MATCHLENS_EXPECTED_MATCHES", DEFAULT_EXPECTED_MATCHES))
    parser.add_argument("--expected-shots", type=int, default=_env_int("MATCHLENS_EXPECTED_SHOTS", DEFAULT_EXPECTED_SHOTS))
    parser.add_argument("--no-count-check", action="store_true", help="skip the expected match/shot count check")
    parser.add_argument("--database-url", default=None, help="defaults to $DATABASE_URL")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    engine = create_db_engine(args.database_url)
    try:
        apply_schema(engine)
        report = import_statsbomb(
            engine,
            args.data_dir,
            args.competition_id,
            args.season_id,
            args.team_id,
            expected_matches=None if args.no_count_check else args.expected_matches,
            expected_shots=None if args.no_count_check else args.expected_shots,
        )
    except LoaderError as exc:
        log.error("%s", exc)
        return 1
    except SQLAlchemyError as exc:
        log.error("database error: %s", getattr(exc, "orig", exc))
        return 1
    finally:
        engine.dispose()

    print(
        f"Loaded team {report.team_id}, competition {report.competition_id} season {report.season_id}: "
        f"{report.matches_inserted} matches inserted, {report.matches_updated} updated, "
        f"{report.matches_pruned} pruned; {report.shots_written} shots ({report.shots_replaced} replaced, "
        f"{report.shots_skipped} set aside without xG)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
