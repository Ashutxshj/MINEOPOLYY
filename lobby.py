from __future__ import annotations

import random
import string
from typing import Any

from redis_client import (
    LOBBY_TTL_SECONDS,
    apply_lobby_ttl,
    dumps_json,
    get_redis,
    lobby_key,
    lobby_players_key,
    lobby_spectators_key,
    read_hash,
    read_players,
    user_key,
    write_players,
)

PLAYER_COLORS = [
    "#2563eb",
    "#ef4444",
    "#22c55e",
    "#facc15",
]

MAP_SIZES = {
    "small": {"cols": 20, "rows": 15, "mines": 15, "duration": 300},
    "medium": {"cols": 40, "rows": 30, "mines": 60, "duration": 600},
    "large": {"cols": 60, "rows": 45, "mines": 135, "duration": 900},
}


def sanitize_name(name: str | None) -> str:
    clean = (name or "Anonymous").strip()
    if not clean:
        clean = "Anonymous"
    return clean[:20]


def _pick_color(players: list[dict[str, Any]]) -> str:
    used = {str(player.get("color")) for player in players}
    available = [color for color in PLAYER_COLORS if color not in used]
    if available:
        return available[0]
    return PLAYER_COLORS[len(players) % len(PLAYER_COLORS)]


async def _generate_lobby_code() -> str:
    client = get_redis()
    charset = string.ascii_uppercase + string.digits
    for _ in range(256):
        code = "".join(random.choices(charset, k=4))
        if not await client.exists(lobby_key(code)):
            return code
    raise RuntimeError("Unable to allocate lobby code")


async def lobby_exists(code: str) -> bool:
    client = get_redis()
    return bool(await client.exists(lobby_key(code.upper())))


async def get_lobby_data(code: str) -> dict[str, str]:
    return await read_hash(lobby_key(code.upper()))


async def get_lobby_status(code: str) -> str | None:
    data = await get_lobby_data(code)
    status = data.get("status")
    return status if status else None


async def get_lobby_map_size(code: str) -> str | None:
    data = await get_lobby_data(code)
    map_size = data.get("map_size")
    return map_size if map_size else None


async def create_lobby(user_id: str, name: str, map_size: str) -> dict[str, Any]:
    client = get_redis()
    map_choice = map_size if map_size in MAP_SIZES else "medium"
    code = await _generate_lobby_code()

    player = {
        "user_id": user_id,
        "name": sanitize_name(name),
        "color": _pick_color([]),
        "ready": False,
        "online": True,
    }

    pipe = client.pipeline(transaction=True)
    pipe.hset(
        lobby_key(code),
        mapping={
            "host_id": user_id,
            "map_size": map_choice,
            "status": "waiting",
        },
    )
    pipe.delete(lobby_players_key(code))
    pipe.rpush(lobby_players_key(code), dumps_json(player))
    pipe.delete(lobby_spectators_key(code))
    pipe.hset(user_key(code, user_id), mapping={"name": player["name"], "color": player["color"]})

    pipe.expire(lobby_key(code), LOBBY_TTL_SECONDS)
    pipe.expire(lobby_players_key(code), LOBBY_TTL_SECONDS)
    pipe.expire(lobby_spectators_key(code), LOBBY_TTL_SECONDS)
    pipe.expire(user_key(code, user_id), LOBBY_TTL_SECONDS)
    await pipe.execute()

    return {"code": code, "map_size": map_choice, "player": player}


async def _upsert_player(code: str, player: dict[str, Any]) -> list[dict[str, Any]]:
    players = await read_players(code)
    for idx, existing in enumerate(players):
        if str(existing.get("user_id")) == str(player.get("user_id")):
            players[idx] = player
            await write_players(code, players)
            return players

    players.append(player)
    await write_players(code, players)
    return players


