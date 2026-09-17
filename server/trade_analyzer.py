"""Fantasy football trade evaluation and projection loading.

An external CSV can be supplied with TRADE_PROJECTIONS_URL. Expected columns:
player_id, projection_standard, projection_half_ppr, projection_ppr, dynasty_value
and optionally display_name, position. Without it, the analyzer uses a clearly
labelled weighted projection from the three most recent loaded seasons.
"""

import csv
import io
import os
import re
import urllib.request
from datetime import date


LEAGUE_TYPES = {"dynasty", "redraft"}
QB_FORMATS = {"one_qb", "superflex"}
SCORING_FORMATS = {"standard", "half_ppr", "ppr"}
PROJECTION_URL = os.getenv("TRADE_PROJECTIONS_URL", "").strip()


def _external_projections():
    # Prefer an explicitly configured URL (http(s) or file://). If not set,
    # allow a local uploaded CSV at data/projections.csv for convenience.
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


def _load_players(conn, player_ids, scoring_format):
    if not player_ids:
        return {}, "No players supplied"
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
        fallback = {row["player_id"]: dict(row) for row in cur.fetchall()}
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
        source_name = "Recent-season weighted fallback"
        if source:
            try:
                projection = float(source.get(projection_key) or "")
                dynasty_value = float(source.get("dynasty_value") or "")
                source_name = "Configured projection feed"
            except (TypeError, ValueError):
                projection = None
        projection = projection if projection is not None else float(base.get("projection") or 0)
        result[player_id] = {
            **row,
            "projection": round(projection, 1),
            "dynasty_value": round(dynasty_value if dynasty_value is not None else projection, 1),
            "source": source_name,
        }
    if external:
        return result, "Configured projection feed"
    if external_error:
        return result, "Recent-season weighted fallback (configured feed unavailable)"
    return result, "Recent-season weighted fallback"


def _replacement_points(players, position, qb_format):
    pool = sorted(
        (p["projection"] for p in players.values() if p["position"] == position),
        reverse=True,
    )
    roster_count = {"QB": 1 if qb_format == "one_qb" else 2, "RB": 2, "WR": 2, "TE": 1}[position]
    index = min(len(pool) - 1, roster_count * 12) if pool else 0
    return pool[index] if pool else 0


def analyze(conn, received_ids, offered_ids, league_type, qb_format, scoring_format):
    league_type = league_type if league_type in LEAGUE_TYPES else "redraft"
    qb_format = qb_format if qb_format in QB_FORMATS else "one_qb"
    scoring_format = scoring_format if scoring_format in SCORING_FORMATS else "ppr"
    player_ids = set(received_ids) | set(offered_ids)
    players, source = _load_players(conn, player_ids, scoring_format)
    missing = sorted(player_ids - set(players))
    if missing:
        raise ValueError(f"Unknown player IDs: {', '.join(missing)}")

    all_for_baseline = players
    details = []
    for side, ids in (("receiving", received_ids), ("offering", offered_ids)):
        for player_id in ids:
            player = players[player_id]
            scarcity = player["projection"] - _replacement_points(all_for_baseline, player["position"], qb_format)
            if player["position"] == "QB" and qb_format == "superflex":
                scarcity *= 1.35
            value = player["dynasty_value"] if league_type == "dynasty" else player["projection"]
            value = max(0, value + max(0, scarcity) * (0.35 if league_type == "dynasty" else 0.2))
            details.append({
                "side": side,
                "player_id": player_id,
                "display_name": player["display_name"],
                "position": player["position"],
                "projection": player["projection"],
                "positional_value": round(scarcity, 1),
                "trade_value": round(value, 1),
            })
    received = sum(p["trade_value"] for p in details if p["side"] == "receiving")
    offered = sum(p["trade_value"] for p in details if p["side"] == "offering")
    difference = round(received - offered, 1)
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
        "verdict": "Strong accept" if difference >= 20 else "Accept" if difference >= 5 else "Close" if difference > -5 else "Decline",
        "players": details,
    }
