#!/usr/bin/env python3
"""
Pro-Football-Reference Scraper (any year, e.g. 1932-1969 pre-merger seasons)

Scrapes the three core season stat pages per year --
/years/{year}/passing.htm, /rushing.htm, /receiving.htm -- merges them into
one per-player-season record, and loads them into the same PostgreSQL schema
used by import_data.py and import_historical_1970_1998.py.

RATE LIMITING -- READ THIS FIRST:
Sports-Reference sites (including PFR) actively rate-limit and temporarily
block IPs that scrape too aggressively. This script makes exactly 3 requests
per year (one per stat table) with a conservative randomized delay between
every single request, and retries with exponential backoff on HTTP 429. Do
not lower REQUEST_DELAY_RANGE below what's set here, and avoid running this
against a large year range in a single sitting without breaks. If you get
repeatedly blocked, stop and wait a few hours before retrying.

Known limitations vs. modern nflreadpy data:
  - No targets before ~1992 (PFR didn't track them yet) -- inserted as 0.
  - No 2-point conversions, sacks, or special-teams TDs in these tables --
    inserted as 0.
  - Position labels vary by era (E, FL, SE, HB, TB, WB, etc.) -- mapped to
    QB/RB/WR/TE via POSITION_ALIASES; anything unmappable is skipped and
    logged so you can extend the alias table if needed.
  - Team codes are PFR's historical abbreviations, mapped to modern nflverse
    codes via PFR_TEAM_MAP. A few codes are genuinely ambiguous across eras
    (e.g. "STL" meant the Cardinals before 1988-ish and the Rams from 1995-
    2015) -- resolved with year-aware logic in normalize_team().

Usage:
    python3 import_pfr_scraper.py --start-year 1960 --end-year 1969
    python3 import_pfr_scraper.py --years 1965 1966
"""

import argparse
import logging
import random
import re
import sys
import time
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

BASE_URL = "https://www.pro-football-reference.com/years/{year}/{table}.htm"
TABLES = ["passing", "rushing", "receiving"]

# Be conservative. PFR/Sports-Reference actively rate-limits scrapers.
REQUEST_DELAY_RANGE = (4.0, 7.0)   # seconds, randomized between every request
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 15  # seconds, doubles each retry

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
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

MODERN_TEAM_ABBR_MAP = {
    "CLV": "CLE", "BLT": "BAL", "HST": "HOU", "OAK": "LV", "LVR": "LV",
    "SD": "LAC", "JAC": "JAX", "ARZ": "ARI",
}

PFR_TEAM_MAP = {
    "RAI": "LV", "OAK": "LV",
    "SFO": "SF",
    "GNB": "GB",
    "KAN": "KC",
    "NOR": "NO",
    "NWE": "NE", "BOS": "NE",           # Patriots were "Boston Patriots" pre-1971
    "SDG": "LAC", "SDC": "LAC",
    "TAM": "TB",
    "PHO": "ARI", "CRD": "ARI",         # Cardinals: Chicago -> St.Louis -> Phoenix -> Arizona
    "RAM": "LA",
    "CLT": "IND", "BLC": "IND",         # Colts: Baltimore -> Indianapolis
    "HOI": "TEN", "OTI": "TEN",         # Oilers -> Titans
    "WSH": "WAS",
    "DTX": "TEN",                        # Dallas Texans (AFL) -> KC Chiefs lineage; treat separately if needed
}


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


def clean_player_name(raw: str) -> str:
    """Strips Pro Bowl (*) / All-Pro (+) marker characters PFR appends to names."""
    return re.sub(r"[\*\+]+$", "", str(raw)).strip()


def normalize_team(raw_team, season: int) -> str:
    raw_team = str(raw_team or "").strip().upper()
    if raw_team in ("2TM", "3TM", "4TM", "5TM"):
        return raw_team  # handled specially in aggregation, never stored directly

    # STL is genuinely ambiguous across eras: Cardinals through 1987,
    # Rams from 1995-2015. Years 1988-1994 had no STL NFL team at all.
    if raw_team == "STL":
        return "ARI" if season <= 1987 else "LA"

    mapped = PFR_TEAM_MAP.get(raw_team, raw_team)
    mapped = MODERN_TEAM_ABBR_MAP.get(mapped, mapped)
    return mapped


