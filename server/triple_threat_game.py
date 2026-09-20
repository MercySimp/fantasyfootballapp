"""Triple Threat: 3-wheel year/stat/rank multiplayer trivia game.

Each round spins all three wheels at once to produce one shared prompt:
a season, a stat category, and a leaderboard rank (e.g. "2017 rushing
yards, rank 10"). Every connected player privately submits a guess for who
they think finished at that rank in that stat that season. Guesses are
hidden from other players until everyone has answered, then the round is
revealed and scored for the whole room at once -- nobody sees anyone
else's guess (or the target) before that reveal.

Wheel 1 (season): a configurable range of "direct" seasons (default
2014-2024, 11 segments) plus one MYSTERY segment that, when spun, draws a
random unused season from a configurable older pool (default 2000-2013)
and is then permanently removed from the wheel -- mystery can only ever be
selected ONCE per room, not repeatedly. A room therefore runs for exactly
(number of direct seasons) + (1 if mystery is enabled else 0) rounds --
12 rounds with the defaults.

Wheel 2 (stat category): a configurable subset of the categories below,
repeatable every round. Sacks requires def_sacks (defensive stats) to be
imported via import_data.py; all-purpose yards reuses
pick_engine.FORMULA_SQL["scrimmage_yards"].

Wheel 3 (rank): a configurable range (default 10-35), repeatable every
round.

Scoring: points equal the absolute distance between the guessed player's
real rank on that season/category leaderboard and the target rank -- e.g.
the wheel lands on rank 16 and you guess a player who actually finished
6th, that's |16 - 6| = 10 points. Guessing the exact correct player (a
"safe lock") scores 0 -- by design, this rewards calculated near-misses
over playing it safe. A guess for a player who isn't on that leaderboard
at all also scores 0.
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
    "rushing_attempts": ("carries", "Rushing Attempts"),
    "sacks": ("def_sacks", "Sacks"),
}

# Fixed canonical order -- used to keep the stat wheel's slice order stable
# regardless of what subset a room's settings enable.
STAT_KEY_ORDER = list(STAT_DEFINITIONS.keys())

MYSTERY_LABEL = "MYSTERY"
NOT_FOUND_POINTS = 0
NEARBY_WINDOW = 3  # how many ranks above/below the target to show on reveal

DEFAULT_YEAR_START = 2014
DEFAULT_YEAR_END = 2024
DEFAULT_MYSTERY_ENABLED = True
DEFAULT_MYSTERY_START = 2000
DEFAULT_MYSTERY_END = 2013
DEFAULT_RANK_MIN = 10
DEFAULT_RANK_MAX = 35

MIN_YEAR, MAX_YEAR = 1970, 2025
MIN_RANK_FLOOR, MAX_RANK_CEIL = 1, 60

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


def _get_leaderboard(cur, year: int, stat_expr: str):
    cur.execute(_leaderboard_query(stat_expr), (year,))
    return cur.fetchall()


def score_guess(target_rank: int, guessed_rank: Optional[int]) -> int:
    """Points = absolute distance between the guessed player's real rank
    and the target rank. Exact match and "not on the leaderboard" both
    score 0 -- the former by design (no reward for playing it safe), the
    latter because there's no real rank to measure a distance from."""
    if guessed_rank is None:
        return NOT_FOUND_POINTS
    return abs(guessed_rank - target_rank)


