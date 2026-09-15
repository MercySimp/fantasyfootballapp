"""
In-memory multiplayer snake-draft room manager.

Name Train is tracked PER PLAYER, not per room. Each player has their own
independent letter chain based only on their own previous successful
picks -- one player's pick never constrains what letter another player
must start with on their turn. Only a player's own prior pick in this
draft feeds their own next required letter.
"""

import asyncio
import random
import string
import time
from typing import Optional

from fastapi import WebSocket

import pick_engine
import question_engine

ROOM_CODE_LENGTH = 5
ROOM_CODE_CHARS = string.ascii_uppercase + string.digits
DUPLICATE_PLAYER_REASON = "That player has already been drafted by someone else in this room"


def _generate_room_code(existing_codes: set) -> str:
    while True:
        code = "".join(random.choices(ROOM_CODE_CHARS, k=ROOM_CODE_LENGTH))
        if code not in existing_codes:
            return code


def _brick_pick(reason: str, scoring_mode: str) -> dict:
    score = pick_engine.GOLF_BRICK_SCORE if scoring_mode == "golf" else 0.0
    return {"player": "Brick", "player_id": None, "team": "", "season": "", "position": "", "fantasy_points": score, "grade_label": reason, "grade_color": "#ffcc56", "pool_rank": None, "pool_size": None, "percentile": None, "missed": True}


class Player:
    def __init__(self, display_name: str, websocket: WebSocket, is_host=False):
        self.display_name = display_name
        self.websocket = websocket
        self.is_host = is_host
        self.connected = True
        self.picks = {}
        self.missed_current_turn = False
        # Name Train state is per-player: only THIS player's own prior
        # successful pick determines what letter THEY need next.
        self.name_train_required_letter: Optional[str] = None
        self.name_train_last_player: Optional[str] = None

    def total_points(self):
        return sum((pick.get("fantasy_points") or 0) for pick in self.picks.values())

    def to_dict(self):
        return {
            "display_name": self.display_name,
            "is_host": self.is_host,
            "connected": self.connected,
            "picks": self.picks,
            "total_points": round(self.total_points(), 2),
            "missed_current_turn": self.missed_current_turn,
            "name_train_required_letter": self.name_train_required_letter,
            "name_train_last_player": self.name_train_last_player,
        }


class Room:
    def __init__(self, code: str, is_public: bool, settings: dict):
        self.code = code
        self.is_public = is_public
        self.settings = settings
        self.players = {}
        self.status = "waiting"
        self.current_slot_idx = 0
        self.turn_index = 0
        self.draft_order = []
        self.on_the_clock: Optional[str] = None
        self.drafted_player_ids = set()
        self.current_question = None
        self.timer_end = None
        self._timer_task = None
        self._turn_token = 0

    def current_order(self):
        return self.draft_order if self.current_slot_idx % 2 == 0 else list(reversed(self.draft_order))

    def prior_picks(self):
        for player in self.players.values():
            if player.picks:
                return {key: value.get("player_id") for key, value in player.picks.items() if value.get("player_id")}
        return {}

    def to_dict(self):
        return {
            "code": self.code, "is_public": self.is_public, "settings": self.settings,
            "status": self.status, "current_slot_idx": self.current_slot_idx,
            "turn_index": self.turn_index, "draft_order": self.draft_order,
            "on_the_clock": self.on_the_clock, "drafted_player_count": len(self.drafted_player_ids),
            "slot_order": question_engine.SLOT_ORDER, "current_question": self.current_question,
            "timer_end": self.timer_end,
            "players": [player.to_dict() for player in self.players.values()],
        }


class RoomManager:
    def __init__(self): self.rooms = {}
    def create_room(self, is_public, settings):
        room = Room(_generate_room_code(set(self.rooms)), is_public, settings)
        self.rooms[room.code] = room
        return room
    def get_room(self, code): return self.rooms.get(code.upper())
    def list_public_rooms(self):
        return [{"code": room.code, "player_count": len(room.players), "status": room.status, "settings": room.settings} for room in self.rooms.values() if room.is_public and room.status == "waiting"]
    def close_room_if_empty(self, room):
        if not any(player.connected for player in room.players.values()): self.rooms.pop(room.code, None)


manager = RoomManager()


async def _broadcast(room, message):
    for player in room.players.values():
        if player.connected:
            try: await player.websocket.send_json(message)
            except Exception: player.connected = False


async def broadcast_room_state(room):
    await _broadcast(room, {"type": "room_state", "room": room.to_dict()})


async def start_draft(room, get_conn):
    room.status, room.current_slot_idx, room.turn_index = "drafting", 0, 0
    room.drafted_player_ids = set()
    room.draft_order = [name for name, player in room.players.items() if player.connected]
    for player in room.players.values():
        player.picks, player.missed_current_turn = {}, False
        player.name_train_required_letter = None
        player.name_train_last_player = None
    await _start_turn(room, get_conn)


async def _new_question(room, get_conn):
    slot = question_engine.SLOT_ORDER[room.current_slot_idx]
    difficulty = None if room.settings.get("mode", "ramp") == "ramp" else room.settings.get("mode")
    with get_conn() as conn, conn.cursor() as cur:
        room.current_question = question_engine.build_question_for_slot(
            cur, slot["key"], difficulty, room.settings.get("scope_mode", "mixed"), room.prior_picks(),
            room.settings.get("allow_college", True), room.settings.get("board_type", "standard"),
            room.settings.get("name_train", False),
        )