POSITION_ALIASES = {
    "HB": "RB", "FB": "RB", "TB": "RB", "WB": "RB", "RB": "RB",
    "WR": "WR", "FL": "WR", "SE": "WR", "E": "WR",
    "TE": "TE",
    "QB": "QB",
}


def normalize_position(raw_pos) -> str:
    raw_pos = str(raw_pos or "").strip().upper()
    return POSITION_ALIASES.get(raw_pos, "")


def fetch_html_with_retry(url: str) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 429:
            wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            log.warning("  Rate limited (429) on %s -- waiting %ds before retry %d/%d",
                        url, wait, attempt, MAX_RETRIES)
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} retries")


def polite_delay():
    time.sleep(random.uniform(*REQUEST_DELAY_RANGE))


def fetch_table(year: int, table_name: str) -> pd.DataFrame:
    url = BASE_URL.format(year=year, table=table_name)
    log.info("  Fetching %s ...", url)
    html = fetch_html_with_retry(url)

    try:
        tables = pd.read_html(StringIO(html), attrs={"id": table_name})
    except ValueError:
        tables = pd.read_html(StringIO(html))

    if not tables:
        raise RuntimeError(f"No table found for {table_name} {year}")

    df = tables[0]

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]

    if "Rk" in df.columns:
        df = df[df["Rk"] != "Rk"]
    if "Player" in df.columns:
        df = df[df["Player"].notna()]
        df = df[df["Player"] != "Player"]
        df["Player"] = df["Player"].apply(clean_player_name)

    return df.reset_index(drop=True)


def dedupe_traded_players(df: pd.DataFrame) -> pd.DataFrame:
    """
    PFR already provides a combined "2TM"/"3TM" row for players traded
    mid-season, PLUS separate rows for each individual team stint. Keep only
    the combined row when present to avoid double-counting; otherwise keep
    the single-team row as-is.
    """
    if "Player" not in df.columns or "Tm" not in df.columns:
        return df

    keep_rows = []
    for name, group in df.groupby("Player"):
        multi_team_rows = group[group["Tm"].astype(str).str.contains("TM", na=False)]
        if len(multi_team_rows) > 0:
            keep_rows.append(multi_team_rows.iloc[[0]])
        else:
            keep_rows.append(group.iloc[[0]])
    return pd.concat(keep_rows, ignore_index=True) if keep_rows else df


def col(row, name, default=0):
    """Safe column access -- returns default if the column doesn't exist in this era's table."""
    if name in row.index and pd.notna(row[name]):
        return row[name]
    return default


