"""Darts-style career-stat game.

Each game has one fixed prompt. Players identify qualifying NFL players and
subtract their career value from a starting total calibrated from the top 25
answers in that prompt's pool.
"""

import random
import re
import uuid
from typing import Optional


STAT_DEFINITIONS = {
    "interceptions": ("interceptions", "interceptions"),
    "passing_yards": ("passing_yards", "passing yards"),
    "passing_tds": ("passing_tds", "passing touchdowns"),
    "rushing_yards": ("rushing_yards", "rushing yards"),
    "rushing_tds": ("rushing_tds", "rushing touchdowns"),
    "receiving_yards": ("receiving_yards", "receiving yards"),
    "receiving_tds": ("receiving_tds", "receiving touchdowns"),
    "receptions": ("receptions", "receptions"),
}

TEAM_LABELS = {
    "NYJ": "New York Jets",
    "NE": "New England Patriots",
    "BUF": "Buffalo Bills",
    "MIA": "Miami Dolphins",
    "DAL": "Dallas Cowboys",
    "GB": "Green Bay Packers",
    "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers",
}

GAMES = {}
DARTS_ROOMS = {}


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _prompt_title(stat_label: str, team_label: str) -> str:
    return f"Career {stat_label} with the {team_label}"


def _pool_query(stat_column: str) -> str:
    return f"""
        SELECT s.player_id, MAX(p.display_name) AS display_name,
               SUM(COALESCE(s.{stat_column}, 0))::float AS value
        FROM trivia_player_seasons s
        JOIN players p ON p.player_id = s.player_id
        WHERE s.team = %s
        GROUP BY s.player_id
        HAVING SUM(COALESCE(s.{stat_column}, 0)) > 0
        ORDER BY value DESC, display_name
    """


def _select_prompt(cur):
    candidates = list(STAT_DEFINITIONS.items())
    random.shuffle(candidates)
    teams = list(TEAM_LABELS)
    random.shuffle(teams)
    for stat_key, (stat_column, stat_label) in candidates:
        for team_abbr in teams:
            cur.execute(_pool_query(stat_column), (team_abbr,))
            rows = cur.fetchall()
            if len(rows) >= 5:
                return stat_key, stat_column, stat_label, team_abbr, rows
    raise ValueError("No darts prompt has enough qualifying player data")


def start_game(conn):
    with conn.cursor() as cur:
        stat_key, stat_column, stat_label, team_abbr, rows = _select_prompt(cur)

    top25 = rows[:25]
    start_score = int(round(sum(float(row["value"] or 0) for row in top25)))
    game_id = uuid.uuid4().hex
    game = {
        "game_id": game_id,
        "stat_key": stat_key,
        "stat_column": stat_column,
        "stat_label": stat_label,
        "team_abbr": team_abbr,
        "team_label": TEAM_LABELS[team_abbr],
        "title": _prompt_title(stat_label, TEAM_LABELS[team_abbr]),
        "start_score": start_score,
        "remaining": start_score,
        "answers": {},
        "history": [],
        "status": "playing",
    }
    GAMES[game_id] = game
    return _public_state(game)


def _public_state(game):
    return {
        "game_id": game["game_id"],
        "title": game["title"],
        "stat_label": game["stat_label"],
        "team": game["team_label"],
        "start_score": game["start_score"],
        "remaining": game["remaining"],
        "history": game["history"],
        "status": game["status"],
    }


