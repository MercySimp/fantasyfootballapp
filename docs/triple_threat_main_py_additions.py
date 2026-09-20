# --- Additions for server/main.py ---------------------------------------
# 1) Add near the other game imports at the top:
#
#     import triple_threat_game
#
# 2) Add near CreateDartsRoomRequest / DartsJoinRequest:

class CreateTripleThreatRoomRequest(BaseModel):
    is_public: bool = False

class TripleThreatJoinRequest(BaseModel):
    display_name: str

# 3) REST endpoints -- place alongside the /api/darts/* routes:

@app.post("/api/triple-threat/rooms")
def create_triple_threat_room(request: CreateTripleThreatRoomRequest):
    room = triple_threat_game.create_room(request.is_public)
    return triple_threat_game._room_public_state(room)

@app.get("/api/triple-threat/rooms")
def public_triple_threat_rooms():
    return triple_threat_game.list_public_rooms()

@app.get("/api/triple-threat/rooms/{code}")
def triple_threat_room_info(code: str):
    room = triple_threat_game.get_room(code)
    if not room:
        raise HTTPException(404, "Triple Threat room not found")
    return triple_threat_game._room_public_state(room)

@app.post("/api/triple-threat/rooms/{code}/join")
def join_triple_threat_room(code: str, request: TripleThreatJoinRequest):
    try:
        room = triple_threat_game.join_room(code, request.display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return triple_threat_game._room_public_state(room)

# 4) Websocket handler -- place alongside /ws/darts/{code}:

@app.websocket("/ws/triple-threat/{code}")
async def triple_threat_socket(websocket: WebSocket, code: str, display_name: str = Query(...)):
    await websocket.accept()
    try:
        room = triple_threat_game.join_room(code, display_name, websocket)
    except ValueError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return

    async def broadcast(message_type="room_state", extra=None):
        payload = {"type": message_type, "room": triple_threat_game._room_public_state(room)}
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
                    with get_conn() as conn:
                        triple_threat_game.start_room(conn, room, display_name)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue
                await broadcast("round_start")

            elif message_type == "submit_guess":
                try:
                    revealed = triple_threat_game.submit_room_guess(
                        room, display_name, str(message.get("guess", ""))
                    )
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue
                if revealed:
                    await broadcast("round_revealed")
                    if room["status"] == "complete":
                        await broadcast("game_complete")
                else:
                    # Acknowledge privately -- don't leak who's answered what.
                    await websocket.send_json({"type": "guess_received"})
                    await broadcast("room_state")

            elif message_type == "advance_round":
                try:
                    with get_conn() as conn:
                        triple_threat_game.advance_round(conn, room, display_name)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue
                await broadcast("round_start")

            elif message_type == "get_state":
                await websocket.send_json({"type": "room_state", "room": triple_threat_game._room_public_state(room)})

            else:
                await websocket.send_json({"type": "error", "message": "Unknown Triple Threat message type"})
    except WebSocketDisconnect:
        triple_threat_game.leave_room(room, display_name)
        await broadcast("room_state")
