"""Fantasy football trade evaluation and projection loading.

An external CSV can be supplied with TRADE_PROJECTIONS_URL. Expected columns:
player_id, projection_standard, projection_half_ppr, projection_ppr, dynasty_value
and optionally display_name, position. Without it, the analyzer uses a clearly
labelled weighted projection from the three most recent loaded seasons.

Rework notes (2026-09-17, trade logic overhaul):
- Dynasty and redraft now diverge based on real signals pulled from the
  `players` table (birth_date / rookie_year) and `player_stats_seasonal`
  (games_played history), not just a copy of the season projection:
    * Age: converted to a position-specific aging-curve multiplier. RBs are
      discounted hardest starting in their mid/late 20s (the well-known "RB
      dead zone"), while QBs retain value the longest.
    * Durability: the average games-played rate over each player's most
      recent seasons on record is used as an injury-proneness proxy. There is
      no injury-report table in this schema, so recent per-season
      availability is the closest real signal available. Low durability
      discounts dynasty value more heavily than redraft value, since a
      single missed season matters far more to a multi-year dynasty asset
      than to a one-year redraft asset.
    * Superflex: on top of the existing QB replacement-level widening (2
      starters counted instead of 1), dynasty QB value gets an additional
      premium in superflex leagues to reflect the much longer runway teams
      get from a good starting QB when every roster needs two.
- Positional replacement level ("scarcity") is now computed on the same
  value basis being scored: a dynasty-adjusted pool (age + durability) for
  dynasty leagues, or a raw-projection pool for redraft leagues -- instead of
  mixing a redraft-shaped scarcity number into a dynasty valuation.
- Every player result now reports `age`, `durability_score`, and the
  age/durability multipliers actually applied, so a trade grade is auditable
  instead of a black box.

Validation notes (2026-09-17, mock-trade check #1 -- within-position):
- Sanity-checked against mock trades built from real, well-known players
  within the same position (young durable RB vs. aging workhorse RB; a
  chronically-injured RB vs. an elite RB with one missed season; elite QB vs.
  aging injury-prone QB in 1QB and superflex). Caught a real bug in the
  original RB age curve -- a single flat multiplier for "age 30 and up" let a
  27-year-old replacement-level back outrank a 30-year-old elite back who'd
  only missed one season. Fixed by re-banding the RB curve with 26/28/30/32
  breakpoints.

Validation notes (2026-09-17, mock-trade check #2 -- cross-position):
- Ran mock trades ACROSS positions against a synthetic points distribution
  and caught a formula bug (raw points dominated over value-over-replacement).
  Fixed by making VOR the dominant term: `value = floor_fraction*base + VOR`.

Validation notes (2026-09-17, live-data check #3 -- duplicate pool entries):
- After deploying the fixes above, live trades still showed QBs scarcer than
  RBs/WRs even in plain 1QB leagues (the opposite of reality -- backup QBs
  are far more replaceable than backup RBs/WRs). Root cause: the replacement
  pool merged the external projections feed (PFR-style player_id, e.g.
  "AlleJo01") with a SQL fallback of real recent performance (nflverse
  gsis_id, e.g. "00-0034857") with NO deduplication. Since virtually every
  rosterable player has both a CSV projection and real recent stats, nearly
  every player was counted TWICE under two different ID strings, roughly
  halving the effective depth reached by the replacement-rank index. Because
  QB's talent curve is flat, losing half the depth barely changes QB's
  replacement value; because RB/WR/TE curves are steep in that exact range,
  losing half the depth inflates their replacement value a lot -- which is
  what was manufacturing the illusion that QBs were scarcer. Fixed by
  deduplicating the pool by normalized player NAME (the only reliable link
  across the two incompatible ID namespaces), always preferring the external
  feed's projection over the historical fallback for the same player, and
  only using the fallback to fill in players missing from the external feed
  entirely.
"""

