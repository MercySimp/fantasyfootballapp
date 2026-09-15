#!/usr/bin/env python3
"""Fantasy Football Trivia / DraftForge FastAPI backend."""

import json
import os
from typing import List, Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rules import CATALOG
import dynamic_rules
import pick_engine
import question_engine
import rooms

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "fantasy_football"),
    "user": os.getenv("DB_USER", "fantasy_admin"),
    "password": os.getenv("DB_PASSWORD", "changeme_secure_password"),
}

VALID_FORMATS = {"standard", "half_ppr", "ppr"}
VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K"}
VALID_SCOPES = {"mixed", "season", "career"}
VALID_MISS_POLICIES = {"zero_skip", "retry_no_penalty", "retry_penalty"}
VALID_BOARD_TYPES = {"standard", "superflex"}
VALID_SCORING_MODES = {"fantasy", "golf"}
SLOT_ORDER = question_engine.SLOT_ORDER
FLEX_POSITIONS = question_engine.FLEX_POSITIONS


def get_conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


app = FastAPI(title="DraftForge API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class PickRequest(BaseModel):
    player_id: str
    position: Optional[str] = None
    rule_id: str
    slot_key: str
    season: Optional[int] = None
    scoring_format: str = "ppr"
    mode: str = "ramp"
    scope_mode: str = "mixed"
    excluded_player_ids: List[str] = []
    penalty_percent: float = 0
    board_type: str = "standard"
    scoring_mode: str = "fantasy"
    name_train: bool = False
    name_train_required_letter: Optional[str] = None


class CreateRoomRequest(BaseModel):
    is_public: bool = False
    mode: str = "ramp"
    scope_mode: str = "mixed"
    timer_seconds: int = 0
    allow_college: bool = True
    miss_policy: str = "zero_skip"
    miss_penalty_percent: int = 25
    board_type: str = "standard"
    scoring_mode: str = "fantasy"
    name_train: bool = False


@app.get("/api/draft/slots")
def get_slots():
    return SLOT_ORDER


@app.get("/api/draft/difficulty-options")
def difficulty_options():
    return [
        {"key": "ramp", "label": "Ramp (Easy → Hard)", "description": "Difficulty rises as the board fills."},
        {"key": "easy", "label": "Easy", "description": "Every question uses easy thresholds."},
        {"key": "medium", "label": "Medium", "description": "Every question uses medium thresholds."},
        {"key": "hard", "label": "Hard", "description": "Every question uses hard thresholds."},
    ]


@app.get("/api/draft/scope-options")
def scope_options():
    return [
        {"key": "mixed", "label": "Mixed", "description": "Single-season and career questions."},
        {"key": "season", "label": "Season Only", "description": "Every question is about a specific season."},
        {"key": "career", "label": "Full Career", "description": "Every question concerns career totals or milestones."},
    ]


@app.get("/api/draft/miss-policy-options")
def miss_policy_options():
    return [
        {"key": "zero_skip", "label": "Zero Points, Skip", "description": "A timeout or wrong pick immediately records a miss and moves on."},
        {"key": "retry_no_penalty", "label": "Retry, No Penalty", "description": "A timeout or wrong pick lets you try again without a score hit."},
        {"key": "retry_penalty", "label": "Retry, With Penalty", "description": "A timeout or wrong pick lets you retry, but the eventual score is penalized."},
    ]


@app.get("/api/draft/board-type-options")
def board_type_options():
    return [
        {"key": "standard", "label": "Standard", "description": "Normal QB, RB, WR, TE, and FLEX roster slots."},
        {"key": "superflex", "label": "Superflex (SFLX)", "description": "Every roster slot can use QB, RB, WR, or TE."},
    ]


@app.get("/api/draft/scoring-mode-options")
def scoring_mode_options():
    return [
        {"key": "fantasy", "label": "Fantasy Points", "description": "Higher total score wins."},
        {"key": "golf", "label": "Golf Rules", "description": "Lower total wins; misses and passes cost 100 strokes."},
    ]


@app.get("/api/draft/question")
def get_question(
    slot_key: str = Query(...),
    mode: str = Query("ramp"),
    scope_mode: str = Query("mixed"),
    prior_picks: Optional[str] = Query(None),
    allow_college: bool = Query(True),
    board_type: str = Query("standard"),
    name_train: bool = Query(False),
):
    if board_type not in VALID_BOARD_TYPES:
        board_type = "standard"
    difficulty_override = None if mode == "ramp" else mode
    try:
        parsed_prior_picks = json.loads(prior_picks) if prior_picks else {}
    except json.JSONDecodeError:
        parsed_prior_picks = {}
    with get_conn() as conn, conn.cursor() as cur:
        try:
            return question_engine.build_question_for_slot(
                cur, slot_key, difficulty_override, scope_mode, parsed_prior_picks,
                allow_college, board_type, name_train,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.get("/api/teams")
def teams():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT team_abbr, team_name, team_color, team_color2, team_logo_url FROM teams")
        return cur.fetchall()


@app.get("/api/players/search")
def search_players(q: str = Query(..., min_length=2), position: Optional[str] = None, limit: int = 15):
    query = """
        SELECT p.player_id, p.display_name, p.position,
               MIN(s.season) AS first_season, MAX(s.season) AS last_season,
               ARRAY_AGG(DISTINCT s.team ORDER BY s.team) FILTER (WHERE s.team IS NOT NULL) AS teams
        FROM players p
        JOIN player_stats_seasonal s ON s.player_id = p.player_id
        WHERE p.display_name ILIKE %s
    """
    params: List = [f"%{q}%"]
    if position and position in VALID_POSITIONS:
        query += " AND p.position = %s"
        params.append(position)
    query += " GROUP BY p.player_id, p.display_name, p.position ORDER BY p.display_name LIMIT %s"
    params.append(limit)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [{
        "player_id": row["player_id"], "display_name": row["display_name"], "position": row["position"],
        "first_season": row["first_season"], "last_season": row["last_season"],
        "years_label": f"{row['first_season']}-{row['last_season']}" if row["first_season"] != row["last_season"] else str(row["first_season"]),
        "teams": row["teams"] or [], "teams_label": ", ".join(row["teams"] or []),
    } for row in rows]


@app.get("/api/players/{player_id}/seasons")
def player_seasons(player_id: str, position: Optional[str] = None):
    query = """
        SELECT display_name, season, team, position, games_played,
               passing_yards, passing_tds, interceptions, rushing_yards, rushing_tds,
               receiving_yards, receiving_tds, receptions, fantasy_pts_standard,
               fantasy_pts_half_ppr, fantasy_pts_ppr, rank_standard, rank_half_ppr, rank_ppr
        FROM trivia_player_seasons WHERE player_id = %s
    """
    params: List = [player_id]
    if position and position in VALID_POSITIONS:
        query += " AND position = %s"
        params.append(position)
    query += " ORDER BY season"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        raise HTTPException(404, "No eligible seasonal data found for this player")
    return rows


@app.post("/api/draft/pick")
def validate_pick(pick: PickRequest):
    slot = next((item for item in SLOT_ORDER if item["key"] == pick.slot_key), None)
    if not slot:
        raise HTTPException(400, f"Unknown slot: {pick.slot_key}")
    if pick.player_id in pick.excluded_player_ids:
        return {"valid": False, "reason": "You already drafted this player earlier in this draft"}

    board_type = pick.board_type if pick.board_type in VALID_BOARD_TYPES else "standard"
    scoring_mode = pick.scoring_mode if pick.scoring_mode in VALID_SCORING_MODES else "fantasy"
    allowed_positions = pick_engine.superflex_allowed_positions(slot["position"], board_type)

    with get_conn() as conn, conn.cursor() as cur:
        if pick.name_train or pick.rule_id == "name_train":
            result = pick_engine.build_name_train_result(cur, pick.player_id, pick.position, pick.season, pick.scoring_format)
        else:
            if pick.position not in allowed_positions:
                return {"valid": False, "reason": f"{pick.position} is not eligible for this slot"}
            result = pick_engine.evaluate_pick(cur, pick.rule_id, pick.position, pick.player_id, pick.season, pick.scoring_format)

    if pick.name_train:
        result = pick_engine.check_name_train(result, pick.name_train_required_letter)
    if not result.get("valid"):
        return result

    result["slot_key"] = pick.slot_key
    if scoring_mode == "golf":
        result = pick_engine.apply_golf_scoring(result, pick.penalty_percent)
    elif pick.penalty_percent:
        result = pick_engine.apply_miss_penalty(result, pick.penalty_percent)
    return result


@app.get("/api/health")
def health():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM players")
            count = cur.fetchone()["c"]
        return {"status": "ok", "players_loaded": count, "trivia_rules_loaded": len(CATALOG)}
    except Exception as exc:
        raise HTTPException(500, f"Database connection failed: {exc}")


@app.post("/api/rooms")
def create_room(request: CreateRoomRequest):
    settings = {
        "mode": request.mode,
        "scope_mode": request.scope_mode if request.scope_mode in VALID_SCOPES else "mixed",
        "timer_seconds": max(0, min(600, request.timer_seconds)),
        "allow_college": request.allow_college,
        "miss_policy": request.miss_policy if request.miss_policy in VALID_MISS_POLICIES else "zero_skip",
        "miss_penalty_percent": max(0, min(100, request.miss_penalty_percent)),
        "board_type": request.board_type if request.board_type in VALID_BOARD_TYPES else "standard",
        "scoring_mode": request.scoring_mode if request.scoring_mode in VALID_SCORING_MODES else "fantasy",
        "name_train": request.name_train,
    }
    room = rooms.manager.create_room(request.is_public, settings)
    return {"code": room.code, "is_public": room.is_public, "settings": room.settings}


@app.get("/api/rooms")
def public_rooms():
    return rooms.manager.list_public_rooms()


@app.get("/api/rooms/{code}")
def room_info(code: str):
    room = rooms.manager.get_room(code)
    if not room:
        raise HTTPException(404, "Room not found")
    return room.to_dict()


@app.websocket("/ws/room/{code}")
async def room_socket(websocket: WebSocket, code: str, display_name: str = Query(...)):
    await websocket.accept()
    room = rooms.manager.get_room(code)
    if not room:
        await websocket.send_json({"type": "error", "message": "Room not found"})
        await websocket.close()
        return
    existing = room.players.get(display_name)
    if existing and existing.connected:
        await websocket.send_json({"type": "error", "message": "That name is already in this room"})
        await websocket.close()
        return
    if existing:
        existing.websocket, existing.connected = websocket, True
        player = existing
    else:
        player = rooms.Player(display_name, websocket, is_host=len(room.players) == 0)
        room.players[display_name] = player
    await rooms.broadcast_room_state(room)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "start_draft":
                if not player.is_host:
                    await websocket.send_json({"type": "error", "message": "Only the host can start the draft"})
                else:
                    await rooms.start_draft(room, get_conn)
            elif message.get("type") == "submit_pick":
                await rooms.submit_pick(room, get_conn, player, message.get("player_id"), message.get("season"))
            elif message.get("type") == "pass":
                await rooms.pass_turn(room, get_conn, player)
    except WebSocketDisconnect:
        was_on_clock = room.status == "drafting" and room.on_the_clock == player.display_name
        player.connected = False
        if was_on_clock:
            await rooms.pass_turn(room, get_conn, player)
        rooms.manager.close_room_if_empty(room)
        if rooms.manager.get_room(room.code):
            await rooms.broadcast_room_state(room)


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
