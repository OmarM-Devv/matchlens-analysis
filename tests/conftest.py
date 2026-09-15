"""Fixtures for the PostgreSQL integration suite.

Set TEST_DATABASE_URL (a role allowed to CREATE SCHEMA) to run the tests, e.g.
    postgresql+psycopg://matchlens:secret@localhost:5432/matchlens

Each session works in its own throwaway schema, so the suite never touches the
tables in ``public`` and can safely run against the development database.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from db.engine import create_schema

COMPETITION_ID = 2
SEASON_ID = 44
FOCUS_TEAM = (1, "Arsenal")
# (goals for, goals against) from the focus team's side: W D L W D L
SCORES = [(2, 0), (1, 1), (0, 1), (3, 2), (2, 2), (0, 2)]
# Shots per match: focus team = goals + Saved + Off T; opponent = goals + Saved.
MISSES = {"focus": [("Saved", 0.10), ("Off T", 0.05)], "opponent": [("Saved", 0.10)]}
GOAL_XG = 0.40
EVENT_NS = uuid.UUID("7f3c1f0e-6a55-4d2b-9d1e-4f2a8c1b9e10")


def _named(id_: int, name: str) -> dict[str, Any]:
    return {"id": id_, "name": name}


def _event(match_id: int, index: int, type_name: str, team: tuple[int, str], **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid5(EVENT_NS, f"{match_id}:{index}")),
        "index": index,
        "period": 1,
        "minute": min(index, 45),
        "second": 30,
        "type": _named(16 if type_name == "Shot" else 30, type_name),
        "team": _named(*team),
        "play_pattern": _named(1, "Regular Play"),
        **extra,
    }


def _shot(match_id: int, index: int, team: tuple[int, str], outcome: str, xg: float) -> dict[str, Any]:
    return _event(
        match_id,
        index,
        "Shot",
        team,
        player=_named(team[0] * 1000 + index, f"Player {team[0]}-{index}"),
        location=[102.0, 38.5],
        shot={
            "statsbomb_xg": xg,
            "end_location": [120.0, 39.0, 1.2],
            "outcome": _named(97, outcome),
            "body_part": _named(40, "Right Foot"),
            "technique": _named(93, "Normal"),
            "type": _named(87, "Open Play"),
        },
    )


@dataclass
class StatsBombFixture:
    """A small StatsBomb-shaped dataset on disk. Mutate ``matches``/``events``
    and call :meth:`write` to simulate a corrected upstream export."""

    root: Path
    matches: list[dict[str, Any]]
    events: dict[int, list[dict[str, Any]]]

    def write(self) -> Path:
        matches_dir = self.root / "matches" / str(COMPETITION_ID)
        events_dir = self.root / "events"
        matches_dir.mkdir(parents=True, exist_ok=True)
        events_dir.mkdir(parents=True, exist_ok=True)
        (matches_dir / f"{SEASON_ID}.json").write_text(json.dumps(self.matches), encoding="utf-8")
        for match_id, events in self.events.items():
            (events_dir / f"{match_id}.json").write_text(json.dumps(events), encoding="utf-8")
        return self.root

    def shots(self, match_id: int | None = None) -> list[dict[str, Any]]:
        """Live references to shot events, optionally for a single match."""
        match_ids = [match_id] if match_id is not None else [m["match_id"] for m in self.matches]
        return [e for mid in match_ids for e in self.events[mid] if e["type"]["name"] == "Shot"]

    @property
    def shot_count(self) -> int:
        return len(self.shots())


def build_fixture(root: Path) -> StatsBombFixture:
    matches: list[dict[str, Any]] = []
    events: dict[int, list[dict[str, Any]]] = {}
    kickoff = date(2003, 8, 16)

    for i, (goals_for, goals_against) in enumerate(SCORES):
        match_id = 3_749_000 + i
        opponent = (100 + i, f"Opponent {i + 1}")
        focus_at_home = i % 2 == 0
        home, away = (FOCUS_TEAM, opponent) if focus_at_home else (opponent, FOCUS_TEAM)
        home_score, away_score = (goals_for, goals_against) if focus_at_home else (goals_against, goals_for)
        matches.append(
            {
                "match_id": match_id,
                "match_date": (kickoff + timedelta(weeks=i)).isoformat(),
                "kick_off": "15:00:00.000",
                "competition": {"competition_id": COMPETITION_ID, "country_name": "England", "competition_name": "Premier League"},
                "season": {"season_id": SEASON_ID, "season_name": "2003/2004"},
                "home_team": {"home_team_id": home[0], "home_team_name": home[1]},
                "away_team": {"away_team_id": away[0], "away_team_name": away[1]},
                "home_score": home_score,
                "away_score": away_score,
                "match_week": i + 1,
                "stadium": {"id": 1, "name": "Highbury"},
                "referee": {"id": 7, "name": "A. Referee"},
            }
        )

        match_events = [_event(match_id, 1, "Pass", home)]  # non-shot events must be ignored
        index = 2
        for side, team, goals in (("focus", FOCUS_TEAM, goals_for), ("opponent", opponent, goals_against)):
            for outcome, xg in [("Goal", GOAL_XG)] * goals + MISSES[side]:
                match_events.append(_shot(match_id, index, team, outcome, xg))
                index += 1
            match_events.append(_event(match_id, index, "Pass", team))
            index += 1
        events[match_id] = match_events

    return StatsBombFixture(root, matches, events)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run the PostgreSQL integration tests")

    schema = f"matchlens_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    test_engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    try:
        create_schema(test_engine)
        yield test_engine
    finally:
        test_engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def db(engine: Engine) -> Engine:
    """The session engine, with all MatchLens tables emptied before the test."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE shots, team_match_views, matches"))
    return engine


@pytest.fixture
def sb(tmp_path: Path) -> StatsBombFixture:
    fixture = build_fixture(tmp_path / "statsbomb")
    fixture.write()
    return fixture
