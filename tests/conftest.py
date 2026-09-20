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

from loader.cleaner import apply_schema

COMPETITION_ID = 2
SEASON_ID = 27
FOCUS_TEAM = (22, "Leicester City")
# (goals for, goals against) from the focus team's side: W D L W D L.
# The focus team is at home in matches 1, 3 and 5.
SCORES = [(2, 0), (1, 1), (0, 1), (3, 2), (2, 2), (0, 2)]
# In the opening match one of the focus team's two goals is an opponent own
# goal: an "Own Goal For" event, not a shot.
OWN_GOAL_MATCH = 0
# In the third match the focus team has one extra shot event without
# statsbomb_xg, which the loader must set aside rather than load.
NO_XG_MATCH = 2
# Shots per match: focus team = shot goals + Saved + Off T; opponent = goals + Saved.
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


def _match(match_id: int, day: date, week: int, home: tuple[int, str], away: tuple[int, str],
           home_score: int, away_score: int) -> dict[str, Any]:
    return {
        "match_id": match_id,
        "match_date": day.isoformat(),
        "kick_off": "15:00:00.000",
        "competition": {"competition_id": COMPETITION_ID, "country_name": "England", "competition_name": "Premier League"},
        "season": {"season_id": SEASON_ID, "season_name": "2015/2016"},
        "home_team": {"home_team_id": home[0], "home_team_name": home[1]},
        "away_team": {"away_team_id": away[0], "away_team_name": away[1]},
        "home_score": home_score,
        "away_score": away_score,
        "match_week": week,
        "stadium": {"id": 1, "name": "King Power Stadium"},
        "referee": {"id": 7, "name": "A. Referee"},
    }


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

    @property
    def focus_matches(self) -> list[dict[str, Any]]:
        return [m for m in self.matches if m["match_id"] in self.events]

    def shots(self, match_id: int | None = None) -> list[dict[str, Any]]:
        """Live references to shot events, optionally for a single match."""
        match_ids = [match_id] if match_id is not None else list(self.events)
        return [e for mid in match_ids for e in self.events[mid] if e["type"]["name"] == "Shot"]


def build_fixture(root: Path) -> StatsBombFixture:
    matches: list[dict[str, Any]] = []
    events: dict[int, list[dict[str, Any]]] = {}
    kickoff = date(2015, 8, 8)

    for i, (goals_for, goals_against) in enumerate(SCORES):
        match_id = 3_754_000 + i
        opponent = (100 + i, f"Opponent {i + 1}")
        focus_at_home = i % 2 == 0
        home, away = (FOCUS_TEAM, opponent) if focus_at_home else (opponent, FOCUS_TEAM)
        home_score, away_score = (goals_for, goals_against) if focus_at_home else (goals_against, goals_for)
        matches.append(_match(match_id, kickoff + timedelta(weeks=i), i + 1, home, away, home_score, away_score))

        match_events = [_event(match_id, 1, "Pass", home)]  # non-shot events must be ignored
        index = 2
        own_goals = 1 if i == OWN_GOAL_MATCH else 0
        if own_goals:
            match_events.append(_event(match_id, index, "Own Goal For", FOCUS_TEAM))
            match_events.append(_event(match_id, index + 1, "Own Goal Against", opponent))
            index += 2
        for side, team, goals in (("focus", FOCUS_TEAM, goals_for - own_goals), ("opponent", opponent, goals_against)):
            for outcome, xg in [("Goal", GOAL_XG)] * goals + MISSES[side]:
                match_events.append(_shot(match_id, index, team, outcome, xg))
                index += 1
            match_events.append(_event(match_id, index, "Pass", team))
            index += 1
        if i == NO_XG_MATCH:
            no_xg = _shot(match_id, index, FOCUS_TEAM, "Off T", 0.0)
            del no_xg["shot"]["statsbomb_xg"]
            match_events.append(no_xg)
        events[match_id] = match_events

    # A match the focus team did not play; it has no events file and must be skipped.
    matches.append(_match(3_754_999, kickoff, 1, (100, "Opponent 1"), (101, "Opponent 2"), 1, 0))
    return StatsBombFixture(root, matches, events)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("TEST_DATABASE_URL is not set; CI must run the integration tests")
        pytest.skip("set TEST_DATABASE_URL to run the PostgreSQL integration tests")

    schema = f"matchlens_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    test_engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    try:
        apply_schema(test_engine)
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
        conn.execute(text("TRUNCATE shots, matches, teams"))
    return engine


@pytest.fixture
def sb(tmp_path: Path) -> StatsBombFixture:
    fixture = build_fixture(tmp_path / "statsbomb")
    fixture.write()
    return fixture
