#!/usr/bin/env python3
"""Fantasy Football Trivia / DraftForge FastAPI backend."""

import json
import os
from typing import List, Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rules import CATALOG
import dynamic_rules
import darts_game
import pick_engine
import question_engine
import rooms
import trade_analyzer
import projections_fetcher
import threading

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


class CreateDartsRoomRequest(BaseModel):
    is_public: bool = False


class DartsJoinRequest(BaseModel):
    display_name: str


class TradeAnalyzeRequest(BaseModel):
    received_player_ids: List[str] = []
    offered_player_ids: List[str] = []
    league_type: str = "redraft"
    qb_format: str = "one_qb"
    scoring_format: str = "ppr"


@app.get("/api/draft/slots")
def get_slots():
    return SLOT_ORDER


@app.get("/api/trade/options")
def trade_options():
    return {
        "league_types": [
            {"key": "redraft", "label": "Redraft"},
            {"key": "dynasty", "label": "Dynasty"},
        ],
        "qb_formats": [
            {"key": "one_qb", "label": "1 QB"},
            {"key": "superflex", "label": "Superflex"},
        ],
        "scoring_formats": [
            {"key": "standard", "label": "Standard"},
            {"key": "half_ppr", "label": "Half PPR"},
            {"key": "ppr", "label": "PPR"},
        ],
    }


@app.post("/api/trade/analyze")
def analyze_trade(request: TradeAnalyzeRequest):
    if not request.received_player_ids or not request.offered_player_ids:
        raise HTTPException(400, "Add at least one player to both sides of the trade")
    if len(request.received_player_ids) > 10 or len(request.offered_player_ids) > 10:
        raise HTTPException(400, "Each side can contain at most 10 players")
    try:
        with get_conn() as conn:
            return trade_analyzer.analyze(
                conn,
                list(dict.fromkeys(request.received_player_ids)),
                list(dict.fromkeys(request.offered_player_ids)),
                request.league_type,
                request.qb_format,
                request.scoring_format,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/trade/upload-projections")
async def upload_projections(file: UploadFile = File(...)):
    """Upload a projections CSV and save it to data/projections.csv for the analyzer to use.

    Expected CSV columns: player_id, projection_standard, projection_half_ppr, projection_ppr, dynasty_value
    """
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)
    dest = os.path.join(data_dir, "projections.csv")
    content = await file.read()
    with open(dest, "wb") as fh:
        fh.write(content)
    return {"status": "ok", "saved_path": dest}


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


@app.post("/api/darts/start")
def start_darts_game():
    with get_conn() as conn:
        try:
            return darts_game.start_game(conn)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/api/darts/{game_id}/answer")
def answer_darts_game(game_id: str, payload: dict):
    answer = str(payload.get("answer", "")).strip()
    with get_conn() as conn:
        try:
            return darts_game.submit_answer(conn, game_id, answer)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/api/darts/rooms")
def create_darts_room(request: CreateDartsRoomRequest):
    with get_conn() as conn:
        try:
            room = darts_game.create_room(conn, request.is_public)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return darts_game._room_public_state(room)


@app.get("/api/darts/rooms")
def public_darts_rooms():
    return darts_game.list_public_rooms()


@app.get("/api/darts/rooms/{code}")
def darts_room_info(code: str):
    room = darts_game.get_room(code)
    if not room:
        raise HTTPException(404, "Darts room not found")
    return darts_game._room_public_state(room)


@app.post("/api/trade/fetch-projections")
def api_fetch_projections():
    """Trigger an immediate fetch of FootballGuys CSV projections (anonymous attempt).
    Saves normalized CSV to data/projections.csv and returns status."""
    try:
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(data_dir, exist_ok=True)
        dest = os.path.join(data_dir, "projections.csv")
        result = projections_fetcher.fetch_and_save(dest_path=dest)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    if not result.get("ok"):
        raise HTTPException(500, result.get("error") or "Unknown failure")
    return result


@app.on_event("startup")
def schedule_projections_fetcher():
    # Start a background thread that attempts a fetch weekly and keeps running.
    def _bg():
        try:
            # run a fetch immediately once, then periodic
            data_dir = os.path.join(os.path.dirname(__file__), "data")
            os.makedirs(data_dir, exist_ok=True)
            dest = os.path.join(data_dir, "projections.csv")
            projections_fetcher.fetch_and_save(dest_path=dest)
        except Exception as exc:
            print(f"[startup projections] initial fetch failed: {exc}")
        # spawn the long-running periodic fetcher
        t = threading.Thread(target=projections_fetcher.periodic_fetcher, kwargs={"interval_seconds": 7*24*3600}, daemon=True)
        t.start()

    thread = threading.Thread(target=_bg, daemon=True)
    thread.start()


