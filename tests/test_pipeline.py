"""Integration tests for the transactional StatsBomb loader, the SQL query layer
and the web application built on it.

The fixture dataset (tests/conftest.py) is 6 focus-team matches with results
W D L W D L, 34 shot events (33 with xG, one without, which the loader sets
aside), one own goal, non-shot events mixed in, and one match the focus team
did not play (which the loader must skip).
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from loader.cleaner import DataValidationError, LoadError, LoadReport, apply_schema, import_statsbomb
from tests.conftest import COMPETITION_ID, FOCUS_TEAM, SEASON_ID, StatsBombFixture

pytestmark = pytest.mark.integration

TOTAL_TEAMS = 7
TOTAL_MATCHES = 6
TOTAL_SHOTS = 33  # loaded: shot events with statsbomb_xg
SHOT_EVENTS = 34  # every shot event, including the one set aside without xG
FOCUS_TEAM_ID = FOCUS_TEAM[0]


def run_import(engine: Engine, sb: StatsBombFixture, **kwargs: Any) -> LoadReport:
    return import_statsbomb(engine, sb.root, COMPETITION_ID, SEASON_ID, FOCUS_TEAM_ID, **kwargs)


def table_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as conn:
        return tuple(  # type: ignore[return-value]
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in ("teams", "matches", "shots")
        )


def table_rows(engine: Engine, table: str, order_by: str) -> list[Any]:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT * FROM {table} ORDER BY {order_by}")).all()


def focus_view(engine: Engine, match_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM team_match_view WHERE match_id = :m AND team_id = :t"),
            {"m": match_id, "t": FOCUS_TEAM_ID},
        ).one()


def client_for(engine: Engine) -> TestClient:
    return TestClient(create_app(engine=engine, team_id=FOCUS_TEAM_ID))


# --------------------------------------------------------------------------- first import


def test_first_import_loads_only_the_focus_teams_matches(db: Engine, sb: StatsBombFixture) -> None:
    # The expected count covers every shot event; the one without xG is set aside, not loaded.
    report = run_import(db, sb, expected_matches=TOTAL_MATCHES, expected_shots=SHOT_EVENTS)

    assert (report.matches_inserted, report.matches_updated, report.shots_written, report.shots_skipped) == (
        TOTAL_MATCHES, 0, TOTAL_SHOTS, SHOT_EVENTS - TOTAL_SHOTS,
    )
    assert table_counts(db) == (TOTAL_TEAMS, TOTAL_MATCHES, TOTAL_SHOTS)
    with db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM team_match_view")).scalar_one() == 2 * TOTAL_MATCHES


# --------------------------------------------------------------------------- repeat imports


def test_repeat_import_is_idempotent(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    shots_before = table_rows(db, "shots", "shot_id")
    with db.connect() as conn:
        first_loaded_at = conn.execute(text("SELECT MAX(loaded_at) FROM matches")).scalar_one()

    report = run_import(db, sb)

    assert (report.matches_inserted, report.matches_updated, report.shots_replaced) == (0, TOTAL_MATCHES, TOTAL_SHOTS)
    assert table_counts(db) == (TOTAL_TEAMS, TOTAL_MATCHES, TOTAL_SHOTS)
    assert table_rows(db, "shots", "shot_id") == shots_before
    with db.connect() as conn:
        assert conn.execute(text("SELECT MIN(loaded_at) FROM matches")).scalar_one() > first_loaded_at


def test_repeat_import_applies_upstream_corrections(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    match_id = sb.matches[0]["match_id"]

    sb.matches[0]["away_score"] = 2  # 2-0 corrected to 2-2
    removed = sb.shots(match_id)[-1]
    sb.events[match_id].remove(removed)
    sb.write()

    report = run_import(db, sb)

    with db.connect() as conn:
        score = conn.execute(
            text("SELECT home_score, away_score FROM matches WHERE match_id = :m"), {"m": match_id}
        ).one()
        removed_row = conn.execute(
            text("SELECT 1 FROM shots WHERE shot_id = :s"), {"s": uuid.UUID(removed["id"])}
        ).first()
    view = focus_view(db, match_id)

    assert tuple(score) == (2, 2)
    assert (view.result, view.points) == ("D", 1)
    assert removed_row is None
    assert report.shots_written == TOTAL_SHOTS - 1
    assert table_counts(db) == (TOTAL_TEAMS, TOTAL_MATCHES, TOTAL_SHOTS - 1)


def test_matches_dropped_from_snapshot_are_pruned(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    dropped_id = sb.focus_matches[-1]["match_id"]
    dropped_shots = len(sb.shots(dropped_id))
    sb.matches = [m for m in sb.matches if m["match_id"] != dropped_id]
    sb.write()

    report = run_import(db, sb)

    with db.connect() as conn:
        orphan_shots = conn.execute(
            text("SELECT COUNT(*) FROM shots WHERE match_id = :m"), {"m": dropped_id}
        ).scalar_one()
    assert report.matches_pruned == 1
    assert orphan_shots == 0
    assert table_counts(db) == (TOTAL_TEAMS, TOTAL_MATCHES - 1, TOTAL_SHOTS - dropped_shots)


# --------------------------------------------------------------------------- failure handling


def test_database_error_rolls_back_the_entire_import(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    matches_before = table_rows(db, "matches", "match_id")
    shots_before = table_rows(db, "shots", "shot_id")

    # A valid change that is written first, followed by a shot the CHECK constraint rejects.
    sb.matches[0]["home_score"] = 9
    sb.shots(sb.focus_matches[-1]["match_id"])[0]["location"] = [500.0, 40.0]
    sb.write()

    with pytest.raises(LoadError, match="location_on_pitch"):
        run_import(db, sb)

    assert table_rows(db, "matches", "match_id") == matches_before
    assert table_rows(db, "shots", "shot_id") == shots_before


def test_count_mismatch_is_rejected_before_writing(db: Engine, sb: StatsBombFixture) -> None:
    with pytest.raises(DataValidationError, match=r"expected 1042 shots, found 34 \(33 with xG, 1 set aside without xG\)"):
        run_import(db, sb, expected_matches=TOTAL_MATCHES, expected_shots=1042)
    assert table_counts(db) == (0, 0, 0)


def test_shot_for_team_not_in_match_is_rejected(db: Engine, sb: StatsBombFixture) -> None:
    sb.shots(sb.matches[0]["match_id"])[0]["team"] = {"id": 999, "name": "Imposter"}
    sb.write()

    with pytest.raises(DataValidationError, match="not in match"):
        run_import(db, sb)
    assert table_counts(db) == (0, 0, 0)


def test_missing_events_file_is_rejected(db: Engine, sb: StatsBombFixture) -> None:
    (sb.root / "events" / f"{sb.matches[2]['match_id']}.json").unlink()

    with pytest.raises(DataValidationError, match="no events file"):
        run_import(db, sb)
    assert table_counts(db) == (0, 0, 0)


def test_schema_constraints_reject_inconsistent_rows(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    match_id = sb.matches[0]["match_id"]
    other_team = sb.matches[1]["home_team"]["home_team_id"]  # exists, but did not play match 0
    with db.connect() as conn:
        shot = dict(conn.execute(text("SELECT * FROM shots WHERE match_id = :m LIMIT 1"), {"m": match_id}).mappings().one())
    for generated in ("is_goal", "is_on_target"):
        shot.pop(generated)
    columns = ", ".join(shot)
    binds = ", ".join(f":{c}" for c in shot)
    insert_shot = text(f"INSERT INTO shots ({columns}) VALUES ({binds})")

    violations = [
        (insert_shot, {**shot, "shot_id": uuid.uuid4(), "event_index": 9_998, "team_id": other_team}, "shots_team_in_match"),
        (insert_shot, {**shot, "shot_id": uuid.uuid4(), "event_index": 9_999, "statsbomb_xg": 1.5}, "ck_shots_xg_probability"),
        (text("UPDATE matches SET away_team_id = home_team_id WHERE match_id = :m"), {"m": match_id}, "ck_matches_distinct_teams"),
    ]
    for statement, params, constraint in violations:
        with pytest.raises(IntegrityError, match=constraint), db.begin() as conn:
            conn.execute(statement, params)


def test_pre_refactor_layout_is_refused(db: Engine) -> None:
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE team_match_views (match_id bigint)"))
    try:
        with pytest.raises(LoadError, match="pre-refactor layout"):
            apply_schema(db)
    finally:
        with db.begin() as conn:
            conn.execute(text("DROP TABLE team_match_views"))


def test_concurrent_imports_serialise_without_duplicates(db: Engine, sb: StatsBombFixture) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: run_import(db, sb), range(2)))

    assert sorted(r.matches_inserted for r in reports) == [0, TOTAL_MATCHES]
    assert table_counts(db) == (TOTAL_TEAMS, TOTAL_MATCHES, TOTAL_SHOTS)


# --------------------------------------------------------------------------- query layer and app


def test_match_report_separates_official_goals_from_shot_goals(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    with client_for(db) as client:
        response = client.get(f"/api/matches/{sb.matches[0]['match_id']}/report")

    body = response.json()
    home, away = body["home"], body["away"]
    assert response.status_code == 200
    # Focus team won 2-0 at home: one shot goal plus one opponent own goal.
    assert [(s["goals_for"], s["shot_goals"], s["goals_not_from_shots"], s["shots"], s["shots_on_target"]) for s in (home, away)] == [
        (2, 1, 1, 3, 2), (0, 0, 0, 1, 1),
    ]
    assert (home["xg"], away["xg"]) == pytest.approx((0.55, 0.10))


def test_rolling_form_endpoint_applies_the_window(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    with client_for(db) as client:
        response = client.get("/api/form", params={"window": 5})

    matches = response.json()["matches"]
    # Points 3 1 0 3 1 0: the sixth window drops the opening win.
    assert (response.status_code, [(m["games_in_window"], m["rolling_points"]) for m in matches]) == (
        200, [(1, 3), (2, 4), (3, 4), (4, 7), (5, 8), (5, 5)],
    )
    assert matches[-1]["form"] == "DLWDL"


def test_home_away_aggregates_shots_before_joining(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    with client_for(db) as client:
        venues = client.get("/api/home-away").json()["venues"]

    # Joining raw shots before summing would multiply goals and points by shots per match.
    assert [(v["venue"], v["played"], v["points"], v["goals_for"], v["goals_against"], v["shots"]) for v in venues] == [
        ("Home", 3, 4, 4, 3, 9), ("Away", 3, 4, 4, 5, 10),
    ]


def test_pages_render(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    with client_for(db) as client:
        season = client.get("/")
        report = client.get(f"/matches/{sb.matches[0]['match_id']}")

    assert season.status_code == 200 and "Rolling 5-match form" in season.text and FOCUS_TEAM[1] in season.text
    assert report.status_code == 200 and "Goals not from shots" in report.text


def test_database_outage_returns_503() -> None:
    unreachable = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:9/none", connect_args={"connect_timeout": 2})
    with client_for(unreachable) as client:
        health, page = client.get("/health"), client.get("/")

    assert (health.status_code, health.json(), page.status_code) == (503, {"detail": "database unavailable"}, 503)
