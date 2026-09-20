-- ============================================================
--  Migration: add defensive sacks support
--  Run this against an EXISTING database (the init/ scripts only run
--  automatically on a brand-new, empty Postgres data volume).
--
--  After running this, re-run:
--      python import_data.py --seasons <the seasons you want backfilled>
--  to populate def_sacks for defensive players (import_data.py now keeps
--  defensive positions instead of filtering them out, and pulls def_sacks
--  from nflreadpy's load_player_stats(), which already includes defensive
--  box-score stats computed via nflfastR::calculate_player_stats_def()).
-- ============================================================

ALTER TABLE player_stats_weekly   ADD COLUMN IF NOT EXISTS def_sacks REAL;
ALTER TABLE player_stats_seasonal ADD COLUMN IF NOT EXISTS def_sacks REAL;

CREATE OR REPLACE VIEW trivia_player_seasons AS
SELECT
    p.player_id,
    p.display_name,
    p.first_name,
    p.last_name,
    p.position,
    p.college,
    p.birth_date,
    p.headshot_url,
    s.season,
    s.team,
    s.games_played,
    -- Passing
    s.passing_yards,
    s.passing_tds,
    s.interceptions,
    s.completions,
    s.attempts,
    -- Rushing
    s.rushing_yards,
    s.rushing_tds,
    s.carries,
    -- Receiving
    s.receiving_yards,
    s.receiving_tds,
    s.receptions,
    s.targets,
    -- Defense
    s.def_sacks,
    -- Fantasy scores (all three formats)
    std.fantasy_points  AS fantasy_pts_standard,
    std.fantasy_ppg     AS fantasy_ppg_standard,
    std.position_rank   AS rank_standard,
    hppr.fantasy_points AS fantasy_pts_half_ppr,
    hppr.position_rank  AS rank_half_ppr,
    ppr.fantasy_points  AS fantasy_pts_ppr,
    ppr.position_rank   AS rank_ppr
FROM players p
JOIN player_stats_seasonal s   ON p.player_id = s.player_id
LEFT JOIN fantasy_scores_seasonal std  ON p.player_id = std.player_id AND s.season = std.season AND std.format_id = 'standard'  AND std.season_type = 'REG'
LEFT JOIN fantasy_scores_seasonal hppr ON p.player_id = hppr.player_id AND s.season = hppr.season AND hppr.format_id = 'half_ppr' AND hppr.season_type = 'REG'
LEFT JOIN fantasy_scores_seasonal ppr  ON p.player_id = ppr.player_id  AND s.season = ppr.season  AND ppr.format_id = 'ppr'      AND ppr.season_type = 'REG'
WHERE s.season_type = 'REG';
