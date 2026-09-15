#!/usr/bin/env python3
"""
Historical Fantasy Football Importer (1970-1998)

Downloads yearly CSV files from the fantasy-csv-data GitHub repo and loads
them into the same PostgreSQL schema used by import_data.py, so these older
seasons become fully usable by the trivia app (SEASON_STAT, CAREER_TOTAL,
STREAK, ERA, etc. rules will all pick these seasons up automatically once
loaded).

Source: https://github.com/bendominguez0111/fantasy-csv-data
        (mirror of fantasyfootballdatapros.com's dataset)

IMPORTANT CAVEATS -- read before running:

1. NO PLAYER IDs. This dataset has no gsis_id/player_id column, just plain
   player names. The script first tries to match each name+position against
   your EXISTING `players` table (in case nflreadpy already has a record for
   that player from a later career stint). If no match is found, it creates
   a synthetic player_id like "ffdp_walter_payton_rb" so the player can still
   be referenced by player_stats_seasonal / fantasy_scores_seasonal. This can
   theoretically collide for two different players who share an identical
   name AND position (rare, but possible) -- spot-check afterward if you
   care about perfect accuracy.

2. MID-SEASON TRADES. A player traded mid-season appears as TWO separate
   rows in the same year's CSV (one per team stint). These are summed into
   a single combined season total before insertion -- summing counting
   stats and games played, and keeping whichever stint had more total yards
   as the "team of record" for that season. This avoids a Postgres
   "ON CONFLICT DO UPDATE command cannot affect row a second time" error
   that occurs if two rows in the same batch target the same
   (player_id, season, season_type) key.

3. OLD TEAM CODES. This era uses Pro-Football-Reference-style abbreviations
   (RAI, SFO, GNB, KAN, NOR, NWE, SDG, TAM, PHO, etc.) which differ from
   nflverse's modern codes. A best-effort mapping table (PFR_TEAM_MAP) is
   included below, then piped through a modern-relocation map for any
   further team moves. Review the console output for anything that looks
   like a raw 3-letter code that never got normalized, and extend
   PFR_TEAM_MAP if so.

4. MISSING FIELDS. This source has no targets, no 2-point conversion counts,
   no sack data, and no special-teams-TD column. Those fields are inserted
   as 0. Fumbles-lost is attributed entirely to the rushing bucket so it's
   still counted exactly once in fantasy scoring (not double-counted), even
   though the source doesn't split rushing vs. receiving fumbles.

5. SEASON TYPE. Only regular-season totals exist in this dataset, so every
   row is inserted with season_type = 'REG', matching your existing schema.

Usage:
    python3 import_historical_1970_1998.py
    python3 import_historical_1970_1998.py --start-year 1970 --end-year 1998
    python3 import_historical_1970_1998.py --years 1985 1986 1987
"""

import argparse
import logging
import re
import sys
from datetime import datetime
from io import StringIO

import pandas as pd
import psycopg2
import psycopg2.extras
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config -- keep in sync with import_data.py ─────────────────────────────
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "fantasy_football",
    "user": "fantasy_admin",
    "password": "changeme_secure_password",
}

RAW_URL_TEMPLATE = "https://raw.githubusercontent.com/bendominguez0111/fantasy-csv-data/master/yearly/{year}.csv"

# Must match SCORING_FORMATS in import_data.py exactly so historical seasons
# score identically to modern ones.
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

# Modern-era relocations -- same as import_data.py's TEAM_ABBR_MAP
MODERN_TEAM_ABBR_MAP = {
    "CLV": "CLE", "BLT": "BAL", "HST": "HOU", "OAK": "LV", "LVR": "LV",
    "SD": "LAC", "STL": "LA", "SL": "LA", "JAC": "JAX", "ARZ": "ARI",
}

# Pro-Football-Reference historical codes -> modern nflverse abbreviations.
# Extend this if any unrecognized codes show up in your data after import.
PFR_TEAM_MAP = {
    "RAI": "LV", "OAK": "LV", "LVR": "LV",             # Raiders (Oakland/LA/Vegas)
    "SFO": "SF",                                        # 49ers
    "GNB": "GB",                                        # Packers
    "KAN": "KC",                                        # Chiefs
    "NOR": "NO",                                        # Saints
    "NWE": "NE",                                        # Patriots
    "SDG": "LAC", "SDC": "LAC",                         # Chargers (San Diego)
    "TAM": "TB",                                        # Buccaneers
    "PHO": "ARI", "CRD": "ARI",                         # Cardinals (Phoenix/St.Louis/Arizona)
    "RAM": "LA",                                        # Rams (LA/St.Louis/LA)
    "CLT": "IND",                                       # Colts (Baltimore/Indianapolis)
    "HOI": "TEN", "OTI": "TEN",                         # Oilers/Titans
    "WSH": "WAS",
}

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE"}
POSITION_ALIASES = {"HB": "RB", "FB": "RB", "WR/RB": "WR", "WR/TE": "WR"}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def safe_int(val):
    try:
        if val is None or (isinstance(val, float) and val != val):
            return 0
        return int(float(val))
    except (TypeError, ValueError):
        return 0


