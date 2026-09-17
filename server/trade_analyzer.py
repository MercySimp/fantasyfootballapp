"""Fantasy football trade evaluation and projection loading.

An external CSV can be supplied with TRADE_PROJECTIONS_URL. Expected columns:
player_id, projection_standard, projection_half_ppr, projection_ppr, dynasty_value
and optionally display_name, position. Without it, the analyzer uses a clearly
labelled weighted projection from the three most recent loaded seasons.

Fix notes (2026-09-17 review):
- Positional "replacement level" is now computed from the full available
  projection pool (external feed + recent-performance fallback) instead of
  only the players present in the trade being evaluated. Previously the
  scarcity bonus was an artifact of which players a user happened to include
  in the trade, not a real market baseline.
- Dynasty value now falls back to an age/experience-adjusted estimate when
  the external feed has no dynasty_value populated (which is currently the
  case for every row in data/projections.csv), instead of silently reusing
  the raw season projection with no adjustment at all.
- Every player's value now reports a `value_source` so the API/UI can show
  whether a number came from the external feed, the recent-performance
  fallback, or an estimated dynasty adjustment.
- The accept/decline verdict is now based on the percentage difference
  relative to trade size instead of a flat point threshold, so it scales
  sensibly across scoring formats and league types.
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


def _dynasty_age_adjustment(rookie_year):
    if not rookie_year:
        return 1.0
    try:
        experience = date.today().year - int(rookie_year)
    except (TypeError, ValueError):
        return 1.0
    if experience <= 1:
        return 1.25
    if experience <= 3:
        return 1.1
    if experience <= 6:
        return 1.0
    if experience <= 9:
        return 0.85
    return 0.6


def _load_players(conn, player_ids, scoring_format):
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
    result = {}
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
    external_by_name = {
        _projection_key(value.get("player_name") or value.get("display_name")): value
        for value in external.values()
        if value.get("player_name") or value.get("display_name")
    }
    for player_id in player_ids:
        row = identity.get(player_id)
        if not row:
            continue
        source = external.get(player_id) or external_by_name.get(_projection_key(row["display_name"]))
        base = fallback.get(player_id, {})
        projection_key = f"projection_{scoring_format}"
        projection = None
        dynasty_value = None
        value_source = "recent_performance_fallback"
        if source:
            try:
                projection = float(source.get(projection_key) or "")
                value_source = "external_feed"
            except (TypeError, ValueError):
                projection = None
            try:
                dynasty_value = float(source.get("dynasty_value") or "")
            except (TypeError, ValueError):
                dynasty_value = None
        projection = projection if projection is not None else float(base.get("projection") or 0)
        if value_source == "recent_performance_fallback" and projection == 0:
            value_source = "no_data"

        dynasty_source = "external_feed"
        if dynasty_value is None:
            dynasty_value = round(projection * _dynasty_age_adjustment(row.get("rookie_year")), 1)
            dynasty_source = "estimated_age_adjusted"

        result[player_id] = {
            **row,
            "projection": round(projection, 1),
            "dynasty_value": round(dynasty_value, 1),
            "value_source": value_source,
            "dynasty_value_source": dynasty_source,
        }
    if external:
        overall_source = "Configured projection feed"
    elif external_error:
        overall_source = "Recent-season weighted fallback (configured feed unavailable)"
    else:
        overall_source = "Recent-season weighted fallback"
    return result, overall_source, external


def _position_replacement_pool(conn, external, scoring_format):
    column = {
        "standard": "fantasy_pts_standard",
        "half_ppr": "fantasy_pts_half_ppr",
        "ppr": "fantasy_pts_ppr",
    }[scoring_format]
    pool = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(_fallback_query(column))
        for row in cur.fetchall():
            position = row["position"]
            projection = row["projection"]
            if position and projection is not None:
                pool[position].append(float(projection))

    projection_key = f"projection_{scoring_format}"
    for row in external.values():
        position = str(row.get("position") or "").upper()
        if position not in POSITIONS_WITH_REPLACEMENT:
            continue
        try:
            value = float(row.get(projection_key) or "")
        except (TypeError, ValueError):
            continue
        pool[position].append(value)

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
    players, source, external = _load_players(conn, player_ids, scoring_format)
    missing = sorted(player_ids - set(players))
    if missing:
        raise ValueError(f"Unknown player IDs: {', '.join(missing)}")

    replacement_pool = _position_replacement_pool(conn, external, scoring_format)

    details = []
    for side, ids in (("receiving", received_ids), ("offering", offered_ids)):
        for player_id in ids:
            player = players[player_id]
            position = player["position"]
            replacement = _replacement_points(replacement_pool, position, qb_format)
            scarcity = player["projection"] - replacement
            if position == "QB" and qb_format == "superflex":
                scarcity *= 1.35
            base_value = player["dynasty_value"] if league_type == "dynasty" else player["projection"]
            value = max(0.0, base_value + max(0.0, scarcity) * (0.35 if league_type == "dynasty" else 0.2))
            details.append({
                "side": side,
                "player_id": player_id,
                "display_name": player["display_name"],
                "position": position,
                "projection": player["projection"],
                "positional_value": round(scarcity, 1),
                "trade_value": round(value, 1),
                "value_source": player["value_source"],
                "dynasty_value_source": player["dynasty_value_source"] if league_type == "dynasty" else None,
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