@app.post("/api/darts/rooms/{code}/join")
def join_darts_room(code: str, request: DartsJoinRequest):
    try:
        room = darts_game.join_room(code, request.display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return darts_game._room_public_state(room)


@app.websocket("/ws/darts/{code}")
async def darts_socket(websocket: WebSocket, code: str, display_name: str = Query(...)):
    await websocket.accept()
    try:
        room = darts_game.join_room(code, display_name, websocket)
    except ValueError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return

    async def broadcast(message_type="room_state", extra=None):
        payload = {"type": message_type, "room": darts_game._room_public_state(room)}
        if extra:
            payload.update(extra)
        for participant in room["players"].values():
            if participant["connected"] and participant.get("websocket"):
                try:
                    await participant["websocket"].send_json(payload)
                except Exception:
                    participant["connected"] = False

    await broadcast("room_state")
    try:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "start_game":
                try:
                    darts_game.start_room(room, display_name)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue
                await broadcast("game_started")
            elif message_type == "submit_answer":
                try:
                    with get_conn() as conn:
                        result = darts_game.submit_room_answer(
                            conn, room, display_name, str(message.get("answer", ""))
                        )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue
                if not result["valid"]:
                    await websocket.send_json({"type": "answer_rejected", **result})
                else:
                    await broadcast("answer_result", {"answer": result["answer"]})
                    if room["status"] == "won":
                        await broadcast("game_complete")
            elif message_type == "get_state":
                await websocket.send_json({"type": "room_state", "room": darts_game._room_public_state(room)})
            else:
                await websocket.send_json({"type": "error", "message": "Unknown darts message type"})
    except WebSocketDisconnect:
        darts_game.leave_room(room, display_name)
        await broadcast("room_state")


@app.get("/api/draft/pick/top-alternatives")
def get_top_alternatives(
    rule_id: str = Query(...),
    position: str = Query(...),
    scoring_format: str = Query("ppr"),
    limit: int = Query(5),
):
    """Return top 5 alternative picks for a given question."""
    if scoring_format not in VALID_FORMATS:
        scoring_format = "ppr"
    if limit < 1 or limit > 10:
        limit = 5

    if rule_id == "name_train":
        points_column = pick_engine.POINTS_COLUMN.get(scoring_format, "fantasy_pts_ppr")
        with get_conn() as conn, conn.cursor() as cur:
            if position == "ANY":
                cur.execute(
                    f"""
                    SELECT display_name, season, team, position, {points_column} AS fantasy_points
                    FROM trivia_player_seasons
                    ORDER BY {points_column} DESC NULLS LAST
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT display_name, season, team, position, {points_column} AS fantasy_points
                    FROM trivia_player_seasons
                    WHERE position = %s
                    ORDER BY {points_column} DESC NULLS LAST
                    LIMIT %s
                    """,
                    (position, limit),
                )
            rows = cur.fetchall()
        return [
            {
                "player": row["display_name"],
                "season": row["season"],
                "team": row["team"],
                "position": row["position"],
                "fantasy_points": row["fantasy_points"],
            }
            for row in rows
        ]

    is_dynamic = rule_id.startswith("dyn|")
    if is_dynamic:
        dynamic_parts = rule_id.split("|")
        dynamic_category = dynamic_parts[1] if len(dynamic_parts) > 1 else ""
        if dynamic_category == "collegematch":
            raise HTTPException(400, "Top alternatives unavailable for college-match questions")
        rule = None
    else:
        rule = pick_engine.get_rule(rule_id)
        if not rule:
            raise HTTPException(400, f"Unknown rule: {rule_id}")

    if rule and rule["category"] == "COLLEGE_MATCH":
        raise HTTPException(400, "Top alternatives unavailable for college questions")

    points_column = pick_engine.POINTS_COLUMN.get(scoring_format, "fantasy_pts_ppr")
    if is_dynamic:
        sql, sql_params, id_mode = dynamic_rules.build_dynamic_pool_query(rule_id, position)
    else:
        sql, sql_params, id_mode = pick_engine.build_pool_query(rule, position)
    if not sql or sql_params is None:
        raise HTTPException(400, f"Top alternatives unavailable for rule: {rule_id}")

    with get_conn() as conn, conn.cursor() as cur:
        pool_join = (
            "pool.player_id = s.player_id AND pool.season = s.season"
            if id_mode == "season"
            else "pool.player_id = s.player_id"
        )
        full_sql = f"""
            SELECT p.display_name, s.season, s.team,
                   s.{points_column} AS fantasy_points
            FROM trivia_player_seasons s
            JOIN players p ON p.player_id = s.player_id
            JOIN ({sql}) pool ON {pool_join}
            ORDER BY s.{points_column} DESC NULLS LAST
            LIMIT %s
        """
        params = list(sql_params) + [limit]
        cur.execute(full_sql, params)
        rows = cur.fetchall()

    return [
        {
            "player": row["display_name"],
            "season": row["season"],
            "team": row["team"],
            "fantasy_points": row["fantasy_points"],
        }
        for row in rows
    ]


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