def safe_float(val):
    try:
        if val is None or (isinstance(val, float) and val != val):
            return 0.0
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", "_", s)
    return s


def normalize_team(raw_team) -> str:
    raw_team = str(raw_team or "").strip().upper()
    mapped = PFR_TEAM_MAP.get(raw_team, raw_team)
    mapped = MODERN_TEAM_ABBR_MAP.get(mapped, mapped)
    return mapped


def normalize_position(raw_pos) -> str:
    raw_pos = str(raw_pos or "").strip().upper()
    return POSITION_ALIASES.get(raw_pos, raw_pos)


def download_year_csv(year: int) -> pd.DataFrame:
    url = RAW_URL_TEMPLATE.format(year=year)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    return df


def get_existing_player_lookup(conn):
    """
    Maps (upper display_name, position) -> player_id for players already in
    the database, so we reuse real gsis_ids where a match exists instead of
    creating unnecessary synthetic ones.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT player_id, display_name, position FROM players")
        rows = cur.fetchall()
    lookup = {}
    for player_id, display_name, position in rows:
        key = (display_name.strip().upper(), (position or "").strip().upper())
        lookup[key] = player_id
    return lookup


def ensure_synthetic_player(conn, cur, name: str, position: str, synthetic_cache: dict) -> str:
    """
    Creates (or reuses) a synthetic player_id for a player with no existing
    gsis_id record, and ensures a row exists in `players` for it.
    """
    slug_key = f"ffdp_{slugify(name)}_{position.lower()}"
    if slug_key in synthetic_cache:
        return slug_key

    name_parts = name.split(" ")
    first_name = name_parts[0]
    last_name = " ".join(name_parts[1:]) or name

    cur.execute(
        """
        INSERT INTO players (player_id, display_name, first_name, last_name, position, status)
        VALUES (%s, %s, %s, %s, %s, 'HISTORICAL')
        ON CONFLICT (player_id) DO NOTHING
        """,
        (slug_key, name, first_name, last_name, position),
    )
    synthetic_cache[slug_key] = True
    return slug_key


def resolve_player_id(conn, cur, existing_lookup: dict, synthetic_cache: dict, name: str, position: str) -> str:
    key = (name.strip().upper(), position.strip().upper())
    if key in existing_lookup:
        return existing_lookup[key]
    return ensure_synthetic_player(conn, cur, name, position, synthetic_cache)


def import_year(conn, year: int, existing_lookup: dict, synthetic_cache: dict) -> int:
    log.info("Downloading %d ...", year)
    try:
        df = download_year_csv(year)
    except Exception as exc:
        log.error("  Failed to download/parse %d: %s", year, exc)
        return 0

    df = df[df["Player"].notna()]
    skipped = 0
    multi_team_merges = 0

    # Aggregate by player_id FIRST, since some players (traded mid-season)
    # appear multiple times in the same year's file -- one row per team
    # stint. Summing here avoids inserting two rows with the same
    # (player_id, season, season_type) key in one batch, which Postgres
    # rejects with "ON CONFLICT DO UPDATE command cannot affect row a
    # second time".
    aggregated = {}  # player_id -> combined stat dict

    with conn.cursor() as cur:
        for _, r in df.iterrows():
            name = str(r.get("Player", "")).strip()
            position = normalize_position(r.get("Pos"))
            if not name or position not in FANTASY_POSITIONS:
                skipped += 1
                continue

            team = normalize_team(r.get("Tm"))
            player_id = resolve_player_id(conn, cur, existing_lookup, synthetic_cache, name, position)

            stint = {
                "games_played": safe_int(r.get("G")),
                "completions": safe_int(r.get("Cmp")),
                "passing_attempts": safe_int(r.get("PassingAtt")),
                "passing_yards": safe_float(r.get("PassingYds")),
                "passing_tds": safe_int(r.get("PassingTD")),
                "interceptions": safe_int(r.get("Int")),
                "carries": safe_int(r.get("RushingAtt")),
                "rushing_yards": safe_float(r.get("RushingYds")),
                "rushing_tds": safe_int(r.get("RushingTD")),
                "receptions": safe_int(r.get("Rec")),
                "receiving_yards": safe_float(r.get("ReceivingYds")),
                "receiving_tds": safe_int(r.get("ReceivingTD")),
                "fumbles_lost": safe_int(r.get("FumblesLost")),
            }

            if player_id not in aggregated:
                aggregated[player_id] = {
                    "name": name, "position": position, "team": team,
                    "_primary_yards": stint["passing_yards"] + stint["rushing_yards"] + stint["receiving_yards"],
                    **stint,
                }
            else:
                multi_team_merges += 1
                existing = aggregated[player_id]
                for key in stint:
                    existing[key] += stint[key]
                this_yards = stint["passing_yards"] + stint["rushing_yards"] + stint["receiving_yards"]
                if this_yards > existing["_primary_yards"]:
                    existing["team"] = team
                    existing["_primary_yards"] = this_yards

    rows = []
    for player_id, s in aggregated.items():
        rows.append((
            player_id, s["name"], s["name"], s["position"], s["position"], year, "REG", s["team"],
            s["games_played"],
            s["completions"], s["passing_attempts"], s["passing_yards"], s["passing_tds"], s["interceptions"],
            0,  # sacks -- not available
            0,  # passing_2pt_conversions -- not available
            s["carries"], s["rushing_yards"], s["rushing_tds"],
            s["fumbles_lost"],  # attributed fully to rushing bucket -- see caveats
            0,  # rushing_2pt_conversions -- not available
            s["receptions"], 0,  # targets -- not available
            s["receiving_yards"], s["receiving_tds"],
            0,  # receiving_fumbles_lost -- attributed to rushing above
            0,  # receiving_2pt_conversions -- not available
            0,  # special_teams_tds -- not available
        ))

    if not rows:
        log.warning("  No usable rows found for %d", year)
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO player_stats_seasonal (
                player_id, player_name, player_display_name, position, position_group,
                season, season_type, team, games_played,
                completions, attempts, passing_yards, passing_tds, interceptions,
                sacks, passing_2pt_conversions,
                carries, rushing_yards, rushing_tds, rushing_fumbles_lost, rushing_2pt_conversions,
                receptions, targets, receiving_yards, receiving_tds,
                receiving_fumbles_lost, receiving_2pt_conversions, special_teams_tds
            ) VALUES %s
            ON CONFLICT (player_id, season, season_type) DO UPDATE SET
                team = EXCLUDED.team,
                games_played = EXCLUDED.games_played,
                completions = EXCLUDED.completions,
                attempts = EXCLUDED.attempts,
                passing_yards = EXCLUDED.passing_yards,
                passing_tds = EXCLUDED.passing_tds,
                interceptions = EXCLUDED.interceptions,
                carries = EXCLUDED.carries,
                rushing_yards = EXCLUDED.rushing_yards,
                rushing_tds = EXCLUDED.rushing_tds,
                rushing_fumbles_lost = EXCLUDED.rushing_fumbles_lost,
                receptions = EXCLUDED.receptions,
                receiving_yards = EXCLUDED.receiving_yards,
                receiving_tds = EXCLUDED.receiving_tds
        """, rows, page_size=500)
    conn.commit()
    log.info("  -> %d rows upserted for %d (%d skipped, %d multi-team stints merged)",
              len(rows), year, skipped, multi_team_merges)
    return len(rows)


