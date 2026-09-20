#!/usr/bin/env python3
"""
Fantasy Football DB Importer
Pulls NFL data from nflverse via nflreadpy and loads into PostgreSQL.
"""

import argparse
import sys
import logging
from datetime import datetime

import psycopg2
import psycopg2.extras
import nflreadpy as nfl

TEAM_ABBR_MAP = {
    "CLV": "CLE",
    "BLT": "BAL",
    "HST": "HOU",
    "OAK": "LV",
    "LVR": "LV",
    "SD": "LAC",
    "STL": "LA",
    "SL": "LA",
    "JAC": "JAX",
    "ARZ": "ARI",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "fantasy_football",
    "user": "fantasy_admin",
    "password": "changeme_secure_password",
}

SCORING_FORMATS = {
    "standard": dict(pass_yd=0.04, pass_td=4, pass_int=-2, pass_2pt=2,
                     rush_yd=0.1, rush_td=6, rush_2pt=2,
                     rec_yd=0.1, rec_td=6, rec_bonus=0.0, rec_2pt=2,
                     fumble=-2, st_td=6),
    "half_ppr": dict(pass_yd=0.04, pass_td=4, pass_int=-2, pass_2pt=2,
                     rush_yd=0.1, rush_td=6, rush_2pt=2,
                     rec_yd=0.1, rec_td=6, rec_bonus=0.5, rec_2pt=2,
                     fumble=-2, st_td=6),
    "ppr": dict(pass_yd=0.04, pass_td=4, pass_int=-2, pass_2pt=2,
                rush_yd=0.1, rush_td=6, rush_2pt=2,
                rec_yd=0.1, rec_td=6, rec_bonus=1.0, rec_2pt=2,
                fumble=-2, st_td=6),
}

# Defensive position codes as labeled by nflverse. These players carry no
# offensive fantasy scoring, but load_player_stats() now returns their
# defensive box-score stats (def_sacks, def_tackles, etc.) in the same file
# as offense/kicking, computed via nflfastR::calculate_player_stats_def().
# Importing them lets trivia games (e.g. Triple Threat) query real sack
# totals instead of treating defense as entirely out of scope.
DEFENSIVE_POSITIONS = {
    "DE", "DT", "LB", "CB", "S", "NT", "OLB", "ILB", "MLB", "FS", "SS", "EDGE", "DL", "DB",
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def safe_int(val):
    try:
        if val is None or (isinstance(val, float) and val != val):
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def safe_float(val):
    try:
        if val is None or (isinstance(val, float) and val != val):
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def safe_str(val, maxlen=None):
    if val is None:
        return None
    s = str(val).strip()
    if s in ("", "nan", "None"):
        return None
    if maxlen:
        s = s[:maxlen]
    return s


def normalize_team_abbr(team):
    team = safe_str(team, 10)
    if not team:
        return None
    return TEAM_ABBR_MAP.get(team, team)


def get_existing_player_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM players")
        return {row[0] for row in cur.fetchall()}


def import_teams(conn):
    log.info("Importing teams …")
    try:
        df = nfl.load_teams().to_pandas()
    except Exception as exc:
        log.error("Failed to load teams: %s", exc)
        return

    rows = []
    for _, r in df.iterrows():
        rows.append((
            safe_str(r.get("team_abbr"), 10),
            safe_str(r.get("team_name"), 100),
            safe_str(r.get("team_nick"), 50),
            safe_str(r.get("team_conf"), 10),
            safe_str(r.get("team_division"), 20),
            safe_str(r.get("team_color"), 10),
            safe_str(r.get("team_color2"), 10),
            safe_str(r.get("team_logo_espn")),
        ))

    rows = [r for r in rows if r[0]]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO teams
                (team_abbr, team_name, team_nick, team_conf,
                 team_division, team_color, team_color2, team_logo_url)
            VALUES %s
            ON CONFLICT (team_abbr) DO UPDATE SET
                team_name     = EXCLUDED.team_name,
                team_nick     = EXCLUDED.team_nick,
                team_conf     = EXCLUDED.team_conf,
                team_division = EXCLUDED.team_division,
                team_color    = EXCLUDED.team_color,
                team_color2   = EXCLUDED.team_color2,
                team_logo_url = EXCLUDED.team_logo_url
        """, rows)
    conn.commit()
    log.info("  → %d teams upserted", len(rows))


def import_players(conn):
    log.info("Importing players (all-time) …")
    try:
        df = nfl.load_players().to_pandas()
    except Exception as exc:
        log.error("Failed to load players: %s", exc)
        return

    rows = []
    for _, r in df.iterrows():
        pid = safe_str(r.get("gsis_id"))
        if not pid:
            continue
        rows.append((
            pid,
            safe_str(r.get("display_name"), 150) or "Unknown",
            safe_str(r.get("first_name"), 100),
            safe_str(r.get("last_name"), 100),
            safe_str(r.get("position"), 20),
            safe_str(r.get("position_group"), 20),
            safe_str(r.get("birth_date")),
            safe_str(r.get("college_name"), 150),
            safe_int(r.get("height")),
            safe_int(r.get("weight")),
            safe_int(r.get("years_exp")),
            safe_int(r.get("entry_year")),
            safe_int(r.get("rookie_year")),
            safe_str(r.get("draft_club"), 10),
            safe_int(r.get("draft_number")),
            safe_str(r.get("status"), 30),
            safe_str(r.get("headshot")),
            safe_str(r.get("esb_id"), 20),
            safe_str(r.get("espn_id"), 20),
            safe_str(r.get("pfr_id"), 20),
            safe_str(r.get("yahoo_id"), 20),
            safe_str(r.get("rotowire_id"), 20),
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO players
                (player_id, display_name, first_name, last_name,
                 position, position_group, birth_date, college,
                 height, weight, years_exp, entry_year, rookie_year,
                 draft_club, draft_number, status, headshot_url,
                 esb_id, espn_id, pfr_id, yahoo_id, rotowire_id)
            VALUES %s
            ON CONFLICT (player_id) DO UPDATE SET
                display_name  = EXCLUDED.display_name,
                first_name    = EXCLUDED.first_name,
                last_name     = EXCLUDED.last_name,
                position      = EXCLUDED.position,
                status        = EXCLUDED.status,
                headshot_url  = EXCLUDED.headshot_url,
                updated_at    = now()
        """, rows, page_size=1000)
    conn.commit()
    log.info("  → %d players upserted", len(rows))