def resolve_settings(raw_settings: Optional[dict]) -> dict:
    """Validate and normalize room settings, filling in defaults for
    anything missing or out of range. Never raises -- always returns a
    usable settings dict."""
    raw_settings = raw_settings or {}

    def _clamp_int(value, default, low, high):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    year_start = _clamp_int(raw_settings.get("year_start"), DEFAULT_YEAR_START, MIN_YEAR, MAX_YEAR)
    year_end = _clamp_int(raw_settings.get("year_end"), DEFAULT_YEAR_END, MIN_YEAR, MAX_YEAR)
    if year_start > year_end:
        year_start, year_end = year_end, year_start

    mystery_enabled = bool(raw_settings.get("mystery_enabled", DEFAULT_MYSTERY_ENABLED))
    mystery_start = _clamp_int(raw_settings.get("mystery_start"), DEFAULT_MYSTERY_START, MIN_YEAR, MAX_YEAR)
    mystery_end = _clamp_int(raw_settings.get("mystery_end"), DEFAULT_MYSTERY_END, MIN_YEAR, MAX_YEAR)
    if mystery_start > mystery_end:
        mystery_start, mystery_end = mystery_end, mystery_start

    rank_min = _clamp_int(raw_settings.get("rank_min"), DEFAULT_RANK_MIN, MIN_RANK_FLOOR, MAX_RANK_CEIL)
    rank_max = _clamp_int(raw_settings.get("rank_max"), DEFAULT_RANK_MAX, MIN_RANK_FLOOR, MAX_RANK_CEIL)
    if rank_max < rank_min:
        rank_min, rank_max = rank_max, rank_min
    if rank_max == rank_min:
        rank_max = min(MAX_RANK_CEIL, rank_min + 1)

    requested_stats = raw_settings.get("stat_keys")
    if isinstance(requested_stats, (list, tuple, set)):
        chosen = {k for k in requested_stats if k in STAT_DEFINITIONS}
    else:
        chosen = set()
    if not chosen:
        chosen = set(STAT_KEY_ORDER)
    stat_keys = [k for k in STAT_KEY_ORDER if k in chosen]

    return {
        "year_start": year_start,
        "year_end": year_end,
        "mystery_enabled": mystery_enabled,
        "mystery_start": mystery_start,
        "mystery_end": mystery_end,
        "rank_min": rank_min,
        "rank_max": rank_max,
        "stat_keys": stat_keys,
    }


class YearWheelState:
    def __init__(self, settings: dict):
        self.remaining_primary = list(range(settings["year_start"], settings["year_end"] + 1))
        self._initial_primary_count = len(self.remaining_primary)
        self.mystery_enabled = settings["mystery_enabled"]
        self.remaining_mystery = (
            list(range(settings["mystery_start"], settings["mystery_end"] + 1))
            if self.mystery_enabled else []
        )
        self.mystery_used = False

    def _mystery_available(self) -> bool:
        return self.mystery_enabled and not self.mystery_used and bool(self.remaining_mystery)

    def available_segments(self):
        segments = [str(y) for y in self.remaining_primary]
        if self._mystery_available():
            segments.append(MYSTERY_LABEL)
        return segments

    def is_exhausted(self) -> bool:
        return not self.remaining_primary and not self._mystery_available()

    def spin(self):
        segments = self.available_segments()
        if not segments:
            raise ValueError("Year wheel is exhausted; the game should have ended")
        pick = random.choice(segments)
        if pick == MYSTERY_LABEL:
            year = random.choice(self.remaining_mystery)
            self.remaining_mystery.remove(year)
            # Mystery can only ever be selected once per room -- remove it
            # from the wheel permanently regardless of pool size left.
            self.mystery_used = True
            return year, True
        year = int(pick)
        self.remaining_primary.remove(year)
        return year, False

    def total_rounds(self) -> int:
        return self._initial_primary_count + (1 if self.mystery_enabled else 0)

    def to_dict(self):
        return {
            "years_remaining": sorted(self.remaining_primary),
            "mystery_available": self._mystery_available(),
            "rounds_remaining": len(self.remaining_primary) + (1 if self._mystery_available() else 0),
        }


def _select_round(cur, year: int, settings: dict):
    """Spin category + rank for a fixed year, retrying combos that don't
    have enough qualifying rows to resolve a target rank within the
    room's configured rank range."""
    rank_min, rank_max = settings["rank_min"], settings["rank_max"]
    rank_range = list(range(rank_min, rank_max + 1))
    min_rows = rank_max
    categories = list(settings["stat_keys"])
    random.shuffle(categories)
    for stat_key in categories:
        stat_expr, _label = STAT_DEFINITIONS[stat_key]
        rows = _get_leaderboard(cur, year, stat_expr)
        if len(rows) < min_rows:
            continue
        rank = random.choice(rank_range)
        target = rows[rank - 1]
        return stat_key, rank, rows, target
    raise ValueError(
        f"No enabled stat category for {year} has enough qualifying players for rank {rank_min}-{rank_max}"
    )


