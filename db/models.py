"""PostgreSQL schema for MatchLens.

Three tables, strictly layered so every row can be traced back to a match:

    matches            one row per fixture (StatsBomb match_id is the natural key)
    team_match_views   exactly two rows per match: the home and away team perspective
    shots              one row per StatsBomb shot event, owned by a (match, team) view

``shots`` carries a composite foreign key to ``team_match_views`` so a shot can
never be attributed to a team that did not play in that match.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Deterministic constraint names keep migrations and error messages stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Match(Base):
    __tablename__ = "matches"

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    competition_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_name: Mapped[str] = mapped_column(String(32), nullable=False)
    match_date: Mapped[date] = mapped_column(Date, nullable=False)
    kick_off: Mapped[time | None] = mapped_column(Time)
    match_week: Mapped[int | None] = mapped_column(SmallInteger)
    home_team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    home_team_name: Mapped[str] = mapped_column(String(100), nullable=False)
    away_team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    away_team_name: Mapped[str] = mapped_column(String(100), nullable=False)
    home_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    away_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    stadium_name: Mapped[str | None] = mapped_column(String(200))
    referee_name: Mapped[str | None] = mapped_column(String(200))
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    team_views: Mapped[list[TeamMatchView]] = relationship(
        back_populates="match", cascade="all, delete-orphan", passive_deletes=True
    )
    shots: Mapped[list[Shot]] = relationship(
        back_populates="match", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("home_team_id <> away_team_id", name="distinct_teams"),
        CheckConstraint("home_score >= 0 AND away_score >= 0", name="non_negative_score"),
        CheckConstraint("match_week IS NULL OR match_week BETWEEN 1 AND 60", name="match_week_range"),
        Index(None, "competition_id", "season_id", "match_date"),
    )


class TeamMatchView(Base):
    """A match seen from one team's side. Denormalises ``match_date`` so rolling
    window queries can be served from a single (team_id, match_date) index."""

    __tablename__ = "team_match_views"

    match_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("matches.match_id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_name: Mapped[str] = mapped_column(String(100), nullable=False)
    opponent_id: Mapped[int] = mapped_column(Integer, nullable=False)
    opponent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False)
    match_date: Mapped[date] = mapped_column(Date, nullable=False)
    goals_for: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    goals_against: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    result: Mapped[str] = mapped_column(String(1), nullable=False)
    points: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    shots: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    shots_on_target: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    xg: Mapped[float] = mapped_column(Double, nullable=False)
    xg_against: Mapped[float] = mapped_column(Double, nullable=False)

    match: Mapped[Match] = relationship(back_populates="team_views")
    shot_events: Mapped[list[Shot]] = relationship(viewonly=True)

    __table_args__ = (
        UniqueConstraint("match_id", "is_home"),
        CheckConstraint("team_id <> opponent_id", name="distinct_teams"),
        CheckConstraint("goals_for >= 0 AND goals_against >= 0", name="non_negative_goals"),
        CheckConstraint("shots >= 0 AND shots_on_target BETWEEN 0 AND shots", name="shot_counts"),
        CheckConstraint("xg >= 0 AND xg_against >= 0", name="non_negative_xg"),
        CheckConstraint(
            "(result = 'W' AND points = 3 AND goals_for > goals_against)"
            " OR (result = 'D' AND points = 1 AND goals_for = goals_against)"
            " OR (result = 'L' AND points = 0 AND goals_for < goals_against)",
            name="result_consistency",
        ),
        Index(None, "team_id", "match_date", "match_id"),
    )


class Shot(Base):
    __tablename__ = "shots"

    shot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    match_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("matches.match_id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    player_id: Mapped[int] = mapped_column(Integer, nullable=False)
    player_name: Mapped[str] = mapped_column(String(200), nullable=False)
    event_index: Mapped[int] = mapped_column(Integer, nullable=False)
    period: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    minute: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    second: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    location_x: Mapped[float] = mapped_column(Double, nullable=False)
    location_y: Mapped[float] = mapped_column(Double, nullable=False)
    end_location_x: Mapped[float | None] = mapped_column(Double)
    end_location_y: Mapped[float | None] = mapped_column(Double)
    statsbomb_xg: Mapped[float] = mapped_column(Double, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    body_part: Mapped[str] = mapped_column(String(32), nullable=False)
    technique: Mapped[str] = mapped_column(String(32), nullable=False)
    shot_type: Mapped[str] = mapped_column(String(32), nullable=False)
    play_pattern: Mapped[str] = mapped_column(String(64), nullable=False)
    under_pressure: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    first_time: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_goal: Mapped[bool] = mapped_column(Boolean, Computed("outcome = 'Goal'", persisted=True))

    match: Mapped[Match] = relationship(back_populates="shots")

    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id", "team_id"],
            ["team_match_views.match_id", "team_match_views.team_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("match_id", "event_index"),
        CheckConstraint("period BETWEEN 1 AND 5", name="period_range"),
        CheckConstraint("minute >= 0 AND second BETWEEN 0 AND 59", name="clock_range"),
        # StatsBomb pitch coordinates: 120 x 80 yards.
        CheckConstraint(
            "location_x BETWEEN 0 AND 120 AND location_y BETWEEN 0 AND 80", name="location_on_pitch"
        ),
        CheckConstraint("statsbomb_xg >= 0 AND statsbomb_xg <= 1", name="xg_probability"),
        Index(None, "match_id", "team_id"),
        Index(None, "team_id", "body_part"),
    )
