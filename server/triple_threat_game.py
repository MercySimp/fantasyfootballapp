"""Triple Threat: 3-wheel year/stat/rank multiplayer trivia game.

Each round spins all three wheels at once to produce one shared prompt:
a season, a stat category, and a leaderboard rank (e.g. "2017 sacks... no,
2017 rushing yards, rank 10"). Every connected player privately submits a
guess for who they think finished at that rank in that stat that season.
Guesses are hidden from other players until everyone has answered, then the
round is revealed and scored for the whole room at once -- nobody sees
anyone else's guess (or the target) before that reveal.

Wheel 1 (season): 2014-2024 (11 direct segments) plus one MYSTERY segment
that, when spun, draws a random unused season from 2000-2013. Whichever
season comes up -- direct or mystery-resolved -- is permanently removed
from the pool. Since every one of the 25 seasons (2000-2024) is used
exactly once over the life of a room, the game runs for exactly 25 rounds.

Wheel 2 (stat category): 10 categories, repeatable every round. Sacks is
intentionally excluded -- there is no defensive-player data in
trivia_player_seasons yet.

Wheel 3 (rank): 10-35, repeatable every round.

Scoring: guessing the exact correct player scores -5 points (by design --
this game rewards calculated close guesses over "safe" exact locks).
Otherwise points fall off with the distance between the guessed player's
real rank in that same season/category leaderboard and the target rank. A
guess for a player who isn't on that leaderboard at all scores 0.
"""

import random
import re
import uuid
from typing import Optional

from pick_engine import FORMULA_SQL

STAT_DEFINITIONS = {
    "rushing_yards": ("rushing_yards", "Rushing Yards"),
    "receiving_yards": ("receiving_yards", "Receiving Yards"),
    "passing_yards": ("passing_yards", "Passing Yards"),
    "passing_tds": ("passing_tds", "Passing TDs"),
    "receiving_tds": ("receiving_tds", "Receiving TDs"),
    "rushing_tds": ("rushing_tds", "Rushing TDs"),
    "interceptions_thrown": ("interceptions", "Interceptions Thrown"),
    "all_purpose_yards": (FORMULA_SQL["scrimmage_yards"], "All-Purpose Yards"),
    "receptions": ("receptions", "Receptions"),
    "rushing_attempts": ("rushing_attempts", "Rushing Attempts"),
}

YEARS_PRIMARY = list(range(2014, 2025))       # 2014-2024, 11 direct segments
YEARS_MYSTERY_POOL = list(range(2000, 2014))  # 2000-2013, mystery-gated
MYSTERY_LABEL = "MYSTERY"
TOTAL_ROUNDS = len(YEARS_PRIMARY) + len(YEARS_MYSTERY_POOL)  # 25

RANK_MIN, RANK_MAX = 10, 35
RANK_RANGE = list(range(RANK_MIN, RANK_MAX + 1))

EXACT_MATCH_PENALTY = -5
MAX_CLOSE_GUESS_POINTS = 20
POINTS_PER_RANK_STEP = 2
NOT_FOUND_POINTS = 0
MIN_QUALIFYING_ROWS = RANK_MAX  # need at least 35 rows to resolve any rank 10-35

TRIPLE_THREAT_ROOMS = {}


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _leaderboard_query(stat_expr: str) -> str:
    return f"""
        SELECT player_id, MAX(display_name) AS display_name,
               SUM(COALESCE({stat_expr}, 0))::float AS value
        FROM trivia_player_seasons
        WHERE season = %s
        GROUP BY player_id
        HAVING SUM(COALESCE({stat_expr}, 0)) > 0
        ORDER BY value DESC, display_name
    """


def _get_leaderboard(cur, year: int, stat_key: str):
    stat_expr, _label = STAT_DEFINITIONS[stat_key]
    cur.execute(_leaderboard_query(stat_expr), (year,))
    return cur.fetchall()