import csv
import io
import os
import re
import urllib.request
from collections import defaultdict
from datetime import date


LEAGUE_TYPES = {"dynasty", "redraft"}
QB_FORMATS = {"one_qb", "superflex"}
SCORING_FORMATS = {"standard", "half_ppr", "ppr"}
PROJECTION_URL = os.getenv("TRADE_PROJECTIONS_URL", "").strip()

POSITIONS_WITH_REPLACEMENT = ("QB", "RB", "WR", "TE")
ROSTER_STARTERS = {"RB": 2, "WR": 2, "TE": 1}

DEFAULT_ROOKIE_AGE = 22  # assumed age at rookie season when birth_date is missing
SUPERFLEX_QB_DYNASTY_PREMIUM = 1.15

FLOOR_FRACTION = {"redraft": 0.15, "dynasty": 0.10}

AGE_CURVES = {
    "QB": [(23, 0.92), (27, 1.05), (32, 1.10), (35, 0.95), (38, 0.75), (None, 0.45)],
    "RB": [(22, 1.05), (24, 1.10), (26, 1.00), (28, 0.75), (30, 0.55), (32, 0.35), (None, 0.20)],
    "WR": [(23, 1.05), (28, 1.10), (31, 0.90), (34, 0.65), (None, 0.40)],
    "TE": [(24, 0.95), (29, 1.10), (32, 0.90), (35, 0.65), (None, 0.40)],
}


def _external_projections():
    source = PROJECTION_URL
    local_path = os.path.join(os.path.dirname(__file__), "data", "projections.csv")
    if not os.path.exists(local_path):
        local_path = os.path.join(os.path.dirname(__file__), "..", "data", "projections.csv")
    if not source and os.path.exists(local_path):
        source = f"file://{os.path.abspath(local_path)}"
    if not source:
        return {}
    if source.startswith("file://"):
        path = source[len("file://"):]
        with open(path, newline='', encoding='utf-8') as fh:
            rows = csv.DictReader(fh)
            result = {}
            for row in rows:
                player_id = (row.get("player_id") or "").strip()
                if not player_id:
                    continue
                result[player_id] = row
            return result
    else:
        with urllib.request.urlopen(source, timeout=8) as response:
            rows = csv.DictReader(io.TextIOWrapper(response, encoding="utf-8"))
            result = {}
            for row in rows:
                player_id = (row.get("player_id") or "").strip()
                if not player_id:
                    continue
                result[player_id] = row
            return result


def _projection_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _fallback_query(scoring_column):
    return f"""
        WITH recent AS (
            SELECT s.player_id, MAX(p.display_name) AS display_name,
                   MAX(p.position) AS position, s.season,
                   s.{scoring_column} AS points,
                   ROW_NUMBER() OVER (PARTITION BY s.player_id ORDER BY s.season DESC) AS recency
            FROM trivia_player_seasons s
            JOIN players p ON p.player_id = s.player_id
            WHERE p.position IN ('QB', 'RB', 'WR', 'TE')
              AND s.season >= 2023
            GROUP BY s.player_id, s.season, s.{scoring_column}
        )
        SELECT player_id, display_name, position,
               SUM(points * CASE recency WHEN 1 THEN 0.5 WHEN 2 THEN 0.3 ELSE 0.2 END) AS projection
        FROM recent
        WHERE recency <= 3
        GROUP BY player_id, display_name, position
    """


def _age_multiplier(position, age):
    curve = AGE_CURVES.get(position)
    if not curve or age is None:
        return 1.0
    for max_age, multiplier in curve:
        if max_age is None or age <= max_age:
            return multiplier
    return curve[-1][1]