def submit_answer(conn, game_id: str, answer: str):
    game = GAMES.get(game_id)
    if not game:
        raise ValueError("Darts game not found or expired")
    if game["status"] != "playing":
        raise ValueError("This darts game is already complete")
    normalized = _normalize_name(answer)
    if len(normalized) < 2:
        raise ValueError("Enter a player name")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.player_id, MAX(p.display_name) AS display_name,
                   SUM(COALESCE(s.{game['stat_column']}, 0))::float AS value
            FROM trivia_player_seasons s
            JOIN players p ON p.player_id = s.player_id
            WHERE s.team = %s
            GROUP BY s.player_id
            HAVING SUM(COALESCE(s.{game['stat_column']}, 0)) > 0
            """,
            (game["team_abbr"],),
        )
        rows = cur.fetchall()

    match = next(
        (row for row in rows if _normalize_name(row["display_name"]) == normalized),
        None,
    )
    if not match:
        return {"valid": False, "reason": "That player is not a qualifying answer for this prompt", **_public_state(game)}
    if match["player_id"] in game["answers"]:
        return {"valid": False, "reason": "That player has already been used", **_public_state(game)}

    value = int(round(float(match["value"] or 0)))
    previous = game["remaining"]
    bust = value > previous
    if not bust:
        game["remaining"] = previous - value
    game["answers"][match["player_id"]] = True
    event = {
        "player": match["display_name"],
        "value": value,
        "previous": previous,
        "remaining": game["remaining"] if not bust else previous,
        "bust": bust,
    }
    game["history"].insert(0, event)
    if game["remaining"] == 0:
        game["status"] = "won"

    return {"valid": True, "answer": event, **_public_state(game)}


def _room_public_state(room):
    """Return a client-safe snapshot for the multiplayer protocol."""
    return {
        "code": room["code"],
        "is_public": room["is_public"],
        "status": room["status"],
        "prompt": {
            "title": room["title"],
            "stat_label": room["stat_label"],
            "team": room["team_label"],
            "start_score": room["start_score"],
        },
        "start_score": room["start_score"],
        "remaining_scores": {
            name: player["remaining"] for name, player in room["players"].items()
        },
        "remaining": (
            room["players"].get(room["current_player"], {}).get("remaining")
            if room["current_player"] else None
        ),
        "history": room["history"],
        "turn_order": room["turn_order"],
        "current_player": room["current_player"],
        "winner": room["winner"],
        "players": [
            {
                "display_name": name,
                "is_host": player["is_host"],
                "connected": player["connected"],
                "remaining": player["remaining"],
            }
            for name, player in room["players"].items()
        ],
    }


def create_room(conn, is_public=False):
    """Create a lobby and choose its one immutable prompt."""
    with conn.cursor() as cur:
        stat_key, stat_column, stat_label, team_abbr, rows = _select_prompt(cur)
    top25 = rows[:25]
    start_score = int(round(sum(float(row["value"] or 0) for row in top25)))
    code = uuid.uuid4().hex[:5].upper()
    while code in DARTS_ROOMS:
        code = uuid.uuid4().hex[:5].upper()
    room = {
        "code": code,
        "is_public": bool(is_public),
        "status": "waiting",
        "stat_key": stat_key,
        "stat_column": stat_column,
        "stat_label": stat_label,
        "team_abbr": team_abbr,
        "team_label": TEAM_LABELS[team_abbr],
        "title": _prompt_title(stat_label, TEAM_LABELS[team_abbr]),
        "start_score": start_score,
        "players": {},
        "turn_order": [],
        "current_player": None,
        "history": [],
        "used_answers": set(),
        "winner": None,
    }
    DARTS_ROOMS[code] = room
    return room


def get_room(code):
    return DARTS_ROOMS.get((code or "").upper())


def list_public_rooms():
    return [
        {
            "code": room["code"],
            "player_count": len(room["players"]),
            "status": room["status"],
            "prompt": room["title"],
        }
        for room in DARTS_ROOMS.values()
        if room["is_public"] and room["status"] == "waiting"
    ]


def join_room(code, display_name, websocket=None):
    room = get_room(code)
    name = (display_name or "").strip()
    if not room:
        raise ValueError("Darts room not found")
    if not name or len(name) > 40:
        raise ValueError("Enter a display name (1-40 characters)")
    player = room["players"].get(name)
    if player and player["connected"] and player.get("websocket"):
        raise ValueError("That name is already in this room")
    if player:
        player["connected"] = True
        player["websocket"] = websocket
    else:
        player = {
            "is_host": not room["players"],
            "connected": True,
            "websocket": websocket,
            "remaining": room["start_score"],
        }
        room["players"][name] = player
        room["turn_order"].append(name)
    return room


def leave_room(room, display_name):
    player = room["players"].get(display_name)
    if player:
        player["connected"] = False


def start_room(room, display_name):
    if room["status"] != "waiting":
        raise ValueError("This darts room has already started")
    player = room["players"].get(display_name)
    if not player or not player["is_host"]:
        raise ValueError("Only the host can start the darts game")
    if not room["turn_order"]:
        raise ValueError("At least one player is required")
    room["status"] = "playing"
    room["current_player"] = room["turn_order"][0]


def submit_room_answer(conn, room, display_name, answer):
    if room["status"] != "playing":
        raise ValueError("This darts room is not accepting answers")
    if room["current_player"] != display_name:
        raise ValueError("It is not your turn")
    result = _find_room_answer(conn, room, answer)
    if not result["valid"]:
        return result
    player_id, player_name, value = result["player_id"], result["player"], result["value"]
    if player_id in room["used_answers"]:
        return {"valid": False, "reason": "That player has already been used"}
    previous = room["players"][display_name]["remaining"]
    bust = value > previous
    remaining = previous if bust else previous - value
    room["players"][display_name]["remaining"] = remaining
    room["used_answers"].add(player_id)
    event = {
        "display_name": display_name, "player": player_name, "value": value,
        "previous": previous, "remaining": remaining, "bust": bust,
    }
    room["history"].insert(0, event)
    if remaining == 0:
        room["status"], room["winner"] = "won", display_name
        room["current_player"] = None
    else:
        index = room["turn_order"].index(display_name)
        room["current_player"] = room["turn_order"][(index + 1) % len(room["turn_order"])]
    return {"valid": True, "answer": event}


def _find_room_answer(conn, room, answer):
    normalized = _normalize_name(answer)
    if len(normalized) < 2:
        return {"valid": False, "reason": "Enter a player name"}
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT s.player_id, MAX(p.display_name) AS display_name,
                       SUM(COALESCE(s.{room['stat_column']}, 0))::float AS value
                FROM trivia_player_seasons s JOIN players p ON p.player_id = s.player_id
                WHERE s.team = %s GROUP BY s.player_id
                HAVING SUM(COALESCE(s.{room['stat_column']}, 0)) > 0""",
            (room["team_abbr"],),
        )
        rows = cur.fetchall()
    match = next((row for row in rows if _normalize_name(row["display_name"]) == normalized), None)
    if not match:
        return {"valid": False, "reason": "That player is not a qualifying answer for this prompt"}
    return {
        "valid": True, "player_id": match["player_id"],
        "player": match["display_name"], "value": int(round(float(match["value"] or 0))),
    }
