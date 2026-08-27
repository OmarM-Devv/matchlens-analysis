"""Transactional, idempotent loader for StatsBomb open-data exports.

Expected layout under ``data_dir`` (mirrors github.com/statsbomb/open-data/data):

    matches/<competition_id>/<season_id>.json
    events/<match_id>.json

Each import treats the competition-season as a snapshot and runs in exactly one
PostgreSQL transaction:

1. take a transaction-scoped advisory lock for the competition-season, so
   concurrent imports of the same data serialise instead of racing;
2. upsert ``matches`` (``INSERT ... ON CONFLICT DO UPDATE``);
3. delete matches in the same competition-season that the snapshot no longer contains;
4. replace every ``team_match_views`` and ``shots`` row for the snapshot's matches;
5. re-count what is now in the database and compare it to the snapshot.

Any failure in any step rolls the whole import back, leaving the previous
successful import untouched. Repeat runs of the same snapshot converge on the
same rows; they never create duplicates.

CLI:  python -m db.loader --data-dir data/statsbomb
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
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from db.engine import create_db_engine, create_schema
from db.models import Match, Shot, TeamMatchView

log = logging.getLogger("matchlens.loader")

# Premier League 2003/04 in the StatsBomb open-data catalogue.
DEFAULT_COMPETITION_ID = 2
DEFAULT_SEASON_ID = 44
DEFAULT_EXPECTED_MATCHES = 38
DEFAULT_EXPECTED_SHOTS = 1086

ON_TARGET_OUTCOMES = frozenset({"Goal", "Saved", "Saved To Post"})
MAX_REPORTED_ERRORS = 20


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
    matches: list[dict[str, Any]]
    team_views: list[dict[str, Any]]
    shots: list[dict[str, Any]]

    @property
    def match_ids(self) -> list[int]:
        return [m["match_id"] for m in self.matches]


@dataclass(frozen=True)
class LoadReport:
    competition_id: int
    season_id: int
    matches_inserted: int
    matches_updated: int
    matches_pruned: int
    team_views_written: int
    shots_written: int
    shots_replaced: int

    @property
    def matches_total(self) -> int:
        return self.matches_inserted + self.matches_updated


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


def extract_shots(events: Iterable[Mapping[str, Any]], match_id: int) -> list[dict[str, Any]]:
    return [
        parse_shot(event, match_id)
        for event in events
        if (event.get("type") or {}).get("name") == "Shot"
    ]


def build_team_views(match: Mapping[str, Any], shots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Derive the home and away perspective rows for one match.

    Goals come from the official score (which includes own goals); shot and xG
    totals come from the shot events.
    """
    home = (match["home_team_id"], match["home_team_name"], match["home_score"])
    away = (match["away_team_id"], match["away_team_name"], match["away_score"])

    xg_by_team: Counter[int] = Counter()
    shots_by_team: Counter[int] = Counter()
    on_target_by_team: Counter[int] = Counter()
    for s in shots:
        xg_by_team[s["team_id"]] += s["statsbomb_xg"]
        shots_by_team[s["team_id"]] += 1
        on_target_by_team[s["team_id"]] += s["outcome"] in ON_TARGET_OUTCOMES

    views = []
    for (team_id, team_name, gf), (opp_id, opp_name, ga), is_home in ((home, away, True), (away, home, False)):
        if gf > ga:
            result, points = "W", 3
        elif gf == ga:
            result, points = "D", 1
        else:
            result, points = "L", 0
        views.append(
            {
                "match_id": match["match_id"],
                "team_id": team_id,
                "team_name": team_name,
                "opponent_id": opp_id,
                "opponent_name": opp_name,
                "is_home": is_home,
                "match_date": match["match_date"],
                "goals_for": gf,
                "goals_against": ga,
                "result": result,
                "points": points,
                "shots": shots_by_team[team_id],
                "shots_on_target": on_target_by_team[team_id],
                "xg": round(xg_by_team[team_id], 6),
                "xg_against": round(xg_by_team[opp_id], 6),
            }
        )
    return views


def build_dataset(
    competition_id: int,
    season_id: int,
    raw_matches: Sequence[Mapping[str, Any]],
    events_by_match: Mapping[int, Sequence[Mapping[str, Any]]],
) -> Dataset:
    matches, team_views, shots = [], [], []
    for raw in raw_matches:
        match = parse_match(raw)
        if match["match_id"] not in events_by_match:
            raise DataValidationError(f"match {match['match_id']}: no events file")
        match_shots = extract_shots(events_by_match[match["match_id"]], match["match_id"])
        matches.append(match)
        shots.extend(match_shots)
        team_views.extend(build_team_views(match, match_shots))
    return Dataset(competition_id, season_id, matches, team_views, shots)


def read_statsbomb(data_dir: Path | str, competition_id: int, season_id: int) -> Dataset:
    root = Path(data_dir)
    matches_file = root / "matches" / str(competition_id) / f"{season_id}.json"
    raw_matches = _read_json(matches_file)
    if not isinstance(raw_matches, list):
        raise DataValidationError(f"{matches_file}: expected a JSON array of matches")

    events_by_match: dict[int, Sequence[Mapping[str, Any]]] = {}
    for raw in raw_matches:
        match_id = raw.get("match_id") if isinstance(raw, dict) else None
        if not isinstance(match_id, int):
            continue  # parse_match reports the malformed record
        events_file = root / "events" / f"{match_id}.json"
        if events_file.exists():
            events_by_match[match_id] = _read_json(events_file)
    return build_dataset(competition_id, season_id, raw_matches, events_by_match)


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
        errors.append("snapshot contains no matches")
    if expected_matches is not None and len(dataset.matches) != expected_matches:
        errors.append(f"expected {expected_matches} matches, found {len(dataset.matches)}")
    if expected_shots is not None and len(dataset.shots) != expected_shots:
        errors.append(f"expected {expected_shots} shots, found {len(dataset.shots)}")

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

