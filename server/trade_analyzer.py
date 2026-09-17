"""Fantasy football trade evaluation and projection loading.

An external CSV can be supplied with TRADE_PROJECTIONS_URL. Expected columns:
player_id, projection_standard, projection_half_ppr, projection_ppr, dynasty_value
and optionally display_name, position. Without it, the analyzer uses a clearly
labelled weighted projection from the three most recent loaded seasons.

Rework notes (2026-09-17, trade logic overhaul):
- Dynasty and redraft now diverge based on real signals pulled from the
  `players` table (birth_date / rookie_year) and `player_stats_seasonal`
  (games_played history): position-specific age curves and a durability
  (injury-proneness) proxy from recent games-played rate.
- Positional replacement level ("scarcity") is computed on the same value
  basis being scored (dynasty-adjusted pool for dynasty, raw projections for
  redraft), instead of mixing a redraft-shaped scarcity number into a
  dynasty valuation.

Validation notes (2026-09-17, mock-trade + live-data debugging, chronological):
1. Within-position mock trades caught a flat RB age-curve bug (fixed with
   finer age breakpoints).
2. Cross-position mock trades caught a formula bug where raw points
   dominated over value-over-replacement (VOR); fixed by making VOR the
   dominant term.
3. Live server output caught duplicate-counting in the replacement pool: the
   external feed (PFR-style IDs) and the SQL historical fallback (nflverse
   gsis_id) were both counted for the same players under different ID
   strings, artificially inflating RB/WR/TE replacement level far more than
   QB's (since QB's curve is flat, losing half the pool depth barely moved
   its replacement value; RB/WR/TE's steep curves moved a lot). Fixed by
   deduplicating the pool by normalized player name.
4. Live output still showed a real, elite-tier WR trailing an elite QB in
   1QB redraft by ~22%. Root cause: ROSTER_STARTERS never accounted for the
   FLEX roster slot (RB/WR-eligible in virtually every real league; QB never
   is), making the WR/RB replacement threshold shallower than real roster
   construction implies. Fixed by treating RB/WR as 2.5 effective starters
   (one shared FLEX spot) instead of 2.0.
5. Even after that fix, a real coefficient-of-variation analysis of the
   startable tier at each position (QB CV=0.06 vs RB=0.19, WR=0.13, TE=0.12
   in this league's real projections) confirmed the user's structural
   critique: a single replacement-rank point doesn't capture how much
   DEPTH/CLUSTERING exists within a position's startable range. QB's
   startable tier is 3-10x more tightly clustered than the other positions,
   meaning the practical difference between "the best QB" and "a good
   enough QB" is much smaller than the raw points-above-replacement number
   implies -- exactly the logic behind the standard "don't reach for QB"
   redraft doctrine. Added a variance/depth multiplier (see
   _position_depth_multipliers) that shrinks VOR for low-CV (deep) positions
   and amplifies it for high-CV (scarce) positions, dampened by sqrt and
   capped to [0.7, 1.3] so one season's noisy CV estimate can't swing values
   wildly.
6. Added a season-to-date performance-vs-expectation signal: if a player has
   played enough games this season to be meaningful, their actual scoring
   pace is blended into their value, so a player drastically over- or
   under-performing their preseason projection gets nudged accordingly
   instead of the model pretending the preseason number is still gospel.
   NOTE: this requires `fantasy_scores_seasonal` to actually contain
   up-to-date rows for the CURRENT season -- if the import job that
   populates that table from nflverse hasn't been run recently, this signal
   will simply be unavailable (returns None) rather than silently wrong.
"""

import csv
import io
import math
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
# Effective starters per 12-team roster, INCLUDING a share of the standard
# single FLEX spot (RB/WR-eligible in the vast majority of real leagues; TE
# and QB are left at their base starter counts).
ROSTER_STARTERS = {"RB": 2.5, "WR": 2.5, "TE": 1}

DEFAULT_ROOKIE_AGE = 22  # assumed age at rookie season when birth_date is missing
SUPERFLEX_QB_DYNASTY_PREMIUM = 1.15

