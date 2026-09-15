-- ============================================================
--  FANTASY FOOTBALL DATABASE SCHEMA
--  Source: nflverse / nflreadpy
--  Supports trivia prompts like "draft a QB from 1997"
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
--  TEAMS
-- ============================================================
CREATE TABLE IF NOT EXISTS teams (
    team_abbr       VARCHAR(10)  PRIMARY KEY,
    team_name       VARCHAR(100),
    team_nick       VARCHAR(50),
    team_conf       VARCHAR(10),
    team_division   VARCHAR(20),
    team_color      VARCHAR(10),
    team_color2     VARCHAR(10),
    team_logo_url   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
--  PLAYERS  (master player list — all time)
-- ============================================================
CREATE TABLE IF NOT EXISTS players (
    player_id           VARCHAR(50)  PRIMARY KEY,   -- nflverse gsis_id
    display_name        VARCHAR(150) NOT NULL,
    first_name          VARCHAR(100),
    last_name           VARCHAR(100),
    position            VARCHAR(20),                -- QB, RB, WR, TE, K, etc.
    position_group      VARCHAR(20),                -- offense / defense / special
    birth_date          DATE,
    college             VARCHAR(150),
    height              SMALLINT,                   -- inches
    weight              SMALLINT,                   -- lbs
    years_exp           SMALLINT,
    entry_year          SMALLINT,
    rookie_year         SMALLINT,
    draft_club          VARCHAR(10),
    draft_number        SMALLINT,
    status              VARCHAR(30),                -- Active, Retired, etc.
    headshot_url        TEXT,
    -- cross-reference IDs for joining other datasets
    esb_id              VARCHAR(20),
    espn_id             VARCHAR(20),
    pfr_id              VARCHAR(20),
    yahoo_id            VARCHAR(20),
    rotowire_id         VARCHAR(20),
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_players_position  ON players(position);
CREATE INDEX IF NOT EXISTS idx_players_lastname  ON players(last_name);
CREATE INDEX IF NOT EXISTS idx_players_status    ON players(status);

-- ============================================================
--  ROSTERS  (player <-> team per season)
-- ============================================================
CREATE TABLE IF NOT EXISTS rosters (
    id              SERIAL       PRIMARY KEY,
    season          SMALLINT     NOT NULL,
    player_id       VARCHAR(50)  REFERENCES players(player_id) ON DELETE CASCADE,
    team            VARCHAR(10)  REFERENCES teams(team_abbr) ON DELETE SET NULL,
    position        VARCHAR(20),
    depth_chart_pos VARCHAR(20),
    jersey_number   SMALLINT,
    status          VARCHAR(30),
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (season, player_id, team)
);

CREATE INDEX IF NOT EXISTS idx_rosters_season    ON rosters(season);
CREATE INDEX IF NOT EXISTS idx_rosters_player    ON rosters(player_id);
CREATE INDEX IF NOT EXISTS idx_rosters_team      ON rosters(team);

-- ============================================================
--  WEEKLY STATS  (offense — offense only for fantasy)
-- ============================================================
CREATE TABLE IF NOT EXISTS player_stats_weekly (
    id                      BIGSERIAL    PRIMARY KEY,
    player_id               VARCHAR(50)  NOT NULL,
    player_name             VARCHAR(150),
    player_display_name     VARCHAR(150),
    position                VARCHAR(20),
    position_group          VARCHAR(20),
    season                  SMALLINT     NOT NULL,
    week                    SMALLINT     NOT NULL,
    season_type             VARCHAR(10)  DEFAULT 'REG',   -- REG / POST
    team                    VARCHAR(10),
    opponent_team           VARCHAR(10),
    -- Passing
    completions             SMALLINT,
    attempts                SMALLINT,
    passing_yards           REAL,
    passing_tds             SMALLINT,
    interceptions           SMALLINT,
    sacks                   SMALLINT,
    sack_yards              REAL,
    sack_fumbles            SMALLINT,
    sack_fumbles_lost       SMALLINT,
    passing_air_yards       REAL,
    passing_yards_after_catch REAL,
    passing_first_downs     SMALLINT,
    passing_epa             REAL,
    passing_2pt_conversions SMALLINT,
    pacr                    REAL,
    dakota                  REAL,
    -- Rushing
    carries                 SMALLINT,
    rushing_yards           REAL,
    rushing_tds             SMALLINT,
    rushing_fumbles         SMALLINT,
    rushing_fumbles_lost    SMALLINT,
    rushing_first_downs     SMALLINT,
    rushing_epa             REAL,
    rushing_2pt_conversions SMALLINT,
    -- Receiving
    receptions              SMALLINT,
    targets                 SMALLINT,
    receiving_yards         REAL,
    receiving_tds           SMALLINT,
    receiving_fumbles       SMALLINT,
    receiving_fumbles_lost  SMALLINT,
    receiving_air_yards     REAL,
    receiving_yards_after_catch REAL,
    receiving_first_downs   SMALLINT,
    receiving_epa           REAL,
    receiving_2pt_conversions SMALLINT,
    racr                    REAL,
    target_share            REAL,
    air_yards_share         REAL,
    wopr                    REAL,
    -- Special Teams
    special_teams_tds       SMALLINT,
    created_at              TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_id, season, week, season_type)
);

CREATE INDEX IF NOT EXISTS idx_weekly_player   ON player_stats_weekly(player_id);
CREATE INDEX IF NOT EXISTS idx_weekly_season   ON player_stats_weekly(season);
CREATE INDEX IF NOT EXISTS idx_weekly_position ON player_stats_weekly(position);
CREATE INDEX IF NOT EXISTS idx_weekly_team     ON player_stats_weekly(team);

-- ============================================================
--  SEASONAL STATS  (aggregated full-season totals)
-- ============================================================
CREATE TABLE IF NOT EXISTS player_stats_seasonal (
    id                      BIGSERIAL    PRIMARY KEY,
    player_id               VARCHAR(50)  NOT NULL,
    player_name             VARCHAR(150),
    player_display_name     VARCHAR(150),
    position                VARCHAR(20),
    position_group          VARCHAR(20),
    season                  SMALLINT     NOT NULL,
    season_type             VARCHAR(10)  DEFAULT 'REG',
    team                    VARCHAR(10),
    games_played            SMALLINT,
    -- Passing
    completions             SMALLINT,
    attempts                SMALLINT,
    passing_yards           REAL,
    passing_tds             SMALLINT,
    interceptions           SMALLINT,
    sacks                   SMALLINT,
    passing_2pt_conversions SMALLINT,
    -- Rushing
    carries                 SMALLINT,
    rushing_yards           REAL,
    rushing_tds             SMALLINT,
    rushing_fumbles_lost    SMALLINT,
    rushing_2pt_conversions SMALLINT,
    -- Receiving
    receptions              SMALLINT,
    targets                 SMALLINT,
    receiving_yards         REAL,
    receiving_tds           SMALLINT,
    receiving_fumbles_lost  SMALLINT,
    receiving_2pt_conversions SMALLINT,
    -- Special Teams
    special_teams_tds       SMALLINT,
    created_at              TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_id, season, season_type)
);