_MATCH_UPDATE_COLUMNS = [c.name for c in Match.__table__.columns if c.name not in ("match_id", "loaded_at")]


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
    matches = Match.__table__
    in_scope = (matches.c.competition_id == ds.competition_id) & (matches.c.season_id == ds.season_id)

    upsert = pg_insert(matches).values(ds.matches)
    upsert = upsert.on_conflict_do_update(
        index_elements=[matches.c.match_id],
        set_={**{col: upsert.excluded[col] for col in _MATCH_UPDATE_COLUMNS}, "loaded_at": func.now()},
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    inserted_flags = conn.execute(upsert).scalars().all()
    inserted = sum(1 for flag in inserted_flags if flag)

    pruned = conn.execute(
        delete(matches).where(in_scope, matches.c.match_id.not_in(match_ids))
    ).rowcount

    # Children are replaced wholesale so events removed upstream disappear too.
    shots_replaced = conn.execute(delete(Shot).where(Shot.match_id.in_(match_ids))).rowcount
    conn.execute(delete(TeamMatchView).where(TeamMatchView.match_id.in_(match_ids)))
    conn.execute(insert(TeamMatchView), ds.team_views)
    if ds.shots:
        conn.execute(insert(Shot), ds.shots)

    _verify_counts(conn, ds)

    report = LoadReport(
        competition_id=ds.competition_id,
        season_id=ds.season_id,
        matches_inserted=inserted,
        matches_updated=len(inserted_flags) - inserted,
        matches_pruned=pruned,
        team_views_written=len(ds.team_views),
        shots_written=len(ds.shots),
        shots_replaced=shots_replaced,
    )
    log.info("import committed: %s", report)
    return report


def _verify_counts(conn: Connection, ds: Dataset) -> None:
    scope = (Match.competition_id == ds.competition_id) & (Match.season_id == ds.season_id)
    n_matches = conn.execute(select(func.count()).select_from(Match).where(scope)).scalar_one()
    n_views = conn.execute(
        select(func.count()).select_from(TeamMatchView).join(Match).where(scope)
    ).scalar_one()
    n_shots = conn.execute(select(func.count()).select_from(Shot).join(Match).where(scope)).scalar_one()

    expected = (len(ds.matches), len(ds.team_views), len(ds.shots))
    if (n_matches, n_views, n_shots) != expected:
        raise LoadError(
            f"post-load verification failed: database has matches/views/shots ="
            f" {(n_matches, n_views, n_shots)}, snapshot has {expected}"
        )


def import_statsbomb(
    engine: Engine,
    data_dir: Path | str,
    competition_id: int = DEFAULT_COMPETITION_ID,
    season_id: int = DEFAULT_SEASON_ID,
    *,
    expected_matches: int | None = None,
    expected_shots: int | None = None,
) -> LoadReport:
    """Read, validate and load one competition-season snapshot."""
    dataset = read_statsbomb(data_dir, competition_id, season_id)
    validate_dataset(dataset, expected_matches=expected_matches, expected_shots=expected_shots)
    return load_dataset(engine, dataset)


# --------------------------------------------------------------------------- CLI


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load StatsBomb open data into MatchLens.")
    parser.add_argument("--data-dir", default=os.environ.get("MATCHLENS_DATA_DIR", "data/statsbomb"))
    parser.add_argument("--competition-id", type=int, default=_env_int("MATCHLENS_COMPETITION_ID", DEFAULT_COMPETITION_ID))
    parser.add_argument("--season-id", type=int, default=_env_int("MATCHLENS_SEASON_ID", DEFAULT_SEASON_ID))
    parser.add_argument("--expected-matches", type=int, default=_env_int("MATCHLENS_EXPECTED_MATCHES", DEFAULT_EXPECTED_MATCHES))
    parser.add_argument("--expected-shots", type=int, default=_env_int("MATCHLENS_EXPECTED_SHOTS", DEFAULT_EXPECTED_SHOTS))
    parser.add_argument("--no-count-check", action="store_true", help="skip the expected match/shot count check")
    parser.add_argument("--database-url", default=None, help="defaults to $DATABASE_URL")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    engine = create_db_engine(args.database_url)
    try:
        create_schema(engine)
        report = import_statsbomb(
            engine,
            args.data_dir,
            args.competition_id,
            args.season_id,
            expected_matches=None if args.no_count_check else args.expected_matches,
            expected_shots=None if args.no_count_check else args.expected_shots,
        )
    except LoaderError as exc:
        log.error("%s", exc)
        return 1
    finally:
        engine.dispose()

    print(
        f"Loaded competition {report.competition_id} season {report.season_id}: "
        f"{report.matches_inserted} matches inserted, {report.matches_updated} updated, "
        f"{report.matches_pruned} pruned; {report.team_views_written} team views; "
        f"{report.shots_written} shots ({report.shots_replaced} replaced)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