def import_rosters(conn, seasons):
    log.info("Importing rosters for %d seasons …", len(seasons))
    try:
        df = nfl.load_rosters(seasons=seasons).to_pandas()
    except Exception as exc:
        log.error("Failed to load rosters: %s", exc)
        return

    dedup = {}
    for _, r in df.iterrows():
        season = safe_int(r.get("season"))
        pid = safe_str(r.get("gsis_id"))
        team = normalize_team_abbr(r.get("team"))
        if not season or not pid or not team:
            continue
        key = (season, pid, team)
        dedup[key] = (
            season,
            pid,
            team,
            safe_str(r.get("position"), 20),
            safe_str(r.get("depth_chart_position"), 20),
            safe_int(r.get("jersey_number")),
            safe_str(r.get("status"), 30),
        )

    raw_count = len(df)
    rows = list(dedup.values())
    log.info("  Deduplicated rosters: %d -> %d", raw_count, len(rows))

    existing_player_ids = get_existing_player_ids(conn)
    missing_ids = sorted({r[1] for r in rows if r[1] not in existing_player_ids})
    if missing_ids:
        log.warning("  Skipping %d roster rows with missing player IDs", len(missing_ids))
        log.warning("  Sample missing IDs: %s", missing_ids[:10])
    before_filter = len(rows)
    rows = [r for r in rows if r[1] in existing_player_ids]
    log.info("  Filtered roster rows by known players: %d -> %d", before_filter, len(rows))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO rosters
                (season, player_id, team, position,
                 depth_chart_pos, jersey_number, status)
            VALUES %s
            ON CONFLICT (season, player_id, team) DO UPDATE SET
                position        = EXCLUDED.position,
                depth_chart_pos = EXCLUDED.depth_chart_pos,
                jersey_number   = EXCLUDED.jersey_number,
                status          = EXCLUDED.status
        """, rows, page_size=1000)

    conn.commit()
    log.info("  → %d roster entries upserted", len(rows))


def import_weekly_stats(conn, seasons):
    log.info("Importing weekly stats for seasons: %s …", seasons)
    try:
        df = nfl.load_player_stats(seasons=seasons).to_pandas()
    except Exception as exc:
        log.error("Failed to load weekly stats: %s", exc)
        return

    # nflreadpy's load_player_stats() now returns offense, defense, and
    # kicking stats in one combined file (the old stat_type="defense" split
    # is deprecated). Keep both the existing offensive skill positions AND
    # defensive positions so def_sacks (and future defensive columns) can be
    # captured for defensive players instead of being filtered out entirely.
    fantasy_positions = {"QB", "RB", "WR", "TE", "FB", "HB"}
    keep_positions = fantasy_positions | DEFENSIVE_POSITIONS
    if "position" in df.columns:
        df = df[df["position"].isin(keep_positions)]

    log.info("  Processing %d weekly rows …", len(df))
    existing_player_ids = get_existing_player_ids(conn)
    rows = []
    skipped_missing_players = 0

    for _, r in df.iterrows():
        pid = safe_str(r.get("player_id"))
        if not pid:
            continue
        if pid not in existing_player_ids:
            skipped_missing_players += 1
            continue
        rows.append((
            pid,
            safe_str(r.get("player_name"), 150),
            safe_str(r.get("player_display_name"), 150),
            safe_str(r.get("position"), 20),
            safe_str(r.get("position_group"), 20),
            safe_int(r.get("season")),
            safe_int(r.get("week")),
            safe_str(r.get("season_type", "REG"), 10),
            normalize_team_abbr(r.get("team")),
            normalize_team_abbr(r.get("opponent_team")),
            safe_int(r.get("completions")),
            safe_int(r.get("attempts")),
            safe_float(r.get("passing_yards")),
            safe_int(r.get("passing_tds")),
            safe_int(r.get("interceptions")),
            safe_int(r.get("sacks")),
            safe_float(r.get("sack_yards")),
            safe_int(r.get("sack_fumbles")),
            safe_int(r.get("sack_fumbles_lost")),
            safe_float(r.get("passing_air_yards")),
            safe_float(r.get("passing_yards_after_catch")),
            safe_int(r.get("passing_first_downs")),
            safe_float(r.get("passing_epa")),
            safe_int(r.get("passing_2pt_conversions")),
            safe_float(r.get("pacr")),
            safe_float(r.get("dakota")),
            safe_int(r.get("carries")),
            safe_float(r.get("rushing_yards")),
            safe_int(r.get("rushing_tds")),
            safe_int(r.get("rushing_fumbles")),
            safe_int(r.get("rushing_fumbles_lost")),
            safe_int(r.get("rushing_first_downs")),
            safe_float(r.get("rushing_epa")),
            safe_int(r.get("rushing_2pt_conversions")),
            safe_int(r.get("receptions")),
            safe_int(r.get("targets")),
            safe_float(r.get("receiving_yards")),
            safe_int(r.get("receiving_tds")),
            safe_int(r.get("receiving_fumbles")),
            safe_int(r.get("receiving_fumbles_lost")),
            safe_float(r.get("receiving_air_yards")),
            safe_float(r.get("receiving_yards_after_catch")),
            safe_int(r.get("receiving_first_downs")),
            safe_float(r.get("receiving_epa")),
            safe_int(r.get("receiving_2pt_conversions")),
            safe_float(r.get("racr")),
            safe_float(r.get("target_share")),
            safe_float(r.get("air_yards_share")),
            safe_float(r.get("wopr")),
            safe_int(r.get("special_teams_tds")),
            safe_float(r.get("def_sacks")),
        ))

    rows = [r for r in rows if r[0] and r[5] and r[6]]
    log.info("  Skipped weekly rows with unknown player IDs: %d", skipped_missing_players)

    dedup = {}
    for r in rows:
        key = (r[0], r[5], r[6], r[7])
        dedup[key] = r
    rows = list(dedup.values())
    log.info("  Weekly rows after deduplication: %d", len(rows))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO player_stats_weekly (
                player_id, player_name, player_display_name, position,
                position_group, season, week, season_type, team, opponent_team,
                completions, attempts, passing_yards, passing_tds,
                interceptions, sacks, sack_yards, sack_fumbles,
                sack_fumbles_lost, passing_air_yards, passing_yards_after_catch,
                passing_first_downs, passing_epa, passing_2pt_conversions,
                pacr, dakota,
                carries, rushing_yards, rushing_tds, rushing_fumbles,
                rushing_fumbles_lost, rushing_first_downs, rushing_epa,
                rushing_2pt_conversions,
                receptions, targets, receiving_yards, receiving_tds,
                receiving_fumbles, receiving_fumbles_lost, receiving_air_yards,
                receiving_yards_after_catch, receiving_first_downs,
                receiving_epa, receiving_2pt_conversions,
                racr, target_share, air_yards_share, wopr, special_teams_tds,
                def_sacks
            ) VALUES %s
            ON CONFLICT (player_id, season, week, season_type) DO UPDATE SET
                player_name      = EXCLUDED.player_name,
                player_display_name = EXCLUDED.player_display_name,
                position         = EXCLUDED.position,
                position_group   = EXCLUDED.position_group,
                team             = EXCLUDED.team,
                opponent_team    = EXCLUDED.opponent_team,
                completions      = EXCLUDED.completions,
                attempts         = EXCLUDED.attempts,
                passing_yards    = EXCLUDED.passing_yards,
                passing_tds      = EXCLUDED.passing_tds,
                interceptions    = EXCLUDED.interceptions,
                sacks            = EXCLUDED.sacks,
                sack_yards       = EXCLUDED.sack_yards,
                sack_fumbles     = EXCLUDED.sack_fumbles,
                sack_fumbles_lost = EXCLUDED.sack_fumbles_lost,
                passing_air_yards = EXCLUDED.passing_air_yards,
                passing_yards_after_catch = EXCLUDED.passing_yards_after_catch,
                passing_first_downs = EXCLUDED.passing_first_downs,
                passing_epa      = EXCLUDED.passing_epa,
                passing_2pt_conversions = EXCLUDED.passing_2pt_conversions,
                pacr             = EXCLUDED.pacr,
                dakota           = EXCLUDED.dakota,
                carries          = EXCLUDED.carries,
                rushing_yards    = EXCLUDED.rushing_yards,
                rushing_tds      = EXCLUDED.rushing_tds,
                rushing_fumbles  = EXCLUDED.rushing_fumbles,
                rushing_fumbles_lost = EXCLUDED.rushing_fumbles_lost,
                rushing_first_downs = EXCLUDED.rushing_first_downs,
                rushing_epa      = EXCLUDED.rushing_epa,
                rushing_2pt_conversions = EXCLUDED.rushing_2pt_conversions,
                receptions       = EXCLUDED.receptions,
                targets          = EXCLUDED.targets,
                receiving_yards  = EXCLUDED.receiving_yards,
                receiving_tds    = EXCLUDED.receiving_tds,
                receiving_fumbles = EXCLUDED.receiving_fumbles,
                receiving_fumbles_lost = EXCLUDED.receiving_fumbles_lost,
                receiving_air_yards = EXCLUDED.receiving_air_yards,
                receiving_yards_after_catch = EXCLUDED.receiving_yards_after_catch,
                receiving_first_downs = EXCLUDED.receiving_first_downs,
                receiving_epa    = EXCLUDED.receiving_epa,
                receiving_2pt_conversions = EXCLUDED.receiving_2pt_conversions,
                racr             = EXCLUDED.racr,
                target_share     = EXCLUDED.target_share,
                air_yards_share  = EXCLUDED.air_yards_share,
                wopr             = EXCLUDED.wopr,
                special_teams_tds = EXCLUDED.special_teams_tds,
                def_sacks        = EXCLUDED.def_sacks
        """, rows, page_size=500)
    conn.commit()
    log.info("  → %d weekly stat rows upserted", len(rows))


