"""MatchLens HTTP API.

Endpoints
    GET /api/teams/{team_id}/form   rolling N-match form (window functions over team_match_views)
    GET /api/shots/analysis         shot-quality breakdown: goals vs StatsBomb xG by category
    GET /                           Jinja2 dashboard of rolling 5-match form
    GET /health                     liveness + database reachability

In deployment the API connects as a read-only database role; it never writes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from db.engine import create_db_engine

log = logging.getLogger("matchlens.api")

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
MAX_WINDOW = 38


# --------------------------------------------------------------------------- SQL

ROLLING_FORM_SQL = text(
    """
    SELECT
        v.match_id,
        v.match_date,
        v.team_name,
        v.opponent_name,
        v.is_home,
        v.goals_for,
        v.goals_against,
        v.result,
        v.points,
        v.xg,
        v.xg_against,
        ROW_NUMBER()                                OVER (ORDER BY v.match_date, v.match_id) AS match_number,
        COUNT(*)                                    OVER w AS games_in_window,
        SUM(v.points)                               OVER w AS rolling_points,
        SUM(v.goals_for - v.goals_against)          OVER w AS rolling_goal_diff,
        ROUND((AVG(v.xg) OVER w)::numeric, 3)              AS rolling_xg,
        ROUND((AVG(v.xg_against) OVER w)::numeric, 3)      AS rolling_xg_against,
        STRING_AGG(v.result, '')                    OVER w AS form,
        SUM(v.points)                               OVER (ORDER BY v.match_date, v.match_id) AS cumulative_points
    FROM team_match_views AS v
    WHERE v.team_id = :team_id
    WINDOW w AS (
        ORDER BY v.match_date, v.match_id
        ROWS BETWEEN CAST(:preceding AS integer) PRECEDING AND CURRENT ROW
    )
    ORDER BY v.match_date, v.match_id
    """
)

TEAMS_SQL = text(
    """
    SELECT team_id, team_name, COUNT(*) AS matches
    FROM team_match_views
    GROUP BY team_id, team_name
    ORDER BY matches DESC, team_name
    """
)


class ShotGrouping(str, Enum):
    body_part = "body_part"
    play_pattern = "play_pattern"
    technique = "technique"
    shot_type = "shot_type"
    outcome = "outcome"
    team = "team"


# Whitelisted SQL expressions; user input only ever selects a key.
_GROUP_EXPRESSIONS = {
    ShotGrouping.body_part: "s.body_part",
    ShotGrouping.play_pattern: "s.play_pattern",
    ShotGrouping.technique: "s.technique",
    ShotGrouping.shot_type: "s.shot_type",
    ShotGrouping.outcome: "s.outcome",
    ShotGrouping.team: "v.team_name",
}


def _shot_analysis_sql(group_by: ShotGrouping, filter_team: bool) -> Any:
    where = "WHERE s.team_id = :team_id" if filter_team else ""
    return text(
        f"""
        SELECT
            {_GROUP_EXPRESSIONS[group_by]}                                       AS bucket,
            COUNT(*)                                                             AS shots,
            COUNT(*) FILTER (WHERE s.is_goal)                                    AS goals,
            ROUND(SUM(s.statsbomb_xg)::numeric, 3)                               AS xg,
            ROUND((COUNT(*) FILTER (WHERE s.is_goal) - SUM(s.statsbomb_xg))::numeric, 3) AS goals_minus_xg,
            ROUND(AVG(s.statsbomb_xg)::numeric, 4)                               AS xg_per_shot,
            ROUND((COUNT(*) FILTER (WHERE s.is_goal))::numeric / COUNT(*), 4)    AS conversion_rate,
            ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)                   AS share_of_shots_pct
        FROM shots AS s
        JOIN team_match_views AS v ON v.match_id = s.match_id AND v.team_id = s.team_id
        {where}
        GROUP BY 1
        ORDER BY shots DESC, bucket
        """
    )


# --------------------------------------------------------------------------- schemas


class FormRow(BaseModel):
    match_number: int
    match_id: int
    match_date: date
    opponent_name: str
    is_home: bool
    goals_for: int
    goals_against: int
    result: str
    points: int
    xg: float
    xg_against: float
    games_in_window: int
    rolling_points: int
    rolling_goal_diff: int
    rolling_xg: float
    rolling_xg_against: float
    form: str
    cumulative_points: int


class TeamForm(BaseModel):
    team_id: int
    team_name: str
    window: int
    matches: list[FormRow]


class ShotBucket(BaseModel):
    bucket: str
    shots: int
    goals: int
    xg: float
    goals_minus_xg: float
    xg_per_shot: float
    conversion_rate: float
    share_of_shots_pct: float


class ShotAnalysis(BaseModel):
    group_by: ShotGrouping
    team_id: int | None
    total_shots: int
    total_goals: int
    total_xg: float
    buckets: list[ShotBucket]


# --------------------------------------------------------------------------- queries


def fetch_rolling_form(conn: Connection, team_id: int, window: int) -> TeamForm | None:
    rows = conn.execute(ROLLING_FORM_SQL, {"team_id": team_id, "preceding": window - 1}).mappings().all()
    if not rows:
        return None
    return TeamForm(
        team_id=team_id,
        team_name=rows[0]["team_name"],
        window=window,
        matches=[FormRow.model_validate(dict(r)) for r in rows],
    )


def fetch_shot_analysis(conn: Connection, group_by: ShotGrouping, team_id: int | None) -> ShotAnalysis:
    sql = _shot_analysis_sql(group_by, filter_team=team_id is not None)
    params = {"team_id": team_id} if team_id is not None else {}
    buckets = [ShotBucket.model_validate(dict(r)) for r in conn.execute(sql, params).mappings()]
    return ShotAnalysis(
        group_by=group_by,
        team_id=team_id,
        total_shots=sum(b.shots for b in buckets),
        total_goals=sum(b.goals for b in buckets),
        total_xg=round(sum(b.xg for b in buckets), 3),
        buckets=buckets,
    )


# --------------------------------------------------------------------------- app


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the application. Pass ``engine`` to share one (tests); otherwise
    one is created from $DATABASE_URL at startup and disposed at shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_engine = engine is None
        app.state.engine = engine or create_db_engine(pool_size=5, max_overflow=5)
        try:
            yield
        finally:
            if owns_engine:
                app.state.engine.dispose()

    app = FastAPI(
        title="MatchLens Football Analysis",
        version="1.0.0",
        summary="Premier League match and StatsBomb shot analytics.",
        lifespan=lifespan,
    )

    def get_conn(request: Request) -> Iterator[Connection]:
        with request.app.state.engine.connect() as conn:
            yield conn

    DbConn = Annotated[Connection, Depends(get_conn)]
    Window = Annotated[int, Query(ge=1, le=MAX_WINDOW, description="Matches per rolling window")]

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        log.exception("database error", exc_info=exc)
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.get("/health", tags=["ops"])
    def health(conn: DbConn) -> dict[str, str]:
        conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/teams/{team_id}/form", response_model=TeamForm, tags=["analysis"])
    def team_form(team_id: int, conn: DbConn, window: Window = 5) -> TeamForm:
        form = fetch_rolling_form(conn, team_id, window)
        if form is None:
            raise HTTPException(status_code=404, detail=f"no matches found for team {team_id}")
        return form

    @app.get("/api/shots/analysis", response_model=ShotAnalysis, tags=["analysis"])
    def shot_analysis(
        conn: DbConn,
        group_by: ShotGrouping = ShotGrouping.body_part,
        team_id: int | None = None,
    ) -> ShotAnalysis:
        return fetch_shot_analysis(conn, group_by, team_id)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, conn: DbConn, team_id: int | None = None, window: Window = 5) -> HTMLResponse:
        teams = [dict(r) for r in conn.execute(TEAMS_SQL).mappings()]
        if team_id is None and teams:
            team_id = teams[0]["team_id"]
        form = fetch_rolling_form(conn, team_id, window) if team_id is not None else None
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {"teams": teams, "selected_team_id": team_id, "window": window, "form": form, "max_points": 3 * window},
        )

    return app


app = create_app()