def scrape_year(year: int) -> dict:
    """Returns dict: player_name -> merged stat dict for this season."""
    merged = {}

    passing_df = fetch_table(year, "passing")
    passing_df = dedupe_traded_players(passing_df)
    polite_delay()

    rushing_df = fetch_table(year, "rushing")
    rushing_df = dedupe_traded_players(rushing_df)
    polite_delay()

    receiving_df = fetch_table(year, "receiving")
    receiving_df = dedupe_traded_players(receiving_df)
    polite_delay()

    def get_or_create(name):
        if name not in merged:
            merged[name] = {
                "name": name, "position": "", "team": "", "games_played": 0,
                "completions": 0, "passing_attempts": 0, "passing_yards": 0.0,
                "passing_tds": 0, "interceptions": 0,
                "carries": 0, "rushing_yards": 0.0, "rushing_tds": 0,
                "receptions": 0, "targets": 0, "receiving_yards": 0.0, "receiving_tds": 0,
                "fumbles_lost": 0,
            }
        return merged[name]

    for _, r in passing_df.iterrows():
        name = r["Player"]
        entry = get_or_create(name)
        pos = normalize_position(col(r, "Pos", ""))
        if pos and not entry["position"]:
            entry["position"] = pos
        elif not entry["position"]:
            entry["position"] = "QB"  # default: appearing in the passing table strongly implies QB
        entry["team"] = normalize_team(col(r, "Tm", entry["team"]), year) or entry["team"]
        entry["games_played"] = max(entry["games_played"], safe_int(col(r, "G")))
        entry["completions"] += safe_int(col(r, "Cmp"))
        entry["passing_attempts"] += safe_int(col(r, "Att"))
        entry["passing_yards"] += safe_float(col(r, "Yds"))
        entry["passing_tds"] += safe_int(col(r, "TD"))
        entry["interceptions"] += safe_int(col(r, "Int"))

    for _, r in rushing_df.iterrows():
        name = r["Player"]
        entry = get_or_create(name)
        pos = normalize_position(col(r, "Pos", ""))
        if pos and (not entry["position"] or entry["position"] == "QB"):
            # Prefer a clearly-labeled skill position over our QB default guess
            if pos != "QB" or not entry["position"]:
                entry["position"] = pos
        entry["team"] = normalize_team(col(r, "Tm", entry["team"]), year) or entry["team"]
        entry["games_played"] = max(entry["games_played"], safe_int(col(r, "G")))
        entry["carries"] += safe_int(col(r, "Att"))
        entry["rushing_yards"] += safe_float(col(r, "Yds"))
        entry["rushing_tds"] += safe_int(col(r, "TD"))
        entry["fumbles_lost"] += safe_int(col(r, "Fmb"))

    for _, r in receiving_df.iterrows():
        name = r["Player"]
        entry = get_or_create(name)
        pos = normalize_position(col(r, "Pos", ""))
        if pos and (not entry["position"] or entry["position"] == "QB"):
            if pos != "QB" or not entry["position"]:
                entry["position"] = pos
        entry["team"] = normalize_team(col(r, "Tm", entry["team"]), year) or entry["team"]
        entry["games_played"] = max(entry["games_played"], safe_int(col(r, "G")))
        entry["receptions"] += safe_int(col(r, "Rec"))
        entry["targets"] += safe_int(col(r, "Tgt"))
        entry["receiving_yards"] += safe_float(col(r, "Yds"))
        entry["receiving_tds"] += safe_int(col(r, "TD"))
        entry["fumbles_lost"] += safe_int(col(r, "Fmb"))

    # Default any player who still has no resolvable position to their
    # dominant statistical category, and drop anyone still unresolved.
    final = {}
    unresolved = 0
    for name, entry in merged.items():
        if entry["position"] not in ("QB", "RB", "WR", "TE"):
            rush = entry["rushing_yards"]
            rec = entry["receiving_yards"]
            passv = entry["passing_yards"]
            if passv >= rush and passv >= rec and passv > 0:
                entry["position"] = "QB"
            elif rush >= rec:
                entry["position"] = "RB"
            else:
                entry["position"] = "WR"
            unresolved += 1
        final[name] = entry

    if unresolved:
        log.info("  %d players had no clean position label -- inferred from dominant stat category", unresolved)

    return final