def _estimate_age(birth_date, rookie_year):
    today = date.today()
    if birth_date:
        try:
            born = birth_date if hasattr(birth_date, "year") else date.fromisoformat(str(birth_date))
            had_birthday = (today.month, today.day) >= (born.month, born.day)
            return today.year - born.year - (0 if had_birthday else 1)
        except (TypeError, ValueError):
            pass
    if rookie_year:
        try:
            return (today.year - int(rookie_year)) + DEFAULT_ROOKIE_AGE
        except (TypeError, ValueError):
            pass
    return None


def _durability_score(games_history):
    if not games_history:
        return None
    weights = (0.5, 0.3, 0.2)
    weighted_rate, total_weight = 0.0, 0.0
    for (season, games_played), weight in zip(games_history[:3], weights):
        denom = 16 if season and season < 2021 else 17
        rate = max(0.0, min(1.0, (games_played or 0) / denom))
        weighted_rate += rate * weight
        total_weight += weight
    if total_weight == 0:
        return None
    return round(weighted_rate / total_weight, 3)


def _durability_multiplier(durability_score, league_type):
    if durability_score is None:
        return 1.0
    if league_type == "dynasty":
        return round(0.6 + 0.4 * durability_score, 3)
    return round(0.85 + 0.15 * durability_score, 3)


