-- MatchLens analytical queries.
--
-- Each query starts with a "-- name: <name>" line; app/main.py loads them by
-- name. Parameters use the :name bind style.
--
-- Every query that reports shot figures aggregates shots to one row per
-- (match, team) first and only then joins that result to team_match_view.
-- Joining raw shots to the view and summing afterwards would repeat each
-- match's goals and points once per shot.


-- name: match_report
-- One match from both sides (two rows, home first).
-- Params: :match_id
WITH shot_totals AS (
    SELECT
        s.team_id,
        COUNT(*)                                AS shots,
        COUNT(*) FILTER (WHERE s.is_on_target)  AS shots_on_target,
        COUNT(*) FILTER (WHERE s.is_goal)       AS shot_goals,
        SUM(s.statsbomb_xg)                     AS xg
    FROM shots AS s
    WHERE s.match_id = :match_id
    GROUP BY s.team_id
)
SELECT
    v.match_id,
    v.match_date,
    v.match_week,
    v.team_id,
    v.team_name,
    v.opponent_name,
    v.is_home,
    v.goals_for,
    v.goals_against,
    v.result,
    v.points,
    COALESCE(st.shots, 0)                                   AS shots,
    COALESCE(st.shots_on_target, 0)                         AS shots_on_target,
    COALESCE(st.shot_goals, 0)                              AS shot_goals,
    v.goals_for - COALESCE(st.shot_goals, 0)                AS goals_not_from_shots,
    ROUND(COALESCE(st.xg, 0)::numeric, 2)                   AS xg,
    ROUND((COALESCE(st.xg, 0) / NULLIF(st.shots, 0))::numeric, 3) AS xg_per_shot
FROM team_match_view AS v
LEFT JOIN shot_totals AS st ON st.team_id = v.team_id
WHERE v.match_id = :match_id
ORDER BY v.is_home DESC;


-- name: rolling_form
-- Rolling N-match form for one team, computed with window functions.
-- Params: :team_id, :preceding (window size minus one)
WITH team_matches AS (
    SELECT v.match_id, v.match_date, v.team_id, v.opponent_id, v.team_name, v.opponent_name,
           v.is_home, v.goals_for, v.goals_against, v.result, v.points
    FROM team_match_view AS v
    WHERE v.team_id = :team_id
),
shot_totals AS (
    SELECT s.match_id, s.team_id, SUM(s.statsbomb_xg) AS xg
    FROM shots AS s
    WHERE s.match_id IN (SELECT match_id FROM team_matches)
    GROUP BY s.match_id, s.team_id
),
per_match AS (
    SELECT
        tm.*,
        COALESCE(f.xg, 0) AS xg,
        COALESCE(a.xg, 0) AS xg_against
    FROM team_matches AS tm
    LEFT JOIN shot_totals AS f ON f.match_id = tm.match_id AND f.team_id = tm.team_id
    LEFT JOIN shot_totals AS a ON a.match_id = tm.match_id AND a.team_id = tm.opponent_id
)
SELECT
    ROW_NUMBER() OVER (ORDER BY match_date, match_id)          AS match_number,
    match_id,
    match_date,
    team_name,
    opponent_name,
    is_home,
    goals_for,
    goals_against,
    result,
    points,
    ROUND(xg::numeric, 2)                                      AS xg,
    ROUND(xg_against::numeric, 2)                              AS xg_against,
    COUNT(*)                            OVER w                 AS games_in_window,
    SUM(points)                         OVER w                 AS rolling_points,
    SUM(goals_for - goals_against)      OVER w                 AS rolling_goal_diff,
    ROUND((AVG(xg) OVER w)::numeric, 2)                        AS rolling_xg,
    ROUND((AVG(xg_against) OVER w)::numeric, 2)                AS rolling_xg_against,
    STRING_AGG(result, '')              OVER w                 AS form,
    SUM(points) OVER (ORDER BY match_date, match_id)           AS cumulative_points
FROM per_match
WINDOW w AS (
    ORDER BY match_date, match_id
    ROWS BETWEEN CAST(:preceding AS integer) PRECEDING AND CURRENT ROW
)
ORDER BY match_date, match_id;


-- name: home_away
-- Home vs away totals for one team (two rows, home first).
-- Params: :team_id
WITH team_matches AS (
    SELECT v.match_id, v.team_id, v.opponent_id, v.is_home,
           v.goals_for, v.goals_against, v.result, v.points
    FROM team_match_view AS v
    WHERE v.team_id = :team_id
),
shot_totals AS (
    SELECT
        s.match_id,
        s.team_id,
        COUNT(*)                                AS shots,
        COUNT(*) FILTER (WHERE s.is_on_target)  AS shots_on_target,
        SUM(s.statsbomb_xg)                     AS xg
    FROM shots AS s
    WHERE s.match_id IN (SELECT match_id FROM team_matches)
    GROUP BY s.match_id, s.team_id
),
per_match AS (
    SELECT
        tm.*,
        COALESCE(f.shots, 0)            AS shots,
        COALESCE(f.shots_on_target, 0)  AS shots_on_target,
        COALESCE(f.xg, 0)               AS xg,
        COALESCE(a.shots, 0)            AS shots_against,
        COALESCE(a.xg, 0)               AS xg_against
    FROM team_matches AS tm
    LEFT JOIN shot_totals AS f ON f.match_id = tm.match_id AND f.team_id = tm.team_id
    LEFT JOIN shot_totals AS a ON a.match_id = tm.match_id AND a.team_id = tm.opponent_id
)
SELECT
    CASE WHEN is_home THEN 'Home' ELSE 'Away' END             AS venue,
    COUNT(*)                                                  AS played,
    COUNT(*) FILTER (WHERE result = 'W')                      AS wins,
    COUNT(*) FILTER (WHERE result = 'D')                      AS draws,
    COUNT(*) FILTER (WHERE result = 'L')                      AS losses,
    SUM(points)::int                                          AS points,
    ROUND(AVG(points)::numeric, 2)                            AS points_per_match,
    SUM(goals_for)::int                                       AS goals_for,
    SUM(goals_against)::int                                   AS goals_against,
    SUM(shots)::int                                           AS shots,
    SUM(shots_on_target)::int                                 AS shots_on_target,
    SUM(shots_against)::int                                   AS shots_against,
    ROUND(SUM(xg)::numeric, 2)                                AS xg,
    ROUND(SUM(xg_against)::numeric, 2)                        AS xg_against,
    ROUND(AVG(xg)::numeric, 2)                                AS xg_per_match,
    ROUND(AVG(xg_against)::numeric, 2)                        AS xg_against_per_match
FROM per_match
GROUP BY is_home
ORDER BY is_home DESC;


-- name: team_matches
-- Lookup (not an analytical query): the team's fixtures, for page navigation.
-- Params: :team_id
SELECT v.match_id, v.match_date, v.match_week, v.team_name, v.opponent_name, v.is_home,
       v.goals_for, v.goals_against, v.result
FROM team_match_view AS v
WHERE v.team_id = :team_id
ORDER BY v.match_date, v.match_id;