def _room_public_state(room):
    round_ = room["current_round"]
    settings = room["settings"]
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
            # The set of segments that existed on the year wheel at the
            # moment this round was spun (before that segment was removed
            # from the pool), so the client can render every possible
            # outcome as a slice, not just the one that landed.
            "year_wheel_options": round_["year_wheel_options"],
        }
        if round_["revealed"]:
            public_round["target_player"] = round_["target_player"]
            public_round["target_value"] = round_["target_value"]
            public_round["results"] = round_["results"]
            public_round["nearby_leaderboard"] = round_["nearby_leaderboard"]

    return {
        "code": room["code"],
        "is_public": room["is_public"],
        "status": room["status"],
        "round_index": room["round_index"],
        "total_rounds": room["year_wheel"].total_rounds(),
        "wheel": room["year_wheel"].to_dict(),
        "settings": settings,
        "stat_wheel_options": [STAT_DEFINITIONS[k][1] for k in settings["stat_keys"]],
        "rank_wheel_options": list(range(settings["rank_min"], settings["rank_max"] + 1)),
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


def create_room(is_public=False, settings=None):
    code = uuid.uuid4().hex[:5].upper()
    while code in TRIPLE_THREAT_ROOMS:
        code = uuid.uuid4().hex[:5].upper()
    resolved_settings = resolve_settings(settings)
    room = {
        "code": code,
        "is_public": bool(is_public),
        "status": "waiting",
        "players": {},
        "settings": resolved_settings,
        "year_wheel": YearWheelState(resolved_settings),
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
    # Snapshot the year wheel's available segments BEFORE spinning, so the
    # client can render the full set of possible outcomes for this round
    # (the wheel object itself removes the drawn year immediately below).
    year_wheel_options = room["year_wheel"].available_segments()

    with conn.cursor() as cur:
        year, was_mystery = room["year_wheel"].spin()
        stat_key, rank, rows, target = _select_round(cur, year, room["settings"])

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
        "target_value": target["value"],
        "guesses": {},
        "revealed": False,
        "results": None,
        "nearby_leaderboard": None,
        "year_wheel_options": year_wheel_options,
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
        guessed_value = None
        guessed_display = guess_text
        if matched:
            guessed_display = matched["display_name"]
            guessed_rank = rows.index(matched) + 1
            guessed_value = matched["value"]
        points = score_guess(round_["rank"], guessed_rank)
        room["players"][display_name]["score"] += points
        results[display_name] = {
            "guess": guessed_display,
            "guessed_rank": guessed_rank,
            "guessed_value": guessed_value,
            "points": points,
        }

    # A small window of the real leaderboard around the target rank, so
    # players can see who was actually near the answer (and by how much)
    # once the round reveals, not just their own guess's outcome.
    target_rank = round_["rank"]
    lo = max(0, target_rank - 1 - NEARBY_WINDOW)
    hi = min(len(rows), target_rank + NEARBY_WINDOW)
    nearby_leaderboard = [
        {"rank": idx + 1, "player": rows[idx]["display_name"], "value": rows[idx]["value"]}
        for idx in range(lo, hi)
    ]

    round_["revealed"] = True
    round_["results"] = results
    round_["nearby_leaderboard"] = nearby_leaderboard
    round_.pop("leaderboard", None)

    room["history"].insert(0, {
        "round_number": round_["round_number"],
        "year": round_["year"],
        "was_mystery": round_["was_mystery"],
        "stat_label": STAT_DEFINITIONS[round_["stat_key"]][1],
        "rank": round_["rank"],
        "target_player": round_["target_player"],
        "target_value": round_["target_value"],
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