def build_seasonal_stats(conn, seasons):
    log.info("Building seasonal stats for seasons: %s …", seasons)
    season_filter = ",".join(str(s) for s in seasons)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO player_stats_seasonal (
                player_id, player_name, player_display_name,
                position, position_group, season, season_type, team,
                games_played,
                completions, attempts, passing_yards, passing_tds,
                interceptions, sacks, passing_2pt_conversions,
                carries, rushing_yards, rushing_tds,
                rushing_fumbles_lost, rushing_2pt_conversions,
                receptions, targets, receiving_yards, receiving_tds,
                receiving_fumbles_lost, receiving_2pt_conversions,
                special_teams_tds, def_sacks
            )
            SELECT
                player_id,
                MAX(player_name),
                MAX(player_display_name),
                MAX(position),
                MAX(position_group),
                season,
                season_type,
                (ARRAY_AGG(team ORDER BY week DESC))[1],
                COUNT(DISTINCT week),
                COALESCE(SUM(completions),0),
                COALESCE(SUM(attempts),0),
                COALESCE(SUM(passing_yards),0),
                COALESCE(SUM(passing_tds),0),
                COALESCE(SUM(interceptions),0),
                COALESCE(SUM(sacks),0),
                COALESCE(SUM(passing_2pt_conversions),0),
                COALESCE(SUM(carries),0),
                COALESCE(SUM(rushing_yards),0),
                COALESCE(SUM(rushing_tds),0),
                COALESCE(SUM(rushing_fumbles_lost),0),
                COALESCE(SUM(rushing_2pt_conversions),0),
                COALESCE(SUM(receptions),0),
                COALESCE(SUM(targets),0),
                COALESCE(SUM(receiving_yards),0),
                COALESCE(SUM(receiving_tds),0),
                COALESCE(SUM(receiving_fumbles_lost),0),
                COALESCE(SUM(receiving_2pt_conversions),0),
                COALESCE(SUM(special_teams_tds),0),
                COALESCE(SUM(def_sacks),0)
            FROM player_stats_weekly
            WHERE season IN ({season_filter})
            GROUP BY player_id, season, season_type
            ON CONFLICT (player_id, season, season_type) DO UPDATE SET
                games_played          = EXCLUDED.games_played,
                player_name           = EXCLUDED.player_name,
                player_display_name   = EXCLUDED.player_display_name,
                position              = EXCLUDED.position,
                position_group        = EXCLUDED.position_group,
                team                  = EXCLUDED.team,
                completions           = EXCLUDED.completions,
                attempts              = EXCLUDED.attempts,
                passing_yards         = EXCLUDED.passing_yards,
                passing_tds           = EXCLUDED.passing_tds,
                interceptions         = EXCLUDED.interceptions,
                sacks                 = EXCLUDED.sacks,
                passing_2pt_conversions = EXCLUDED.passing_2pt_conversions,
                carries               = EXCLUDED.carries,
                rushing_yards         = EXCLUDED.rushing_yards,
                rushing_tds           = EXCLUDED.rushing_tds,
                rushing_fumbles_lost  = EXCLUDED.rushing_fumbles_lost,
                rushing_2pt_conversions = EXCLUDED.rushing_2pt_conversions,
                receptions            = EXCLUDED.receptions,
                targets               = EXCLUDED.targets,
                receiving_yards       = EXCLUDED.receiving_yards,
                receiving_tds         = EXCLUDED.receiving_tds,
                receiving_fumbles_lost = EXCLUDED.receiving_fumbles_lost,
                receiving_2pt_conversions = EXCLUDED.receiving_2pt_conversions,
                special_teams_tds     = EXCLUDED.special_teams_tds,
                def_sacks             = EXCLUDED.def_sacks
        """)
    conn.commit()
    log.info("  → seasonal aggregation complete for %s", seasons)


def calculate_fantasy_scores(conn, seasons):
    log.info("Calculating fantasy scores for seasons: %s …", seasons)
    season_filter = ",".join(str(s) for s in seasons)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT player_id, season, season_type,
                   passing_yards, passing_tds, interceptions,
                   passing_2pt_conversions,
                   rushing_yards, rushing_tds, rushing_fumbles_lost,
                   rushing_2pt_conversions,
                   receptions, receiving_yards, receiving_tds,
                   receiving_fumbles_lost, receiving_2pt_conversions,
                   special_teams_tds, games_played
            FROM player_stats_seasonal
            WHERE season IN ({season_filter})
        """)
        rows = cur.fetchall()

    score_rows = []
    for (pid, season, stype,
         pass_yd, pass_td, ints, pass_2pt,
         rush_yd, rush_td, rush_fum, rush_2pt,
         rec, rec_yd, rec_td, rec_fum, rec_2pt,
         st_td, gp) in rows:
        for fmt_id, fmt in SCORING_FORMATS.items():
            pts = (
                (pass_yd or 0) * fmt["pass_yd"] +
                (pass_td or 0) * fmt["pass_td"] +
                (ints or 0) * fmt["pass_int"] +
                (pass_2pt or 0) * fmt["pass_2pt"] +
                (rush_yd or 0) * fmt["rush_yd"] +
                (rush_td or 0) * fmt["rush_td"] +
                (rush_fum or 0) * fmt["fumble"] +
                (rush_2pt or 0) * fmt["rush_2pt"] +
                (rec or 0) * fmt["rec_bonus"] +
                (rec_yd or 0) * fmt["rec_yd"] +
                (rec_td or 0) * fmt["rec_td"] +
                (rec_fum or 0) * fmt["fumble"] +
                (rec_2pt or 0) * fmt["rec_2pt"] +
                (st_td or 0) * fmt["st_td"]
            )
            ppg = round(pts / gp, 2) if gp and gp > 0 else None
            score_rows.append((pid, season, stype, fmt_id, round(pts, 2), ppg, gp))

    if not score_rows:
        log.warning("  No fantasy scores to calculate.")
        return

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO fantasy_scores_seasonal
                (player_id, season, season_type, format_id,
                 fantasy_points, fantasy_ppg, games_played)
            VALUES %s
            ON CONFLICT (player_id, season, season_type, format_id) DO UPDATE SET
                fantasy_points = EXCLUDED.fantasy_points,
                fantasy_ppg    = EXCLUDED.fantasy_ppg,
                games_played   = EXCLUDED.games_played
        """, score_rows, page_size=500)
    conn.commit()
    log.info("  → %d fantasy score rows upserted", len(score_rows))

    log.info("  Updating position ranks …")
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE fantasy_scores_seasonal fs
            SET position_rank = ranked.rn
            FROM (
                SELECT
                    fs2.player_id, fs2.season, fs2.format_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.position, fs2.season, fs2.format_id
                        ORDER BY fs2.fantasy_points DESC
                    ) AS rn
                FROM fantasy_scores_seasonal fs2
                JOIN players p ON fs2.player_id = p.player_id
                WHERE fs2.season IN ({season_filter})
            ) ranked
            WHERE fs.player_id = ranked.player_id
              AND fs.season = ranked.season
              AND fs.format_id = ranked.format_id
        """)
    conn.commit()
    log.info("  → position ranks updated")


