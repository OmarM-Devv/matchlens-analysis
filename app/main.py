"""MatchLens web application: JSON endpoints and server-rendered pages over the
named queries in sql/queries.sql.

Pages
    GET /                                season overview: rolling form, home vs away, fixtures
    GET /matches/{match_id}              match report
JSON
    GET /api/matches                     the focus team's fixtures
    GET /api/matches/{match_id}/report   match report (both sides)
    GET /api/form?window=5               rolling N-match form
    GET /api/home-away                   home vs away comparison
    GET /health                          liveness + database reachability

The application only reads. In the compose stack it connects as a read-only
database role.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import TextClause

log = logging.getLogger("matchlens.app")

QUERIES_FILE = Path(__file__).resolve().parents[1] / "sql" / "queries.sql"
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
DEFAULT_TEAM_ID = 22  # Leicester City in StatsBomb open data
MAX_WINDOW = 38
CONNECT_TIMEOUT_SECONDS = 3


# --------------------------------------------------------------------------- query layer

_NAME_LINE = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)


def load_queries(path: Path = QUERIES_FILE) -> dict[str, TextClause]:
    """Split a SQL file into named statements on ``-- name: <name>`` lines."""
    source = path.read_text(encoding="utf-8")
    marks = list(_NAME_LINE.finditer(source))
    queries: dict[str, TextClause] = {}
    for mark, following in zip(marks, marks[1:] + [None]):
        body = source[mark.end(): following.start() if following else len(source)]
        queries[mark.group(1)] = text(body.strip().rstrip(";"))
    return queries


QUERIES = load_queries()


class SideReport(BaseModel):
    team_id: int
    team_name: str
    is_home: bool
    goals_for: int
    result: str
    points: int
    shots: int
    shots_on_target: int
    shot_goals: int
    goals_not_from_shots: int
    xg: float
    xg_per_shot: float | None


class MatchReport(BaseModel):
    match_id: int
    match_date: date
    match_week: int | None
    home: SideReport
    away: SideReport


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


class VenueSplit(BaseModel):
    venue: str
    played: int
    wins: int
    draws: int
    losses: int
    points: int
    points_per_match: float
    goals_for: int
    goals_against: int
    shots: int
    shots_on_target: int
    shots_against: int
    xg: float
    xg_against: float
    xg_per_match: float
    xg_against_per_match: float


class HomeAway(BaseModel):
    team_id: int
    venues: list[VenueSplit]


class Fixture(BaseModel):
    match_id: int
    match_date: date
    match_week: int | None
    opponent_name: str
    is_home: bool
    goals_for: int
    goals_against: int
    result: str


def fetch_match_report(conn: Connection, match_id: int) -> MatchReport | None:
    rows = conn.execute(QUERIES["match_report"], {"match_id": match_id}).mappings().all()
    if len(rows) != 2:
        return None
    home, away = rows
    return MatchReport(
        match_id=match_id,
        match_date=home["match_date"],
        match_week=home["match_week"],
        home=SideReport.model_validate(dict(home)),
        away=SideReport.model_validate(dict(away)),
    )


def fetch_rolling_form(conn: Connection, team_id: int, window: int) -> TeamForm | None:
    rows = conn.execute(QUERIES["rolling_form"], {"team_id": team_id, "preceding": window - 1}).mappings().all()
    if not rows:
        return None
    return TeamForm(
        team_id=team_id,
        team_name=rows[0]["team_name"],
        window=window,
        matches=[FormRow.model_validate(dict(r)) for r in rows],
    )


def fetch_home_away(conn: Connection, team_id: int) -> HomeAway:
    rows = conn.execute(QUERIES["home_away"], {"team_id": team_id}).mappings()
    return HomeAway(team_id=team_id, venues=[VenueSplit.model_validate(dict(r)) for r in rows])


def fetch_fixtures(conn: Connection, team_id: int) -> tuple[str | None, list[Fixture]]:
    rows = conn.execute(QUERIES["team_matches"], {"team_id": team_id}).mappings().all()
    team_name = rows[0]["team_name"] if rows else None
    return team_name, [Fixture.model_validate(dict(r)) for r in rows]


# --------------------------------------------------------------------------- app


def get_conn(request: Request) -> Iterator[Connection]:
    with request.app.state.engine.connect() as conn:
        yield conn


DbConn = Annotated[Connection, Depends(get_conn)]
Window = Annotated[int, Query(ge=1, le=MAX_WINDOW, description="Matches per rolling window")]


def create_app(engine: Engine | None = None, team_id: int | None = None) -> FastAPI:
    """Build the application. Pass ``engine`` to share one (tests); otherwise
    one is created from $DATABASE_URL at startup and disposed at shutdown."""
    focus_team = team_id if team_id is not None else int(os.environ.get("MATCHLENS_TEAM_ID", DEFAULT_TEAM_ID))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_engine = engine is None
        if owns_engine:
            url = os.environ.get("DATABASE_URL")
            if not url:
                raise RuntimeError("$DATABASE_URL is not set")
            app.state.engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                # Without this, psycopg waits ~130 s per request while the database is down.
                connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
            )
        else:
            app.state.engine = engine
        try:
            yield
        finally:
            if owns_engine:
                app.state.engine.dispose()

    app = FastAPI(
        title="MatchLens Football Analysis",
        version="2.0.0",
        summary="Descriptive match analysis of one team's season from StatsBomb open data.",
        lifespan=lifespan,
    )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> Response:
        log.error("database error on %s: %s", request.url.path, getattr(exc, "orig", exc))
        if request.url.path.startswith(("/api/", "/health")):
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        return TEMPLATES.TemplateResponse(request, "error.html", {"message": "The database is unavailable."}, status_code=503)

    @app.get("/health", tags=["ops"])
    def health(conn: DbConn) -> dict[str, str]:
        conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/matches", response_model=list[Fixture], tags=["analysis"])
    def matches(conn: DbConn) -> list[Fixture]:
        return fetch_fixtures(conn, focus_team)[1]

    @app.get("/api/matches/{match_id}/report", response_model=MatchReport, tags=["analysis"])
    def match_report(match_id: int, conn: DbConn) -> MatchReport:
        report = fetch_match_report(conn, match_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"match {match_id} not found")
        return report

    @app.get("/api/form", response_model=TeamForm, tags=["analysis"])
    def form(conn: DbConn, window: Window = 5) -> TeamForm:
        result = fetch_rolling_form(conn, focus_team, window)
        if result is None:
            raise HTTPException(status_code=404, detail=f"no matches loaded for team {focus_team}")
        return result

    @app.get("/api/home-away", response_model=HomeAway, tags=["analysis"])
    def home_away(conn: DbConn) -> HomeAway:
        return fetch_home_away(conn, focus_team)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def season_page(request: Request, conn: DbConn, window: Window = 5) -> HTMLResponse:
        team_name, fixtures = fetch_fixtures(conn, focus_team)
        context: dict[str, Any] = {
            "team_name": team_name,
            "window": window,
            "fixtures": fixtures,
            "form": fetch_rolling_form(conn, focus_team, window) if fixtures else None,
            "home_away": fetch_home_away(conn, focus_team) if fixtures else None,
        }
        return TEMPLATES.TemplateResponse(request, "season.html", context)

    @app.get("/matches/{match_id}", response_class=HTMLResponse, include_in_schema=False)
    def match_page(request: Request, match_id: int, conn: DbConn) -> HTMLResponse:
        report = fetch_match_report(conn, match_id)
        if report is None:
            return TEMPLATES.TemplateResponse(
                request, "error.html", {"message": f"Match {match_id} is not loaded."}, status_code=404
            )
        _, fixtures = fetch_fixtures(conn, focus_team)
        ids = [f.match_id for f in fixtures]
        pos = ids.index(match_id) if match_id in ids else None
        context = {
            "report": report,
            "previous_id": ids[pos - 1] if pos else None,
            "next_id": ids[pos + 1] if pos is not None and pos + 1 < len(ids) else None,
        }
        return TEMPLATES.TemplateResponse(request, "match_report.html", context)

    return app


app = create_app()