def calculate_fantasy_scores_for_years(conn, years: list):
    log.info("Calculating fantasy scores for %s ...", years)
    season_filter = ",".join(str(y) for y in years)

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
    log.info("  -> %d fantasy score rows upserted", len(score_rows))

    log.info("  Updating position ranks for these seasons ...")
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
    log.info("  -> position ranks updated")


def try_refresh_materialized_view(conn):
    """
    If trivia_player_seasons is a materialized view, it needs a manual
    refresh to pick up the new rows. If it's a normal view, this is a no-op
    that will simply fail harmlessly (caught below).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW trivia_player_seasons")
        conn.commit()
        log.info("Refreshed materialized view trivia_player_seasons")
    except Exception:
        conn.rollback()
        log.info("trivia_player_seasons is not a materialized view (or refresh not needed) -- skipping")


def main():
    parser = argparse.ArgumentParser(description="Import historical (1970-1998) fantasy football data")
    parser.add_argument("--start-year", type=int, default=1970)
    parser.add_argument("--end-year", type=int, default=1998)
    parser.add_argument("--years", nargs="+", type=int, default=None,
                        help="Specific years to import instead of a range")
    args = parser.parse_args()

    years = sorted(args.years) if args.years else list(range(args.start_year, args.end_year + 1))

    log.info("=" * 60)
    log.info("Historical Fantasy Football Importer")
    log.info("Years: %s - %s (%d total)", years[0], years[-1], len(years))
    log.info("=" * 60)

    conn = get_conn()
    start = datetime.now()
    total_rows = 0
    synthetic_cache = {}

    try:
        existing_lookup = get_existing_player_lookup(conn)
        log.info("Loaded %d existing players for name-matching", len(existing_lookup))

        imported_years = []
        for year in years:
            n = import_year(conn, year, existing_lookup, synthetic_cache)
            if n > 0:
                imported_years.append(year)
                total_rows += n

        if imported_years:
            calculate_fantasy_scores_for_years(conn, imported_years)
            try_refresh_materialized_view(conn)
        else:
            log.warning("No years were successfully imported -- skipping score calculation")

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
    log.info("Done in %s -- %d total rows imported across %d years",
              str(elapsed).split(".")[0], total_rows, len(years))
    log.info("Synthetic player IDs created: %d (see 'HISTORICAL' status in players table)",
              len(synthetic_cache))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
