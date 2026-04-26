from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import game
import leaderboard
import lobby
from redis_client import (
    PubSub,
    close_redis,
    close_subscription,
    create_subscription,
    init_redis,
    publish_event,
    spectator_count,
)

app = FastAPI(title="GridWar")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_INDEX = os.path.join(BASE_DIR, "static", "index.html")

countdown_tasks: dict[str, asyncio.Task[Any]] = {}
timer_tasks: dict[str, asyncio.Task[Any]] = {}


@dataclass
class Session:
    websocket: WebSocket
    user_id: str
    name: str = "Anonymous"
    role: str = "none"  # player | spectator | none
    state: str = "pre_lobby"  # pre_lobby | lobby | game | spectate | post_game
    code: str | None = None
    color: str = "#888888"
    pubsub: PubSub | None = None
    relay_task: asyncio.Task[Any] | None = None


@app.on_event("startup")
async def on_startup() -> None:
    await init_redis()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task in list(countdown_tasks.values()):
        task.cancel()
    for task in list(timer_tasks.values()):
        task.cancel()
    await close_redis()


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_INDEX)


@app.get("/leaderboard")
async def global_leaderboard() -> list[dict[str, Any]]:
    return await leaderboard.get_global_leaderboard()


async def attach_subscription(session: Session, code: str) -> None:
    if session.pubsub is not None:
        await detach_subscription(session)

    session.code = code.upper()
    session.pubsub = await create_subscription(session.code)
    session.relay_task = asyncio.create_task(relay_pubsub_events(session))


async def detach_subscription(session: Session) -> None:
    if session.relay_task is not None:
        session.relay_task.cancel()
        try:
            await session.relay_task
        except asyncio.CancelledError:
            pass
        session.relay_task = None

    if session.pubsub is not None:
        await close_subscription(session.pubsub)
        session.pubsub = None