CREATE INDEX IF NOT EXISTS idx_seasonal_player   ON player_stats_seasonal(player_id);
CREATE INDEX IF NOT EXISTS idx_seasonal_season   ON player_stats_seasonal(season);
CREATE INDEX IF NOT EXISTS idx_seasonal_position ON player_stats_seasonal(position);

-- ============================================================
--  FANTASY SCORING FORMATS
-- ============================================================
CREATE TABLE IF NOT EXISTS scoring_formats (
    format_id       VARCHAR(30)  PRIMARY KEY,
    format_name     VARCHAR(100) NOT NULL,
    description     TEXT,
    -- Passing
    pass_yd_per     REAL DEFAULT 0.04,     -- pts per passing yard (25 yds = 1 pt)
    pass_td         REAL DEFAULT 4.0,
    pass_int        REAL DEFAULT -2.0,
    pass_2pt        REAL DEFAULT 2.0,
    -- Rushing
    rush_yd_per     REAL DEFAULT 0.1,      -- 10 yds = 1 pt
    rush_td         REAL DEFAULT 6.0,
    rush_2pt        REAL DEFAULT 2.0,
    -- Receiving
    rec_yd_per      REAL DEFAULT 0.1,
    rec_td          REAL DEFAULT 6.0,
    reception_bonus REAL DEFAULT 0.0,      -- 0=standard, 0.5=half-ppr, 1.0=ppr
    rec_2pt         REAL DEFAULT 2.0,
    -- Misc
    fumble_lost     REAL DEFAULT -2.0,
    two_pt_conv     REAL DEFAULT 2.0,
    st_td           REAL DEFAULT 6.0,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Seed default scoring formats
INSERT INTO scoring_formats (format_id, format_name, description, reception_bonus)
VALUES
    ('standard', 'Standard', 'No reception bonus', 0.0),
    ('half_ppr', 'Half PPR', '0.5 points per reception', 0.5),
    ('ppr',      'Full PPR', '1.0 point per reception', 1.0)
ON CONFLICT DO NOTHING;

-- ============================================================
--  FANTASY SCORES  (precomputed per player/season/format)
--  Avoids recalculating on every trivia query
-- ============================================================
CREATE TABLE IF NOT EXISTS fantasy_scores_seasonal (
    id              BIGSERIAL    PRIMARY KEY,
    player_id       VARCHAR(50)  NOT NULL,
    season          SMALLINT     NOT NULL,
    season_type     VARCHAR(10)  DEFAULT 'REG',
    format_id       VARCHAR(30)  REFERENCES scoring_formats(format_id),
    fantasy_points  REAL         NOT NULL,
    fantasy_ppg     REAL,        -- points per game
    games_played    SMALLINT,
    -- rank within position for that season/format
    position_rank   SMALLINT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (player_id, season, season_type, format_id)
);

CREATE INDEX IF NOT EXISTS idx_ff_scores_player   ON fantasy_scores_seasonal(player_id);
CREATE INDEX IF NOT EXISTS idx_ff_scores_season   ON fantasy_scores_seasonal(season);
CREATE INDEX IF NOT EXISTS idx_ff_scores_format   ON fantasy_scores_seasonal(format_id);

-- ============================================================
--  CONVENIENCE VIEW: trivia_player_seasons
--  Used directly by trivia queries ("draft a QB from 1997")
-- ============================================================
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