def get_existing_player_lookup(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT player_id, display_name, position FROM players")
        rows = cur.fetchall()
    lookup = {}
    for player_id, display_name, position in rows:
        key = (display_name.strip().upper(), (position or "").strip().upper())
        lookup[key] = player_id
    return lookup


def ensure_synthetic_player(conn, cur, name: str, position: str, synthetic_cache: dict) -> str:
    slug_key = f"pfr_{slugify(name)}_{position.lower()}"
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


def resolve_player_id(conn, cur, existing_lookup, synthetic_cache, name, position) -> str:
    key = (name.strip().upper(), position.strip().upper())
    if key in existing_lookup:
        return existing_lookup[key]
    return ensure_synthetic_player(conn, cur, name, position, synthetic_cache)


def import_year(conn, year: int, existing_lookup: dict, synthetic_cache: dict) -> int:
    log.info("Scraping %d ...", year)
    try:
        merged = scrape_year(year)
    except Exception as exc:
        log.error("  Failed to scrape %d: %s", year, exc)
        return 0

    rows = []
    with conn.cursor() as cur:
        for name, s in merged.items():
            if s["position"] not in ("QB", "RB", "WR", "TE"):
                continue
            player_id = resolve_player_id(conn, cur, existing_lookup, synthetic_cache, name, s["position"])
            rows.append((
                player_id, name, name, s["position"], s["position"], year, "REG", s["team"],
                s["games_played"],
                s["completions"], s["passing_attempts"], s["passing_yards"], s["passing_tds"], s["interceptions"],
                0, 0,
                s["carries"], s["rushing_yards"], s["rushing_tds"], s["fumbles_lost"], 0,
                s["receptions"], s["targets"], s["receiving_yards"], s["receiving_tds"], 0, 0, 0,
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
                targets = EXCLUDED.targets,
                receiving_yards = EXCLUDED.receiving_yards,
                receiving_tds = EXCLUDED.receiving_tds
        """, rows, page_size=500)
    conn.commit()
    log.info("  -> %d rows upserted for %d", len(rows), year)
    return len(rows)


def calculate_fantasy_scores_for_years(conn, years: list):
    log.info("Calculating fantasy scores for %s ...", years)
    season_filter = ",".join(str(y) for y in years)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT player_id, season, season_type,
                   passing_yards, passing_tds, interceptions, passing_2pt_conversions,
                   rushing_yards, rushing_tds, rushing_fumbles_lost, rushing_2pt_conversions,
                   receptions, receiving_yards, receiving_tds,
                   receiving_fumbles_lost, receiving_2pt_conversions,
                   special_teams_tds, games_played
            FROM player_stats_seasonal
            WHERE season IN ({season_filter})
        """)
        rows = cur.fetchall()

    score_rows = []
    for (pid, season, stype, pass_yd, pass_td, ints, pass_2pt,
         rush_yd, rush_td, rush_fum, rush_2pt,
         rec, rec_yd, rec_td, rec_fum, rec_2pt, st_td, gp) in rows:
        for fmt_id, fmt in SCORING_FORMATS.items():
            pts = (
                (pass_yd or 0) * fmt["pass_yd"] + (pass_td or 0) * fmt["pass_td"] +
                (ints or 0) * fmt["pass_int"] + (pass_2pt or 0) * fmt["pass_2pt"] +
                (rush_yd or 0) * fmt["rush_yd"] + (rush_td or 0) * fmt["rush_td"] +
                (rush_fum or 0) * fmt["fumble"] + (rush_2pt or 0) * fmt["rush_2pt"] +
                (rec or 0) * fmt["rec_bonus"] + (rec_yd or 0) * fmt["rec_yd"] +
                (rec_td or 0) * fmt["rec_td"] + (rec_fum or 0) * fmt["fumble"] +
                (rec_2pt or 0) * fmt["rec_2pt"] + (st_td or 0) * fmt["st_td"]
            )
            ppg = round(pts / gp, 2) if gp and gp > 0 else None
            score_rows.append((pid, season, stype, fmt_id, round(pts, 2), ppg, gp))

    if not score_rows:
        log.warning("  No fantasy scores to calculate.")
        return

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO fantasy_scores_seasonal
                (player_id, season, season_type, format_id, fantasy_points, fantasy_ppg, games_played)
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
                SELECT fs2.player_id, fs2.season, fs2.format_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY p.position, fs2.season, fs2.format_id
                           ORDER BY fs2.fantasy_points DESC
                       ) AS rn
                FROM fantasy_scores_seasonal fs2
                JOIN players p ON fs2.player_id = p.player_id
                WHERE fs2.season IN ({season_filter})
            ) ranked
            WHERE fs.player_id = ranked.player_id AND fs.season = ranked.season AND fs.format_id = ranked.format_id
        """)
    conn.commit()
    log.info("  -> position ranks updated")


def try_refresh_materialized_view(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW trivia_player_seasons")
        conn.commit()
        log.info("Refreshed materialized view trivia_player_seasons")
    except Exception:
        conn.rollback()
        log.info("trivia_player_seasons is not a materialized view -- skipping refresh")


def main():
    parser = argparse.ArgumentParser(description="Scrape Pro-Football-Reference season stats")
    parser.add_argument("--start-year", type=int, default=1960)
    parser.add_argument("--end-year", type=int, default=1969)
    parser.add_argument("--years", nargs="+", type=int, default=None)
    args = parser.parse_args()

    years = sorted(args.years) if args.years else list(range(args.start_year, args.end_year + 1))

    log.info("=" * 60)
    log.info("Pro-Football-Reference Scraper")
    log.info("Years: %s - %s (%d total, %d requests total, ~%.0f min estimated)",
              years[0], years[-1], len(years), len(years) * 3,
              len(years) * 3 * sum(REQUEST_DELAY_RANGE) / 2 / 60)
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
    log.info("Synthetic player IDs created: %d", len(synthetic_cache))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