async def relay_pubsub_events(session: Session) -> None:
    assert session.pubsub is not None

    try:
        while True:
            message = await session.pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not message:
                await asyncio.sleep(0.01)
                continue

            raw_data = message.get("data")
            if isinstance(raw_data, bytes):
                payload = json.loads(raw_data.decode("utf-8"))
            elif isinstance(raw_data, str):
                payload = json.loads(raw_data)
            else:
                continue

            msg_type = payload.get("type")
            if msg_type == "game_start":
                session.state = "spectate" if session.role == "spectator" else "game"
            elif msg_type == "game_over":
                session.state = "post_game"
            elif msg_type == "lobby_update" and session.role == "player":
                session.state = "lobby"

            await session.websocket.send_json(payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _register_timer_task(code: str, duration: int) -> None:
    existing = timer_tasks.get(code)
    if existing and not existing.done():
        existing.cancel()

    task = asyncio.create_task(game.run_game_timer(code, duration))
    timer_tasks[code] = task

    def _cleanup(_: asyncio.Task[Any]) -> None:
        timer_tasks.pop(code, None)

    task.add_done_callback(_cleanup)


async def _countdown_then_start(code: str) -> None:
    try:
        code = code.upper()

        for seconds in (3, 2, 1, 0):
            status = await lobby.get_lobby_status(code)
            snapshot = await lobby.get_lobby_snapshot(code)
            if status != "waiting" or len(snapshot["players"]) != 4 or not snapshot["all_ready"]:
                return

            await publish_event(code, {"type": "countdown", "seconds": seconds})
            await asyncio.sleep(1)

        status = await lobby.get_lobby_status(code)
        snapshot = await lobby.get_lobby_snapshot(code)
        if status != "waiting" or len(snapshot["players"]) != 4 or not snapshot["all_ready"]:
            return

        map_size = await lobby.get_lobby_map_size(code) or "medium"
        config = await game.init_grid(code, map_size)

        lb = await leaderboard.get_in_game_leaderboard(code)
        grid = await game.get_grid_state(code)
        watching = await spectator_count(code)

        await publish_event(
            code,
            {
                "type": "game_start",
                "grid": grid,
                "mines_count": int(config["mines"]),
                "cols": int(config["cols"]),
                "rows": int(config["rows"]),
                "duration": int(config["duration"]),
                "leaderboard": lb,
                "spectator_count": watching,
            },
        )

        await _register_timer_task(code, int(config["duration"]))
    finally:
        countdown_tasks.pop(code, None)


async def maybe_start_countdown(code: str) -> None:
    code = code.upper()
    if code in countdown_tasks and not countdown_tasks[code].done():
        return

    status = await lobby.get_lobby_status(code)
    snapshot = await lobby.get_lobby_snapshot(code)
    if status != "waiting" or len(snapshot["players"]) != 4 or not snapshot["all_ready"]:
        return

    task = asyncio.create_task(_countdown_then_start(code))
    countdown_tasks[code] = task


async def _send_playing_state(websocket: WebSocket, code: str, force_spectator: bool) -> None:
    map_size = await lobby.get_lobby_map_size(code) or "medium"
    config = lobby.MAP_SIZES.get(map_size, lobby.MAP_SIZES["medium"])
    lb = await leaderboard.get_in_game_leaderboard(code)
    grid = await game.get_grid_state(code)
    remaining = await game.get_remaining_seconds(code)
    watching = await spectator_count(code)

    await websocket.send_json(
        {
            "type": "game_start",
            "grid": grid,
            "mines_count": int(config["mines"]),
            "cols": int(config["cols"]),
            "rows": int(config["rows"]),
            "duration": remaining if remaining > 0 else int(config["duration"]),
            "leaderboard": lb,
            "spectator": bool(force_spectator),
            "spectator_count": watching,
        }
    )


async def _handle_capture_message(session: Session, cell_id: str) -> None:
    if not session.code:
        return

    result = await game.capture(session.code, session.user_id, cell_id)
    result_type = result.get("type")

    if result_type == "cooldown":
        await session.websocket.send_json(
            {
                "type": "cooldown",
                "remaining_ms": int(result.get("remaining_ms", 0)),
            }
        )
        return

    if result_type == "capture":
        await publish_event(session.code, result)
        return

    if result_type == "mine_triggered":
        await publish_event(session.code, result)

        async def _respawn() -> None:
            if await game.respawn_mine_after_delay(session.code):
                await publish_event(session.code, {"type": "mine_respawned"})

        asyncio.create_task(_respawn())
        return


async def _handle_disconnect(session: Session) -> None:
    if not session.code:
        return

    code = session.code.upper()
    if session.role == "spectator":
        watching = await lobby.remove_spectator(code, session.user_id)
        await publish_event(code, {"type": "spectator_update", "spectator_count": watching})
        return

    if session.role != "player":
        return

    status = await lobby.get_lobby_status(code)
    if status == "waiting":
        countdown_task = countdown_tasks.get(code)
        if countdown_task and not countdown_task.done():
            countdown_task.cancel()
            countdown_tasks.pop(code, None)

        snapshot = await lobby.remove_player_waiting(code, session.user_id)
        if not snapshot.get("deleted"):
            await publish_event(
                code,
                {
                    "type": "lobby_update",
                    "players": snapshot.get("players", []),
                    "spectator_count": int(snapshot.get("spectator_count", 0)),
                    "all_ready": bool(snapshot.get("all_ready", False)),
                },
            )
            await publish_event(code, {"type": "user_left", "user_id": session.user_id, "online": False})
        return

    if status in {"playing", "ended"}:
        updated = await lobby.mark_player_online(code, session.user_id, False)
        if updated:
            await publish_event(code, {"type": "user_left", "user_id": session.user_id, "online": False})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session = Session(websocket=websocket, user_id=str(uuid.uuid4()))

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = str(data.get("type", ""))

            if "name" in data:
                session.name = lobby.sanitize_name(data.get("name"))

            if msg_type == "create_lobby":
                map_size = str(data.get("map_size", "medium"))
                created = await lobby.create_lobby(session.user_id, session.name, map_size)

                session.role = "player"
                session.state = "lobby"
                session.code = str(created["code"]).upper()
                session.color = str(created["player"]["color"])

                await attach_subscription(session, session.code)

                await websocket.send_json(
                    {
                        "type": "lobby_created",
                        "code": session.code,
                        "map_size": str(created["map_size"]),
                        "user_id": session.user_id,
                        "color": session.color,
                        "role": session.role,
                    }
                )

                snapshot = await lobby.get_lobby_snapshot(session.code)
                await publish_event(
                    session.code,
                    {
                        "type": "lobby_update",
                        "players": snapshot["players"],
                        "spectator_count": snapshot["spectator_count"],
                        "all_ready": snapshot["all_ready"],
                    },
                )
                await publish_event(
                    session.code,
                    {
                        "type": "user_joined",
                        "user": created["player"],
                        "online": True,
                    },
                )
                continue

            if msg_type == "join_lobby":
                code = str(data.get("code", "")).strip().upper()
                if not code:
                    await websocket.send_json({"type": "lobby_error", "reason": "missing lobby code"})
                    continue

                joined = await lobby.join_lobby(code, session.user_id, session.name)
                if not joined.get("ok"):
                    await websocket.send_json({"type": "lobby_error", "reason": joined.get("error", "join failed")})
                    continue

                if joined.get("route") == "spectator":
                    await websocket.send_json({"type": "lobby_error", "reason": joined.get("reason", "joining as spectator")})
                    data = {"type": "join_spectate", "code": code}
                    msg_type = "join_spectate"
                else:
                    session.role = "player"
                    session.state = "lobby"
                    session.code = code
                    session.color = str(joined["player"]["color"])

                    await attach_subscription(session, code)
                    map_size = await lobby.get_lobby_map_size(code) or "medium"
                    await websocket.send_json(
                        {
                            "type": "lobby_created",
                            "code": code,
                            "map_size": map_size,
                            "user_id": session.user_id,
                            "color": session.color,
                            "role": session.role,
                        }
                    )

                    snapshot = await lobby.get_lobby_snapshot(code)
                    await publish_event(
                        code,
                        {
                            "type": "lobby_update",
                            "players": snapshot["players"],
                            "spectator_count": snapshot["spectator_count"],
                            "all_ready": snapshot["all_ready"],
                        },
                    )
                    await publish_event(
                        code,
                        {
                            "type": "user_joined",
                            "user": joined["player"],
                            "online": True,
                        },
                    )
                    continue

            if msg_type == "join_spectate":
                code = str(data.get("code", "")).strip().upper()
                if not code:
                    await websocket.send_json({"type": "lobby_error", "reason": "missing lobby code"})
                    continue

                spectate = await lobby.join_spectate(code, session.user_id)
                if not spectate.get("ok"):
                    await websocket.send_json({"type": "lobby_error", "reason": spectate.get("error", "spectate failed")})
                    continue

                session.role = "spectator"
                session.state = "spectate"
                session.code = code

                await attach_subscription(session, code)
                await websocket.send_json(
                    {
                        "type": "lobby_created",
                        "code": code,
                        "map_size": str(spectate.get("map_size", "medium")),
                        "user_id": session.user_id,
                        "role": session.role,
                    }
                )
                await publish_event(
                    code,
                    {
                        "type": "spectator_update",
                        "spectator_count": int(spectate.get("spectator_count", 0)),
                    },
                )

                status = str(spectate.get("status", "waiting"))
                if status == "playing":
                    await _send_playing_state(websocket, code, True)
                elif status == "waiting":
                    snapshot = await lobby.get_lobby_snapshot(code)
                    await websocket.send_json(
                        {
                            "type": "lobby_update",
                            "players": snapshot["players"],
                            "spectator_count": snapshot["spectator_count"],
                            "all_ready": snapshot["all_ready"],
                        }
                    )
                else:
                    lb = await leaderboard.get_in_game_leaderboard(code)
                    watching = await spectator_count(code)
                    await websocket.send_json({"type": "game_over", "leaderboard": lb, "spectator_count": watching})
                continue

            if msg_type == "ready_up":
                if session.role != "player" or session.state != "lobby" or not session.code:
                    await websocket.send_json({"type": "lobby_error", "reason": "not in lobby"})
                    continue

                toggled = await lobby.toggle_ready(session.code, session.user_id)
                if not toggled.get("ok"):
                    await websocket.send_json({"type": "lobby_error", "reason": toggled.get("error", "ready failed")})
                    continue

                await publish_event(
                    session.code,
                    {
                        "type": "lobby_update",
                        "players": toggled["players"],
                        "spectator_count": toggled["spectator_count"],
                        "all_ready": toggled["all_ready"],
                    },
                )
                await maybe_start_countdown(session.code)
                continue

            if msg_type == "capture":
                if session.role == "spectator":
                    continue
                if session.role != "player" or session.state not in {"game", "lobby"} or not session.code:
                    continue

                cell_id = str(data.get("cell_id", ""))
                await _handle_capture_message(session, cell_id)
                continue

            await websocket.send_json({"type": "lobby_error", "reason": "unknown message type"})

    except WebSocketDisconnect:
        pass
    finally:
        await detach_subscription(session)
        await _handle_disconnect(session)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