async def _start_turn(room, get_conn):
    if room.current_slot_idx >= len(question_engine.SLOT_ORDER):
        room.status, room.on_the_clock = "complete", None
        await _broadcast(room, {"type": "draft_complete"})
        await broadcast_room_state(room)
        return
    if room.turn_index == 0: await _new_question(room, get_conn)
    order = room.current_order()
    if not order:
        room.status = "complete"
        await _broadcast(room, {"type": "draft_complete"})
        return
    room.on_the_clock = order[room.turn_index]
    player = room.players[room.on_the_clock]
    player.missed_current_turn = False
    room._turn_token += 1
    token = room._turn_token
    seconds = room.settings.get("timer_seconds", 0)
    room.timer_end = time.time() + seconds if seconds else None
    await _broadcast(room, {
        "type": "turn_start", **room.current_question,
        "on_the_clock": room.on_the_clock, "timer_end": room.timer_end,
        # This player's OWN required letter, based only on their own prior picks.
        "name_train_required_letter": player.name_train_required_letter,
        "name_train_last_player": player.name_train_last_player,
    })
    await broadcast_room_state(room)
    if seconds:
        if room._timer_task: room._timer_task.cancel()
        room._timer_task = asyncio.create_task(_timer(room, get_conn, seconds, token))


async def _timer(room, get_conn, seconds, token):
    try: await asyncio.sleep(seconds)
    except asyncio.CancelledError: return
    if room.status != "drafting" or room._turn_token != token: return
    player = room.players.get(room.on_the_clock)
    if not player: return
    if room.settings.get("miss_policy", "zero_skip") == "zero_skip" or not player.connected:
        slot = question_engine.SLOT_ORDER[room.current_slot_idx]
        player.picks[slot["key"]] = _brick_pick("Timed Out", room.settings.get("scoring_mode", "fantasy"))
        await _broadcast(room, {"type": "turn_miss", "display_name": player.display_name, "reason": "timeout"})
        await _advance(room, get_conn)
        return
    player.missed_current_turn = True
    await _broadcast(room, {"type": "turn_retry", "display_name": player.display_name, "penalty_active": room.settings.get("miss_policy") == "retry_penalty"})
    room._turn_token += 1
    room.timer_end = time.time() + seconds
    await broadcast_room_state(room)
    room._timer_task = asyncio.create_task(_timer(room, get_conn, seconds, room._turn_token))


async def _advance(room, get_conn):
    room.turn_index += 1
    if room.turn_index >= len(room.current_order()):
        room.current_slot_idx += 1
        room.turn_index = 0
    await _start_turn(room, get_conn)


async def submit_pick(room, get_conn, player, player_id, season):
    if room.status != "drafting" or player.display_name != room.on_the_clock:
        await player.websocket.send_json({"type": "error", "message": "It is not your turn"})
        return
    if player_id in room.drafted_player_ids:
        await player.websocket.send_json({"type": "pick_rejected", "reason": DUPLICATE_PLAYER_REASON, "policy": None})
        return
    question = room.current_question
    with get_conn() as conn, conn.cursor() as cur:
        if question["category"] == "NAME_TRAIN":
            result = pick_engine.build_name_train_result(cur, player_id, None, season)
        else:
            result = pick_engine.evaluate_pick(cur, question["rule_id"], question["position"], player_id, season, "ppr")
    if room.settings.get("name_train"):
        # Only THIS player's own required letter can invalidate their pick.
        result = pick_engine.check_name_train(result, player.name_train_required_letter)
    policy = room.settings.get("miss_policy", "zero_skip")
    if not result.get("valid"):
        await player.websocket.send_json({"type": "pick_rejected", "reason": result.get("reason"), "policy": policy})
        if policy == "zero_skip":
            slot = question_engine.SLOT_ORDER[room.current_slot_idx]
            player.picks[slot["key"]] = _brick_pick("Wrong Pick", room.settings.get("scoring_mode", "fantasy"))
            if room._timer_task: room._timer_task.cancel()
            await _broadcast(room, {"type": "turn_miss", "display_name": player.display_name, "reason": "wrong_pick"})
            await _advance(room, get_conn)
        elif policy == "retry_penalty":
            player.missed_current_turn = True
            await _broadcast(room, {"type": "turn_retry", "display_name": player.display_name, "penalty_active": True})
        return
    penalty = room.settings.get("miss_penalty_percent", 0) if player.missed_current_turn and policy == "retry_penalty" else 0
    if room.settings.get("scoring_mode") == "golf": result = pick_engine.apply_golf_scoring(result, penalty)
    elif penalty: result = pick_engine.apply_miss_penalty(result, penalty)
    slot = question_engine.SLOT_ORDER[room.current_slot_idx]
    player.picks[slot["key"]] = result
    room.drafted_player_ids.add(player_id)
    if room.settings.get("name_train"):
        # Update ONLY this player's own chain -- never touches other players.
        player.name_train_last_player = result["player"]
        player.name_train_required_letter = pick_engine.last_letter(result["player"])
    if room._timer_task: room._timer_task.cancel()
    await _broadcast(room, {"type": "pick_result", "display_name": player.display_name, "slot_key": slot["key"], "result": result})
    await _advance(room, get_conn)


async def pass_turn(room, get_conn, player):
    if room.status != "drafting" or player.display_name != room.on_the_clock: return
    slot = question_engine.SLOT_ORDER[room.current_slot_idx]
    player.picks[slot["key"]] = _brick_pick("Passed", room.settings.get("scoring_mode", "fantasy"))
    if room._timer_task: room._timer_task.cancel()
    await _advance(room, get_conn)