def main():
    parser = argparse.ArgumentParser(description="Import NFL data into PostgreSQL")
    parser.add_argument("--seasons", nargs="+", type=int, default=None,
                        help="Seasons to import. Default: 1999-current.")
    parser.add_argument("--skip-weekly", action="store_true",
                        help="Skip weekly stat import")
    parser.add_argument("--recalc-scores", action="store_true",
                        help="Only recalculate fantasy scores")
    parser.add_argument("--skip-players", action="store_true",
                        help="Skip player/roster import")
    args = parser.parse_args()

    seasons = sorted(args.seasons) if args.seasons else list(range(1999, 2026))

    log.info("=" * 60)
    log.info("Fantasy Football DB Importer")
    log.info("Seasons: %s – %s (%d total)", seasons[0], seasons[-1], len(seasons))
    log.info("=" * 60)

    conn = get_conn()
    start = datetime.now()

    try:
        if not args.recalc_scores:
            if not args.skip_players:
                import_teams(conn)
                import_players(conn)
                import_rosters(conn, seasons)
            if not args.skip_weekly:
                import_weekly_stats(conn, seasons)
            build_seasonal_stats(conn, seasons)
        calculate_fantasy_scores(conn, seasons)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()

    elapsed = datetime.now() - start
    log.info("=" * 60)
    log.info("Done in %s", str(elapsed).split(".")[0])
    log.info("=" * 60)


if __name__ == "__main__":
    main()