def score_guess(target_rank: int, target_player_id, guessed_player_id, guessed_rank: Optional[int]) -> int:
    if guessed_player_id is not None and guessed_player_id == target_player_id:
        return EXACT_MATCH_PENALTY
    if guessed_rank is None:
        return NOT_FOUND_POINTS
    diff = abs(guessed_rank - target_rank)
    if diff == 0:
        # Same rank slot but somehow a different player id (shouldn't happen
        # with clean data) -- treat as an exact hit for scoring purposes.
        return EXACT_MATCH_PENALTY
    return max(MAX_CLOSE_GUESS_POINTS - (POINTS_PER_RANK_STEP * diff), 1)


class YearWheelState:
    def __init__(self):
        self.remaining_primary = list(YEARS_PRIMARY)
        self.remaining_mystery = list(YEARS_MYSTERY_POOL)

    def available_segments(self):
        segments = [str(y) for y in self.remaining_primary]
        if self.remaining_mystery:
            segments.append(MYSTERY_LABEL)
        return segments

    def is_exhausted(self) -> bool:
        return not self.remaining_primary and not self.remaining_mystery

    def spin(self):
        segments = self.available_segments()
        if not segments:
            raise ValueError("Year wheel is exhausted; the game should have ended")
        pick = random.choice(segments)
        if pick == MYSTERY_LABEL:
            year = random.choice(self.remaining_mystery)
            self.remaining_mystery.remove(year)
            return year, True
        year = int(pick)
        self.remaining_primary.remove(year)
        return year, False

    def to_dict(self):
        return {
            "years_remaining": sorted(self.remaining_primary),
            "mystery_pool_remaining": len(self.remaining_mystery),
            "rounds_remaining": len(self.remaining_primary) + len(self.remaining_mystery),
        }


def _select_round(cur, year: int):
    """Spin category + rank for a fixed year, retrying combos that don't
    have enough qualifying rows to resolve a rank 10-35 target."""
    categories = list(STAT_DEFINITIONS)
    random.shuffle(categories)
    for stat_key in categories:
        rows = _get_leaderboard(cur, year, stat_key)
        if len(rows) < MIN_QUALIFYING_ROWS:
            continue
        rank = random.choice(RANK_RANGE)
        target = rows[rank - 1]
        return stat_key, rank, rows, target
    raise ValueError(f"No stat category for {year} has enough qualifying players for Triple Threat")


def _room_public_state(room):
    round_ = room["current_round"]
    public_round = None
    if round_:
        public_round = {
            "round_number": round_["round_number"],
            "year": round_["year"],
            "was_mystery": round_["was_mystery"],
            "stat_label": STAT_DEFINITIONS[round_["stat_key"]][1],
            "rank": round_["rank"],
            "revealed": round_["revealed"],
            "players_answered": list(round_["guesses"]),
        }
        if round_["revealed"]:
            public_round["target_player"] = round_["target_player"]
            public_round["results"] = round_["results"]

    return {
        "code": room["code"],
        "is_public": room["is_public"],
        "status": room["status"],
        "round_index": room["round_index"],
        "total_rounds": TOTAL_ROUNDS,
        "wheel": room["year_wheel"].to_dict(),
        "current_round": public_round,
        "history": room["history"],
        "winner": room["winner"],
        "players": [
            {
                "display_name": name,
                "is_host": player["is_host"],
                "connected": player["connected"],
                "score": player["score"],
                "has_answered_current_round": name in (round_["guesses"] if round_ else {}),
            }
            for name, player in room["players"].items()
        ],
    }


def create_room(is_public=False):
    code = uuid.uuid4().hex[:5].upper()
    while code in TRIPLE_THREAT_ROOMS:
        code = uuid.uuid4().hex[:5].upper()
    room = {
        "code": code,
        "is_public": bool(is_public),
        "status": "waiting",
        "players": {},
        "year_wheel": YearWheelState(),
        "round_index": 0,
        "current_round": None,
        "history": [],
        "winner": None,
    }
    TRIPLE_THREAT_ROOMS[code] = room
    return room


def get_room(code):
    return TRIPLE_THREAT_ROOMS.get((code or "").upper())


def list_public_rooms():
    return [
        {"code": room["code"], "player_count": len(room["players"]), "status": room["status"]}
        for room in TRIPLE_THREAT_ROOMS.values()
        if room["is_public"] and room["status"] == "waiting"
    ]


