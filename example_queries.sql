-- ============================================================
--  EXAMPLE TRIVIA QUERIES
--  Run these against the fantasy_football DB after import
-- ============================================================

-- 1. Draft a QB from 1997 (PPR ranking)
--    Note: nflverse weekly stats start 1999.
--    For 1997/1998 use the seasonal aggregate from Pro Football Ref
--    (handled via manual CSV import — see README).

SELECT
    display_name,
    team,
    season,
    passing_yards,
    passing_tds,
    interceptions,
    fantasy_pts_ppr,
    rank_ppr
FROM trivia_player_seasons
WHERE season = 1997
  AND position = 'QB'
ORDER BY fantasy_pts_ppr DESC NULLS LAST
LIMIT 20;


-- 2. Top 10 RBs from the 2000s (Standard scoring)
SELECT
    display_name,
    season,
    team,
    rushing_yards,
    rushing_tds,
    receptions,
    fantasy_pts_standard,
    rank_standard
FROM trivia_player_seasons
WHERE season BETWEEN 2000 AND 2009
  AND position = 'RB'
ORDER BY fantasy_pts_standard DESC NULLS LAST
LIMIT 10;


-- 3. All WRs with 1000+ receiving yards in a single season
SELECT
    display_name,
    season,
    team,
    receiving_yards,
    receiving_tds,
    receptions,
    fantasy_pts_ppr,
    rank_ppr
FROM trivia_player_seasons
WHERE position = 'WR'
  AND receiving_yards >= 1000
ORDER BY season, rank_ppr;


-- 4. Trivia: "Name a TE who scored 10+ TDs in 2004"
SELECT
    display_name,
    season,
    team,
    receiving_tds,
    fantasy_pts_half_ppr
FROM trivia_player_seasons
WHERE position = 'TE'
  AND season = 2004
  AND receiving_tds >= 10
ORDER BY receiving_tds DESC;


-- 5. Build a full fantasy team from 1997 (one of each position, best available)
WITH ranked AS (
    SELECT
        display_name,
        position,
        season,
        team,
        fantasy_pts_standard,
        rank_standard,
        ROW_NUMBER() OVER (PARTITION BY position ORDER BY fantasy_pts_standard DESC NULLS LAST) AS rn
    FROM trivia_player_seasons
    WHERE season = 1997
      AND position IN ('QB','RB','WR','TE','K')
)
SELECT position, display_name, team, fantasy_pts_standard
FROM ranked
WHERE rn = 1
ORDER BY CASE position WHEN 'QB' THEN 1 WHEN 'RB' THEN 2 WHEN 'WR' THEN 3 WHEN 'TE' THEN 4 ELSE 5 END;


-- 6. Check data coverage: which seasons have player stat rows?
SELECT season, COUNT(DISTINCT player_id) AS unique_players
FROM player_stats_seasonal
WHERE season_type = 'REG'
GROUP BY season
ORDER BY season;

