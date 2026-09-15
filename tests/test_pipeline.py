"""Integration tests for the transactional StatsBomb loader and the API built on it.

The fixture dataset (tests/conftest.py) is 6 matches for the focus team with
results W D L W D L, 34 shot events, and some non-shot events mixed in.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from api.main import create_app
from db.loader import DataValidationError, LoadError, LoadReport, import_statsbomb
from db.models import Match, Shot, TeamMatchView
from tests.conftest import COMPETITION_ID, FOCUS_TEAM, SEASON_ID, StatsBombFixture

pytestmark = pytest.mark.integration

TOTAL_MATCHES = 6
TOTAL_VIEWS = 12
TOTAL_SHOTS = 34
FOCUS_TEAM_ID = FOCUS_TEAM[0]


def run_import(engine: Engine, sb: StatsBombFixture, **kwargs: Any) -> LoadReport:
    return import_statsbomb(engine, sb.root, COMPETITION_ID, SEASON_ID, **kwargs)


def table_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as conn:
        return tuple(  # type: ignore[return-value]
            conn.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (Match, TeamMatchView, Shot)
        )


def table_rows(engine: Engine, table: Table) -> list[Any]:
    with engine.connect() as conn:
        return conn.execute(select(table).order_by(*table.primary_key.columns)).all()


def focus_view(engine: Engine, match_id: int) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            select(TeamMatchView).where(
                TeamMatchView.match_id == match_id, TeamMatchView.team_id == FOCUS_TEAM_ID
            )
        ).one()


# --------------------------------------------------------------------------- first import


def test_first_import_loads_every_record(db: Engine, sb: StatsBombFixture) -> None:
    report = run_import(db, sb, expected_matches=TOTAL_MATCHES, expected_shots=TOTAL_SHOTS)

    assert (report.matches_inserted, report.matches_updated, report.team_views_written, report.shots_written) == (
        TOTAL_MATCHES, 0, TOTAL_VIEWS, TOTAL_SHOTS,
    )
    assert table_counts(db) == (TOTAL_MATCHES, TOTAL_VIEWS, TOTAL_SHOTS)

    # Opening match: focus team won 2-0 with 2 goals + Saved + Off T.
    view = focus_view(db, sb.matches[0]["match_id"])
    assert (view.result, view.points, view.goals_for, view.goals_against) == ("W", 3, 2, 0)
    assert (view.shots, view.shots_on_target) == (4, 3)
    assert (view.xg, view.xg_against) == pytest.approx((0.95, 0.10))


# --------------------------------------------------------------------------- repeat imports


def test_repeat_import_is_idempotent(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    shots_before = table_rows(db, Shot.__table__)
    views_before = table_rows(db, TeamMatchView.__table__)
    with db.connect() as conn:
        first_loaded_at = conn.execute(select(func.max(Match.loaded_at))).scalar_one()

    report = run_import(db, sb)

    assert (report.matches_inserted, report.matches_updated, report.shots_replaced) == (0, TOTAL_MATCHES, TOTAL_SHOTS)
    assert table_counts(db) == (TOTAL_MATCHES, TOTAL_VIEWS, TOTAL_SHOTS)
    assert table_rows(db, Shot.__table__) == shots_before
    assert table_rows(db, TeamMatchView.__table__) == views_before
    with db.connect() as conn:
        assert conn.execute(select(func.min(Match.loaded_at))).scalar_one() > first_loaded_at


def test_repeat_import_applies_upstream_corrections(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    match_id = sb.matches[0]["match_id"]

    sb.matches[0]["away_score"] = 2  # 2-0 corrected to 2-2
    removed = sb.shots(match_id)[-1]
    sb.events[match_id].remove(removed)
    sb.write()

    report = run_import(db, sb)

    with db.connect() as conn:
        score = conn.execute(select(Match.home_score, Match.away_score).where(Match.match_id == match_id)).one()
        removed_row = conn.execute(select(Shot.shot_id).where(Shot.shot_id == uuid.UUID(removed["id"]))).first()
    view = focus_view(db, match_id)

    assert tuple(score) == (2, 2)
    assert (view.result, view.points) == ("D", 1)
    assert removed_row is None
    assert report.shots_written == TOTAL_SHOTS - 1
    assert table_counts(db) == (TOTAL_MATCHES, TOTAL_VIEWS, TOTAL_SHOTS - 1)


def test_matches_dropped_from_snapshot_are_pruned(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    dropped_id = sb.matches[-1]["match_id"]
    dropped_shots = len(sb.shots(dropped_id))
    sb.matches.pop()
    sb.write()

    report = run_import(db, sb)

    with db.connect() as conn:
        orphan_shots = conn.execute(
            select(func.count()).select_from(Shot).where(Shot.match_id == dropped_id)
        ).scalar_one()
    assert report.matches_pruned == 1
    assert orphan_shots == 0
    assert table_counts(db) == (TOTAL_MATCHES - 1, TOTAL_VIEWS - 2, TOTAL_SHOTS - dropped_shots)


# --------------------------------------------------------------------------- failure handling


def test_database_error_rolls_back_the_entire_import(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    before = {t.name: table_rows(db, t) for t in (Match.__table__, TeamMatchView.__table__, Shot.__table__)}

    # A valid change that is written first, followed by a shot the CHECK constraint rejects.
    sb.matches[0]["home_score"] = 9
    sb.shots(sb.matches[-1]["match_id"])[0]["location"] = [500.0, 40.0]
    sb.write()

    with pytest.raises(LoadError, match="location_on_pitch"):
        run_import(db, sb)

    assert table_rows(db, Match.__table__) == before["matches"]
    assert table_rows(db, TeamMatchView.__table__) == before["team_match_views"]
    assert table_rows(db, Shot.__table__) == before["shots"]


def test_count_mismatch_is_rejected_before_writing(db: Engine, sb: StatsBombFixture) -> None:
    with pytest.raises(DataValidationError, match="expected 1086 shots, found 34"):
        run_import(db, sb, expected_matches=TOTAL_MATCHES, expected_shots=1086)
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
    with db.connect() as conn:
        shot = dict(conn.execute(select(Shot.__table__).where(Shot.match_id == match_id).limit(1)).mappings().one())
        view = dict(conn.execute(select(TeamMatchView.__table__).where(TeamMatchView.match_id == match_id).limit(1)).mappings().one())
    shot.pop("is_goal")  # generated column

    violations = [
        (
            insert(Shot).values({**shot, "shot_id": uuid.uuid4(), "event_index": 9_999, "team_id": 999}),
            "fk_shots_match_id_team_id_team_match_views",
        ),
        (
            insert(TeamMatchView).values({**view, "team_id": 999, "team_name": "Imposter"}),
            "uq_team_match_views_match_id_is_home",
        ),
        (
            update(TeamMatchView)
            .where(TeamMatchView.match_id == match_id, TeamMatchView.team_id == FOCUS_TEAM_ID)
            .values(points=0),
            "ck_team_match_views_result_consistency",
        ),
    ]
    for statement, constraint in violations:
        with pytest.raises(IntegrityError, match=constraint), db.begin() as conn:
            conn.execute(statement)


def test_concurrent_imports_serialise_without_duplicates(db: Engine, sb: StatsBombFixture) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: run_import(db, sb), range(2)))

    assert sorted(r.matches_inserted for r in reports) == [0, TOTAL_MATCHES]
    assert table_counts(db) == (TOTAL_MATCHES, TOTAL_VIEWS, TOTAL_SHOTS)


# --------------------------------------------------------------------------- API


def test_rolling_form_endpoint_applies_the_window(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    with TestClient(create_app(engine=db)) as client:
        response = client.get(f"/api/teams/{FOCUS_TEAM_ID}/form", params={"window": 5})

    matches = response.json()["matches"]
    assert response.status_code == 200
    # Points 3 1 0 3 1 0: the sixth window drops the opening win.
    assert [(m["games_in_window"], m["rolling_points"]) for m in matches] == [
        (1, 3), (2, 4), (3, 4), (4, 7), (5, 8), (5, 5),
    ]
    assert matches[-1]["form"] == "DLWDL"


def test_dashboard_and_shot_analysis_render(db: Engine, sb: StatsBombFixture) -> None:
    run_import(db, sb)
    with TestClient(create_app(engine=db)) as client:
        dashboard = client.get("/", params={"team_id": FOCUS_TEAM_ID})
        analysis = client.get("/api/shots/analysis", params={"group_by": "outcome", "team_id": FOCUS_TEAM_ID})

    assert dashboard.status_code == 200 and "Rolling 5-match form" in dashboard.text and FOCUS_TEAM[1] in dashboard.text
    body = analysis.json()
    assert (analysis.status_code, body["total_shots"], body["total_goals"]) == (200, 20, 8)