def _fetch_games_history(conn, player_ids):
    if not player_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_id, season, games_played
            FROM player_stats_seasonal
            WHERE player_id = ANY(%s) AND season_type = 'REG'
            ORDER BY player_id, season DESC
            """,
            (list(player_ids),),
        )
        rows = cur.fetchall()
    history = defaultdict(list)
    for row in rows:
        history[row["player_id"]].append((row["season"], row["games_played"]))
    return history


def _load_players(conn, player_ids, scoring_format, league_type, qb_format):
    if not player_ids:
        return {}, "No players supplied", {}
    column = {
        "standard": "fantasy_pts_standard",
        "half_ppr": "fantasy_pts_half_ppr",
        "ppr": "fantasy_pts_ppr",
    }[scoring_format]
    external = {}
    external_error = None
    if PROJECTION_URL or os.path.exists(os.path.join(os.path.dirname(__file__), "data", "projections.csv")):
        try:
            external = _external_projections()
        except (OSError, ValueError) as exc:
            external_error = str(exc)
    with conn.cursor() as cur:
        cur.execute(_fallback_query(column))
        fallback_rows = [dict(row) for row in cur.fetchall()]
        fallback = {row["player_id"]: row for row in fallback_rows}
        cur.execute(
            """SELECT player_id, display_name, position, birth_date, rookie_year
               FROM players WHERE player_id = ANY(%s)""",
            (list(player_ids),),
        )
        identity = {row["player_id"]: dict(row) for row in cur.fetchall()}

    games_history = _fetch_games_history(conn, player_ids)

    external_by_name = {
        _projection_key(value.get("player_name") or value.get("display_name")): value
        for value in external.values()
        if value.get("player_name") or value.get("display_name")
    }
    result = {}
    for player_id in player_ids:
        row = identity.get(player_id)
        if not row:
            continue
        source = external.get(player_id) or external_by_name.get(_projection_key(row["display_name"]))
        base = fallback.get(player_id, {})
        projection_key = f"projection_{scoring_format}"
        projection = None
        value_source = "recent_performance_fallback"
        if source:
            try:
                projection = float(source.get(projection_key) or "")
                value_source = "external_feed"
            except (TypeError, ValueError):
                projection = None
        projection = projection if projection is not None else float(base.get("projection") or 0)
        if value_source == "recent_performance_fallback" and projection == 0:
            value_source = "no_data"

        age = _estimate_age(row.get("birth_date"), row.get("rookie_year"))
        durability_score = _durability_score(games_history.get(player_id, []))

        result[player_id] = {
            **row,
            "projection": round(projection, 1),
            "value_source": value_source,
            "age": age,
            "durability_score": durability_score,
        }
    if external:
        overall_source = "Configured projection feed"
    elif external_error:
        overall_source = "Recent-season weighted fallback (configured feed unavailable)"
    else:
        overall_source = "Recent-season weighted fallback"
    return result, overall_source, external


def _dynasty_value_for(projection, position, age, durability_score, qb_format):
    age_mult = _age_multiplier(position, age)
    durability_mult = _durability_multiplier(durability_score, "dynasty")
    value = projection * age_mult * durability_mult
    if position == "QB" and qb_format == "superflex":
        value *= SUPERFLEX_QB_DYNASTY_PREMIUM
    return value, age_mult, durability_mult


def _collect_external_pool_rows(external, scoring_format):
    """Returns (rows, name_keys_seen) where rows is a list of
    (player_id, position, projection) tuples from the external feed, and
    name_keys_seen is the set of normalized display names already covered --
    used by the caller to avoid double-counting the same real player via the
    SQL fallback's different ID namespace.
    """
    projection_key = f"projection_{scoring_format}"
    rows = []
    name_keys_seen = set()
    for player_id, row in external.items():
        position = str(row.get("position") or "").upper()
        if position not in POSITIONS_WITH_REPLACEMENT:
            continue
        try:
            value = float(row.get(projection_key) or "")
        except (TypeError, ValueError):
            continue
        rows.append((player_id, position, value))
        name_key = _projection_key(row.get("player_name") or row.get("display_name"))
        if name_key:
            name_keys_seen.add(name_key)
    return rows, name_keys_seen


def _position_value_pool(conn, external, scoring_format, league_type, qb_format):
    """League-wide pool used purely for replacement-level scarcity, built on
    the SAME value basis being scored: dynasty-adjusted for dynasty leagues,
    raw projections for redraft leagues.

    IMPORTANT: the external feed and the SQL fallback below use two
    DIFFERENT, INCOMPATIBLE player_id namespaces (PFR-style IDs in the feed
    vs. nflverse gsis_id from the fallback query) for the same real players.
    Naively concatenating both sources double-counts almost every rosterable
    player. We deduplicate by normalized display NAME -- the only reliable
    link between the two ID schemes -- always preferring the external feed's
    value and only pulling from the fallback for players missing from the
    feed entirely.
    """
    column = {
        "standard": "fantasy_pts_standard",
        "half_ppr": "fantasy_pts_half_ppr",
        "ppr": "fantasy_pts_ppr",
    }[scoring_format]

    raw_rows, external_name_keys = _collect_external_pool_rows(external, scoring_format)

    with conn.cursor() as cur:
        cur.execute(_fallback_query(column))
        for row in cur.fetchall():
            if not row["position"] or row["projection"] is None:
                continue
            name_key = _projection_key(row["display_name"])
            if name_key and name_key in external_name_keys:
                # Same real player already represented via the external feed
                # under a different ID scheme -- skip to avoid double-counting.
                continue
            raw_rows.append((row["player_id"], row["position"], float(row["projection"])))

    pool = defaultdict(list)
    if league_type != "dynasty":
        for _player_id, position, projection in raw_rows:
            pool[position].append(projection)
        for position in pool:
            pool[position].sort(reverse=True)
        return pool

    # Dynasty: batch-fetch age/durability inputs for every pool member so we
    # don't run a query per player. Since these player_ids come from two
    # different ID namespaces, this lookup will only match players sourced
    # from the SQL fallback (real gsis_id) -- external-feed-only players will
    # get a neutral (1.0) age/durability multiplier here, same as elsewhere
    # in this module when identity data can't be resolved for a given ID.
    pool_player_ids = list({player_id for player_id, _pos, _val in raw_rows})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT player_id, birth_date, rookie_year FROM players WHERE player_id = ANY(%s)",
            (pool_player_ids,),
        )
        identity = {row["player_id"]: dict(row) for row in cur.fetchall()}
    games_history = _fetch_games_history(conn, pool_player_ids)

    for player_id, position, projection in raw_rows:
        identity_row = identity.get(player_id, {})
        age = _estimate_age(identity_row.get("birth_date"), identity_row.get("rookie_year"))
        durability_score = _durability_score(games_history.get(player_id, []))
        dynasty_value, _age_mult, _dur_mult = _dynasty_value_for(
            projection, position, age, durability_score, qb_format
        )
        pool[position].append(dynasty_value)

    for position in pool:
        pool[position].sort(reverse=True)
    return pool


def _replacement_points(pool, position, qb_format):
    values = pool.get(position, [])
    if not values:
        return 0.0
    if position == "QB":
        roster_count = 1 if qb_format == "one_qb" else 2
    else:
        roster_count = ROSTER_STARTERS.get(position, 1)
    index = min(len(values) - 1, roster_count * 12)
    return values[index]


def analyze(conn, received_ids, offered_ids, league_type, qb_format, scoring_format):
    league_type = league_type if league_type in LEAGUE_TYPES else "redraft"
    qb_format = qb_format if qb_format in QB_FORMATS else "one_qb"
    scoring_format = scoring_format if scoring_format in SCORING_FORMATS else "ppr"
    player_ids = set(received_ids) | set(offered_ids)
    players, source, external = _load_players(conn, player_ids, scoring_format, league_type, qb_format)
    missing = sorted(player_ids - set(players))
    if missing:
        raise ValueError(f"Unknown player IDs: {', '.join(missing)}")

    replacement_pool = _position_value_pool(conn, external, scoring_format, league_type, qb_format)
    floor_fraction = FLOOR_FRACTION[league_type]

    details = []
    for side, ids in (("receiving", received_ids), ("offering", offered_ids)):
        for player_id in ids:
            player = players[player_id]
            position = player["position"]
            age = player["age"]
            durability_score = player["durability_score"]

            if league_type == "dynasty":
                base_value, age_mult, durability_mult = _dynasty_value_for(
                    player["projection"], position, age, durability_score, qb_format
                )
                value_basis = "dynasty_age_durability_model"
            else:
                age_mult = 1.0
                durability_mult = _durability_multiplier(durability_score, "redraft")
                base_value = player["projection"] * durability_mult
                value_basis = "redraft_projection_with_durability_haircut"

            replacement = _replacement_points(replacement_pool, position, qb_format)
            value_over_replacement = base_value - replacement
            if position == "QB" and qb_format == "superflex" and league_type != "dynasty":
                value_over_replacement *= 1.35

            value = floor_fraction * base_value + max(0.0, value_over_replacement)

            details.append({
                "side": side,
                "player_id": player_id,
                "display_name": player["display_name"],
                "position": position,
                "projection": player["projection"],
                "age": age,
                "durability_score": durability_score,
                "age_multiplier": round(age_mult, 3) if league_type == "dynasty" else None,
                "durability_multiplier": round(durability_mult, 3),
                "replacement_level": round(replacement, 1),
                "value_over_replacement": round(value_over_replacement, 1),
                "trade_value": round(value, 1),
                "value_source": player["value_source"],
                "value_basis": value_basis,
            })
    received = sum(p["trade_value"] for p in details if p["side"] == "receiving")
    offered = sum(p["trade_value"] for p in details if p["side"] == "offering")
    difference = round(received - offered, 1)
    denominator = max(received, offered, 1.0)
    percent_difference = round((difference / denominator) * 100, 1)
    if percent_difference >= 15:
        verdict = "Strong accept"
    elif percent_difference >= 5:
        verdict = "Accept"
    elif percent_difference > -5:
        verdict = "Close"
    else:
        verdict = "Decline"
    return {
        "league": {
            "type": league_type,
            "qb_format": qb_format,
            "scoring_format": scoring_format,
        },
        "projection_source": source,
        "received_value": round(received, 1),
        "offered_value": round(offered, 1),
        "difference": difference,
        "percent_difference": percent_difference,
        "verdict": verdict,
        "players": details,
    }