async def join_lobby(code: str, user_id: str, name: str) -> dict[str, Any]:
    client = get_redis()
    code = code.upper()

    if not await client.exists(lobby_key(code)):
        return {"ok": False, "error": "lobby not found"}

    status = (await read_hash(lobby_key(code))).get("status", "waiting")
    players = await read_players(code)

    if status != "waiting" or len(players) >= 4:
        return {
            "ok": True,
            "route": "spectator",
            "reason": "game in progress, joining as spectator",
        }

    existing = next((player for player in players if str(player.get("user_id")) == user_id), None)
    if existing:
        existing["online"] = True
        await _upsert_player(code, existing)
        await apply_lobby_ttl(code, LOBBY_TTL_SECONDS)
        return {"ok": True, "route": "player", "player": existing}

    color = _pick_color(players)
    player = {
        "user_id": user_id,
        "name": sanitize_name(name),
        "color": color,
        "ready": False,
        "online": True,
    }
    players = await _upsert_player(code, player)

    pipe = client.pipeline(transaction=False)
    pipe.hset(user_key(code, user_id), mapping={"name": player["name"], "color": player["color"]})
    pipe.expire(user_key(code, user_id), LOBBY_TTL_SECONDS)
    await pipe.execute()

    await apply_lobby_ttl(code, LOBBY_TTL_SECONDS)
    return {"ok": True, "route": "player", "player": player, "players": players}


async def join_spectate(code: str, user_id: str) -> dict[str, Any]:
    client = get_redis()
    code = code.upper()

    if not await client.exists(lobby_key(code)):
        return {"ok": False, "error": "lobby not found"}

    pipe = client.pipeline(transaction=False)
    pipe.sadd(lobby_spectators_key(code), user_id)
    pipe.expire(lobby_spectators_key(code), LOBBY_TTL_SECONDS)
    await pipe.execute()

    await apply_lobby_ttl(code, LOBBY_TTL_SECONDS)
    lobby_data = await read_hash(lobby_key(code))
    return {
        "ok": True,
        "status": lobby_data.get("status", "waiting"),
        "map_size": lobby_data.get("map_size", "medium"),
        "spectator_count": int(await client.scard(lobby_spectators_key(code))),
    }


async def get_lobby_snapshot(code: str) -> dict[str, Any]:
    client = get_redis()
    code = code.upper()
    players = await read_players(code)
    all_ready = len(players) == 4 and all(bool(player.get("ready")) for player in players)
    spectator_count = int(await client.scard(lobby_spectators_key(code)))
    return {
        "players": players,
        "spectator_count": spectator_count,
        "all_ready": all_ready,
    }


async def toggle_ready(code: str, user_id: str) -> dict[str, Any]:
    code = code.upper()
    players = await read_players(code)

    touched = False
    for player in players:
        if str(player.get("user_id")) == user_id:
            player["ready"] = not bool(player.get("ready"))
            touched = True
            break

    if not touched:
        return {"ok": False, "error": "player not found"}

    await write_players(code, players)
    snapshot = await get_lobby_snapshot(code)
    await apply_lobby_ttl(code, LOBBY_TTL_SECONDS)
    return {"ok": True, **snapshot}


async def set_lobby_status(code: str, status: str) -> None:
    client = get_redis()
    await client.hset(lobby_key(code.upper()), "status", status)


async def set_host(code: str, host_id: str) -> None:
    client = get_redis()
    await client.hset(lobby_key(code.upper()), "host_id", host_id)


async def mark_player_online(code: str, user_id: str, online: bool) -> dict[str, Any] | None:
    code = code.upper()
    players = await read_players(code)
    updated: dict[str, Any] | None = None

    for player in players:
        if str(player.get("user_id")) == user_id:
            player["online"] = online
            updated = player
            break

    if updated is None:
        return None

    await write_players(code, players)
    return updated


async def remove_player_waiting(code: str, user_id: str) -> dict[str, Any]:
    client = get_redis()
    code = code.upper()

    lobby_data = await read_hash(lobby_key(code))
    host_id = lobby_data.get("host_id")

    players = await read_players(code)
    remaining = [player for player in players if str(player.get("user_id")) != user_id]

    pipe = client.pipeline(transaction=False)
    pipe.delete(user_key(code, user_id))

    if not remaining:
        pipe.delete(lobby_key(code))
        pipe.delete(lobby_players_key(code))
        pipe.delete(lobby_spectators_key(code))
        await pipe.execute()
        return {"deleted": True}

    await write_players(code, remaining)
    if host_id == user_id:
        await set_host(code, str(remaining[0].get("user_id")))
    await pipe.execute()

    snapshot = await get_lobby_snapshot(code)
    return {"deleted": False, **snapshot}


async def remove_spectator(code: str, user_id: str) -> int:
    client = get_redis()
    code = code.upper()
    await client.srem(lobby_spectators_key(code), user_id)
    return int(await client.scard(lobby_spectators_key(code)))