FLOOR_FRACTION = {"redraft": 0.15, "dynasty": 0.10}

# Depth-multiplier dampening/clamping (see _position_depth_multipliers).
DEPTH_MULT_MIN = 0.7
DEPTH_MULT_MAX = 1.3

# Season-to-date performance-vs-expectation blending.
CURRENT_SEASON = date.today().year
SEASON_WEEKS = 17
MIN_GAMES_FOR_PERFORMANCE_SIGNAL = 3
PERFORMANCE_BLEND_WEIGHT = 0.25  # how much actual current-season pace nudges projected value
PERFORMANCE_RATIO_MIN = 0.6
PERFORMANCE_RATIO_MAX = 1.6

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


def _fetch_current_season_performance(conn, player_ids, scoring_format):
    """Returns {player_id: (fantasy_points_so_far, games_played_so_far)} for
    the current calendar-year NFL season, sourced from fantasy_scores_seasonal
    (which is only as fresh as the last time the import job was run against
    nflverse). Callers must treat a missing player_id as "no signal
    available", not "player has 0 points".
    """
    if not player_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_id, fantasy_points, games_played
            FROM fantasy_scores_seasonal
            WHERE player_id = ANY(%s) AND season = %s AND season_type = 'REG' AND format_id = %s
            """,
            (list(player_ids), CURRENT_SEASON, scoring_format),
        )
        return {row["player_id"]: (row["fantasy_points"], row["games_played"]) for row in cur.fetchall()}


def _performance_ratio(actual_points, games_played, season_projection):
    """Compares actual scoring pace this season to the preseason full-season
    projection. Returns None (no adjustment) when there isn't enough signal
    yet -- specifically fewer than MIN_GAMES_FOR_PERFORMANCE_SIGNAL games
    played, or no usable projection to compare against. Clipped to avoid a
    tiny early-season sample (e.g. one huge or one disastrous game) swinging
    a player's trade value wildly.
    """
    if games_played is None or games_played < MIN_GAMES_FOR_PERFORMANCE_SIGNAL:
        return None
    if not season_projection or season_projection <= 0:
        return None
    pace = (actual_points or 0) / games_played * SEASON_WEEKS
    ratio = pace / season_projection
    return round(max(PERFORMANCE_RATIO_MIN, min(PERFORMANCE_RATIO_MAX, ratio)), 3)


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
    current_performance = _fetch_current_season_performance(conn, player_ids, scoring_format)

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

        actual_points, games_played_this_season = current_performance.get(player_id, (None, None))
        performance_ratio = _performance_ratio(actual_points, games_played_this_season, projection)
        if performance_ratio is not None:
            projection_adjusted = projection * (1 - PERFORMANCE_BLEND_WEIGHT + PERFORMANCE_BLEND_WEIGHT * performance_ratio)
        else:
            projection_adjusted = projection

        result[player_id] = {
            **row,
            "projection": round(projection, 1),
            "projection_adjusted": round(projection_adjusted, 1),
            "performance_ratio": performance_ratio,
            "games_played_this_season": games_played_this_season,
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

    Deduplicates by normalized display NAME across the external feed
    (PFR-style IDs) and the SQL fallback (nflverse gsis_id) -- see module
    docstring item 3.
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
                continue
            raw_rows.append((row["player_id"], row["position"], float(row["projection"])))

    pool = defaultdict(list)
    if league_type != "dynasty":
        for _player_id, position, projection in raw_rows:
            pool[position].append(projection)
        for position in pool:
            pool[position].sort(reverse=True)
        return pool

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


def _roster_count_for(position, qb_format):
    if position == "QB":
        return 1 if qb_format == "one_qb" else 2
    return ROSTER_STARTERS.get(position, 1)


def _replacement_points(pool, position, qb_format):
    values = pool.get(position, [])
    if not values:
        return 0.0
    roster_count = _roster_count_for(position, qb_format)
    # roster_count may be fractional (e.g. 2.5 to represent a shared FLEX
    # spot) -- round to the nearest whole roster slot before indexing.
    index = min(len(values) - 1, int(round(roster_count * 12)))
    return values[index]


def _position_depth_multipliers(pool, qb_format):
    """Computes a variance/depth-based multiplier per position, reflecting
    how tightly clustered (deep/replaceable) vs. spread out (scarce) each
    position's real STARTABLE tier is -- not just the single point at the
    replacement rank.

    Positions with a low coefficient of variation (stdev/mean) across their
    startable tier are "deep": the difference between the best option and a
    solidly-good option is small in practice, which is exactly the logic
    behind the standard "don't reach for a QB" redraft doctrine, since a
    12-team league's startable QB tier is historically far more tightly
    clustered than RB/WR. High-CV positions are "scarce": the gap between
    great and merely-good options is large and real.

    The raw CV ratio is dampened with sqrt() and capped to
    [DEPTH_MULT_MIN, DEPTH_MULT_MAX] so that a single season's noisy
    estimate (especially early in a season, or for thin position pools)
    can't swing trade values by an extreme amount.
    """
    coefficients = {}
    for position in POSITIONS_WITH_REPLACEMENT:
        values = pool.get(position, [])
        if len(values) < 2:
            continue
        roster_count = _roster_count_for(position, qb_format)
        tier_size = min(len(values), int(round(roster_count * 12)) + 1)
        tier = values[:tier_size]
        if len(tier) < 2:
            continue
        mean = sum(tier) / len(tier)
        if mean <= 0:
            continue
        variance = sum((v - mean) ** 2 for v in tier) / len(tier)
        coefficients[position] = (variance ** 0.5) / mean

    if not coefficients:
        return {position: 1.0 for position in POSITIONS_WITH_REPLACEMENT}

    avg_cv = sum(coefficients.values()) / len(coefficients)
    multipliers = {}
    for position in POSITIONS_WITH_REPLACEMENT:
        cv = coefficients.get(position)
        if not cv or avg_cv <= 0:
            multipliers[position] = 1.0
            continue
        raw_multiplier = math.sqrt(cv / avg_cv)
        multipliers[position] = round(max(DEPTH_MULT_MIN, min(DEPTH_MULT_MAX, raw_multiplier)), 3)
    return multipliers


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
    depth_multipliers = _position_depth_multipliers(replacement_pool, qb_format)
    floor_fraction = FLOOR_FRACTION[league_type]

    details = []
    for side, ids in (("receiving", received_ids), ("offering", offered_ids)):
        for player_id in ids:
            player = players[player_id]
            position = player["position"]
            age = player["age"]
            durability_score = player["durability_score"]
            projection = player["projection_adjusted"]

            if league_type == "dynasty":
                base_value, age_mult, durability_mult = _dynasty_value_for(
                    projection, position, age, durability_score, qb_format
                )
                value_basis = "dynasty_age_durability_model"
            else:
                age_mult = 1.0
                durability_mult = _durability_multiplier(durability_score, "redraft")
                base_value = projection * durability_mult
                value_basis = "redraft_projection_with_durability_haircut"

            replacement = _replacement_points(replacement_pool, position, qb_format)
            value_over_replacement = base_value - replacement
            if position == "QB" and qb_format == "superflex" and league_type != "dynasty":
                value_over_replacement *= 1.35
            depth_multiplier = depth_multipliers.get(position, 1.0)
            value_over_replacement *= depth_multiplier

            value = floor_fraction * base_value + max(0.0, value_over_replacement)

            details.append({
                "side": side,
                "player_id": player_id,
                "display_name": player["display_name"],
                "position": position,
                "projection": player["projection"],
                "performance_ratio": player["performance_ratio"],
                "games_played_this_season": player["games_played_this_season"],
                "age": age,
                "durability_score": durability_score,
                "age_multiplier": round(age_mult, 3) if league_type == "dynasty" else None,
                "durability_multiplier": round(durability_mult, 3),
                "depth_multiplier": depth_multiplier,
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
