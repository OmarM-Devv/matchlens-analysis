-- MatchLens PostgreSQL schema.
--
-- Three tables and one view:
--
--     teams             one row per club (StatsBomb team_id is the natural key)
--     matches           one row per fixture, official score only
--     shots             one row per StatsBomb shot event
--     team_match_view   every match seen from each side (two rows per match)
--
-- The file is idempotent: the loader applies it before every import. It does
-- not migrate the pre-refactor layout (team_match_views table, team names on
-- matches); the loader refuses to run against that layout instead.

CREATE TABLE IF NOT EXISTS teams (
    team_id   integer      PRIMARY KEY,
    team_name varchar(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    match_id       bigint       PRIMARY KEY,
    competition_id integer      NOT NULL,
    season_id      integer      NOT NULL,
    season_name    varchar(32)  NOT NULL,
    match_date     date         NOT NULL,
    kick_off       time,
    match_week     smallint,
    home_team_id   integer      NOT NULL REFERENCES teams (team_id),
    away_team_id   integer      NOT NULL REFERENCES teams (team_id),
    home_score     smallint     NOT NULL,
    away_score     smallint     NOT NULL,
    stadium_name   varchar(200),
    referee_name   varchar(200),
    loaded_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ck_matches_distinct_teams     CHECK (home_team_id <> away_team_id),
    CONSTRAINT ck_matches_non_negative_score CHECK (home_score >= 0 AND away_score >= 0),
    CONSTRAINT ck_matches_match_week_range   CHECK (match_week IS NULL OR match_week BETWEEN 1 AND 60)
);

CREATE INDEX IF NOT EXISTS ix_matches_season      ON matches (competition_id, season_id, match_date);
CREATE INDEX IF NOT EXISTS ix_matches_home_team   ON matches (home_team_id, match_date);
CREATE INDEX IF NOT EXISTS ix_matches_away_team   ON matches (away_team_id, match_date);

CREATE TABLE IF NOT EXISTS shots (
    shot_id        uuid             PRIMARY KEY,
    match_id       bigint           NOT NULL REFERENCES matches (match_id) ON DELETE CASCADE,
    team_id        integer          NOT NULL REFERENCES teams (team_id),
    player_id      integer          NOT NULL,
    player_name    varchar(200)     NOT NULL,
    event_index    integer          NOT NULL,
    period         smallint         NOT NULL,
    minute         smallint         NOT NULL,
    second         smallint         NOT NULL,
    location_x     double precision NOT NULL,
    location_y     double precision NOT NULL,
    end_location_x double precision,
    end_location_y double precision,
    statsbomb_xg   double precision NOT NULL,
    outcome        varchar(32)      NOT NULL,
    body_part      varchar(32)      NOT NULL,
    technique      varchar(32)      NOT NULL,
    shot_type      varchar(32)      NOT NULL,
    play_pattern   varchar(64)      NOT NULL,
    under_pressure boolean          NOT NULL DEFAULT false,
    first_time     boolean          NOT NULL DEFAULT false,
    is_goal        boolean          GENERATED ALWAYS AS (outcome = 'Goal') STORED,
    is_on_target   boolean          GENERATED ALWAYS AS (outcome IN ('Goal', 'Saved', 'Saved to Post')) STORED,
    CONSTRAINT uq_shots_match_event        UNIQUE (match_id, event_index),
    CONSTRAINT ck_shots_period_range       CHECK (period BETWEEN 1 AND 5),
    CONSTRAINT ck_shots_clock_range        CHECK (minute >= 0 AND second BETWEEN 0 AND 59),
    -- StatsBomb pitch coordinates: 120 x 80 yards.
    CONSTRAINT ck_shots_location_on_pitch  CHECK (location_x BETWEEN 0 AND 120 AND location_y BETWEEN 0 AND 80),
    CONSTRAINT ck_shots_xg_probability     CHECK (statsbomb_xg >= 0 AND statsbomb_xg <= 1)
);

-- Covers the per-(match, team) shot aggregation every analytical query starts with.
CREATE INDEX IF NOT EXISTS ix_shots_match_team
    ON shots (match_id, team_id) INCLUDE (statsbomb_xg, is_goal, is_on_target);

-- A shot may only be credited to one of the two teams in its match. A foreign
-- key cannot express "home or away team", so a trigger enforces it.
CREATE OR REPLACE FUNCTION shots_team_in_match() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM matches AS m
        WHERE m.match_id = NEW.match_id
          AND NEW.team_id IN (m.home_team_id, m.away_team_id)
    ) THEN
        RAISE EXCEPTION 'shot % credited to team %, which did not play match %',
                        NEW.shot_id, NEW.team_id, NEW.match_id
            USING ERRCODE = 'foreign_key_violation', CONSTRAINT = 'shots_team_in_match';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_shots_team_in_match
    BEFORE INSERT OR UPDATE OF match_id, team_id ON shots
    FOR EACH ROW EXECUTE FUNCTION shots_team_in_match();

-- Team perspective: each match twice, once per side. Goals, result and points
-- come from the official score only; shot figures are joined in by the queries
-- after being aggregated per (match, team).
CREATE OR REPLACE VIEW team_match_view AS
SELECT
    side.match_id,
    side.competition_id,
    side.season_id,
    side.match_date,
    side.match_week,
    side.team_id,
    t.team_name,
    side.opponent_id,
    o.team_name AS opponent_name,
    side.is_home,
    side.goals_for,
    side.goals_against,
    CASE
        WHEN side.goals_for > side.goals_against THEN 'W'
        WHEN side.goals_for = side.goals_against THEN 'D'
        ELSE 'L'
    END AS result,
    CASE
        WHEN side.goals_for > side.goals_against THEN 3
        WHEN side.goals_for = side.goals_against THEN 1
        ELSE 0
    END AS points
FROM (
    SELECT m.match_id, m.competition_id, m.season_id, m.match_date, m.match_week,
           m.home_team_id AS team_id, m.away_team_id AS opponent_id, true AS is_home,
           m.home_score AS goals_for, m.away_score AS goals_against
    FROM matches AS m
    UNION ALL
    SELECT m.match_id, m.competition_id, m.season_id, m.match_date, m.match_week,
           m.away_team_id, m.home_team_id, false,
           m.away_score, m.home_score
    FROM matches AS m
) AS side
JOIN teams AS t ON t.team_id = side.team_id
JOIN teams AS o ON o.team_id = side.opponent_id;