def join_room(code, display_name, websocket=None):
    room = get_room(code)
    name = (display_name or "").strip()
    if not room:
        raise ValueError("Triple Threat room not found")
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
            "score": 0,
        }
    room["players"][name] = player
    return room


def leave_room(room, display_name):
    player = room["players"].get(display_name)
    if player:
        player["connected"] = False


def start_room(conn, room, display_name):
    if room["status"] != "waiting":
        raise ValueError("This Triple Threat room has already started")
    player = room["players"].get(display_name)
    if not player or not player["is_host"]:
        raise ValueError("Only the host can start Triple Threat")
    if not room["players"]:
        raise ValueError("At least one player is required")
    room["status"] = "playing"
    _spin_new_round(conn, room)


def _spin_new_round(conn, room):
    with conn.cursor() as cur:
        year, was_mystery = room["year_wheel"].spin()
        stat_key, rank, rows, target = _select_round(cur, year)

    room["round_index"] += 1
    room["current_round"] = {
        "round_number": room["round_index"],
        "year": year,
        "was_mystery": was_mystery,
        "stat_key": stat_key,
        "rank": rank,
        "leaderboard": rows,
        "target_player": target["display_name"],
        "target_player_id": target["player_id"],
        "guesses": {},
        "revealed": False,
        "results": None,
    }


def submit_room_guess(room, display_name: str, guess_text: str):
    round_ = room["current_round"]
    if room["status"] != "playing" or not round_:
        raise ValueError("Triple Threat is not accepting guesses right now")
    if round_["revealed"]:
        raise ValueError("This round has already been revealed")
    if display_name not in room["players"]:
        raise ValueError("You are not in this room")
    if display_name in round_["guesses"]:
        raise ValueError("You have already submitted a guess for this round")

    round_["guesses"][display_name] = (guess_text or "").strip()

    connected_players = [name for name, p in room["players"].items() if p["connected"]]
    all_answered = all(name in round_["guesses"] for name in connected_players)
    revealed = False
    if all_answered:
        _reveal_round(room)
        revealed = True
    return revealed


def _reveal_round(room):
    round_ = room["current_round"]
    rows = round_["leaderboard"]
    by_normalized = {_normalize_name(row["display_name"]): row for row in rows}

    results = {}
    for display_name, guess_text in round_["guesses"].items():
        normalized = _normalize_name(guess_text)
        matched = by_normalized.get(normalized)
        guessed_rank = None
        guessed_player_id = None
        guessed_display = guess_text
        if matched:
            guessed_player_id = matched["player_id"]
            guessed_display = matched["display_name"]
            guessed_rank = rows.index(matched) + 1
        points = score_guess(round_["rank"], round_["target_player_id"], guessed_player_id, guessed_rank)
        room["players"][display_name]["score"] += points
        results[display_name] = {
            "guess": guessed_display,
            "guessed_rank": guessed_rank,
            "points": points,
        }

    round_["revealed"] = True
    round_["results"] = results
    round_.pop("leaderboard", None)

    room["history"].insert(0, {
        "round_number": round_["round_number"],
        "year": round_["year"],
        "was_mystery": round_["was_mystery"],
        "stat_label": STAT_DEFINITIONS[round_["stat_key"]][1],
        "rank": round_["rank"],
        "target_player": round_["target_player"],
        "results": results,
    })

    if room["year_wheel"].is_exhausted():
        room["status"] = "complete"
        best_score = max((p["score"] for p in room["players"].values()), default=None)
        winners = [name for name, p in room["players"].items() if p["score"] == best_score]
        room["winner"] = winners[0] if len(winners) == 1 else winners


def advance_round(conn, room, display_name):
    """Host-triggered transition to the next spin after a reveal."""
    player = room["players"].get(display_name)
    if not player or not player["is_host"]:
        raise ValueError("Only the host can advance to the next round")
    if room["status"] != "playing":
        raise ValueError("Triple Threat is not currently playing")
    if not room["current_round"] or not room["current_round"]["revealed"]:
        raise ValueError("The current round hasn't been revealed yet")
    if room["year_wheel"].is_exhausted():
        raise ValueError("All years have been used; the game is over")
    _spin_new_round(conn, room)
