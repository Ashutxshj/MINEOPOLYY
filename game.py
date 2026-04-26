from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import leaderboard
from lobby import MAP_SIZES
from redis_client import (
    LOBBY_TTL_SECONDS,
    apply_lobby_ttl,
    cooldown_key,
    dumps_json,
    expire_game_keys,
    game_stats_key,
    get_redis,
    grid_key,
    leaderboard_key,
    loads_json,
    lobby_key,
    mines_key,
    publish_event,
    read_hash,
    read_players,
    spectator_count,
    user_key,
)


def _parse_cell_id(cell_id: str) -> tuple[int, int] | None:
    try:
        x_str, y_str = cell_id.split(",")
        return int(x_str), int(y_str)
    except (ValueError, AttributeError):
        return None


def _cell_id(x: int, y: int) -> str:
    return f"{x},{y}"


async def get_grid_state(code: str) -> dict[str, dict[str, Any]]:
    client = get_redis()
    raw = await client.hgetall(grid_key(code))
    parsed: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in raw.items():
        key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
        cell_data = loads_json(raw_value, {})
        if isinstance(cell_data, dict) and cell_data.get("owner"):
            parsed[key] = cell_data
    return parsed


async def init_grid(code: str, map_size: str) -> dict[str, int]:
    client = get_redis()
    config = MAP_SIZES.get(map_size, MAP_SIZES["medium"])
    cols, rows, mine_count, duration = (
        int(config["cols"]),
        int(config["rows"]),
        int(config["mines"]),
        int(config["duration"]),
    )

    all_cells = [_cell_id(x, y) for x in range(cols) for y in range(rows)]
    selected_mines = random.sample(all_cells, mine_count)

    now = time.time()
    pipe = client.pipeline(transaction=True)
    pipe.delete(grid_key(code))
    pipe.delete(mines_key(code))
    pipe.delete(leaderboard_key(code))
    if selected_mines:
        pipe.sadd(mines_key(code), *selected_mines)
    pipe.hset(
        lobby_key(code),
        mapping={
            "status": "playing",
            "started_at": str(now),
            "ends_at": str(now + duration),
        },
    )
    pipe.expire(grid_key(code), LOBBY_TTL_SECONDS)
    pipe.expire(mines_key(code), LOBBY_TTL_SECONDS)
    await pipe.execute()

    await apply_lobby_ttl(code, LOBBY_TTL_SECONDS)
    await leaderboard.initialize_in_game_leaderboard(code)
    await recalculate_scores(code)
    return config


def _cluster_bonus(cells: set[str]) -> int:
    if not cells:
        return 0

    visited: set[str] = set()
    bonus = 0

    for start in list(cells):
        if start in visited:
            continue

        queue = [start]
        visited.add(start)
        region_size = 0

        while queue:
            current = queue.pop()
            region_size += 1
            parsed = _parse_cell_id(current)
            if parsed is None:
                continue
            x, y = parsed
            neighbors = [
                _cell_id(x + 1, y),
                _cell_id(x - 1, y),
                _cell_id(x, y + 1),
                _cell_id(x, y - 1),
            ]
            for neighbor in neighbors:
                if neighbor in cells and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)

        if region_size >= 5:
            bonus += 1

    return bonus


async def recalculate_scores(code: str) -> dict[str, int]:
    players = await read_players(code)
    grid = await get_grid_state(code)

    owned_cells: dict[str, set[str]] = {}
    for cid, cell in grid.items():
        owner_id = str(cell.get("owner", ""))
        if not owner_id:
            continue
        owned_cells.setdefault(owner_id, set()).add(cid)

    scores: dict[str, int] = {}
    for player in players:
        user_id = str(player.get("user_id"))
        cells = owned_cells.get(user_id, set())
        score = len(cells) + _cluster_bonus(cells)
        scores[user_id] = max(0, int(score))

    await leaderboard.set_in_game_scores(code, scores)
    return scores


async def _capture_with_watch(
    code: str,
    user_id: str,
    username: str,
    color: str,
    cell_id: str,
    capture_ts: float,
) -> dict[str, Any]:
    client = get_redis()
    grid_k = grid_key(code)
    cooldown_k = cooldown_key(code, user_id)
    stats_k = game_stats_key(code, user_id)

    async with client.lock:
        grid_entry = client._ensure_entry(grid_k, "hash", {})
        raw_existing = grid_entry.value.get(cell_id)
        existing = loads_json(raw_existing, {})

        if isinstance(existing, dict):
            owner_id = str(existing.get("owner", ""))
            if owner_id == user_id:
                return {"type": "noop"}
            if owner_id:
                return {"type": "occupied"}
            existing_ts = float(existing.get("ts", 0) or 0)
            if existing_ts > capture_ts:
                return {"type": "stale"}

        payload = {
            "owner": user_id,
            "color": color,
            "name": username,
            "ts": capture_ts,
        }

        grid_entry.value[cell_id] = dumps_json(payload)
        client._locked_set(cooldown_k, "1", px=1500)
        client._locked_hincrby(stats_k, "captures", 1)
        client._locked_expire(stats_k, LOBBY_TTL_SECONDS)

    await recalculate_scores(code)
    lb = await leaderboard.get_in_game_leaderboard(code)
    watching = await spectator_count(code)

    return {
        "type": "capture",
        "cell_id": cell_id,
        "owner_id": user_id,
        "color": color,
        "name": username,
        "ts": capture_ts,
        "leaderboard": lb,
        "spectator_count": watching,
    }


async def _trigger_mine(code: str, user_id: str, cell_id: str) -> dict[str, Any]:
    client = get_redis()

    lobby_data = await read_hash(lobby_key(code))
    map_size = lobby_data.get("map_size", "medium")
    config = MAP_SIZES.get(map_size, MAP_SIZES["medium"])
    cols = int(config["cols"])
    rows = int(config["rows"])

    parsed = _parse_cell_id(cell_id)
    if parsed is None:
        return {"type": "error", "reason": "invalid cell"}
    x, y = parsed

    radius_cells: list[str] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            nx = x + dx
            ny = y + dy
            if 0 <= nx < cols and 0 <= ny < rows:
                radius_cells.append(_cell_id(nx, ny))

    raw_cells = await client.hmget(grid_key(code), radius_cells)
    affected_cells: list[str] = []
    impacted_users: set[str] = set()

    for cid, raw in zip(radius_cells, raw_cells):
        cell = loads_json(raw, {})
        owner = str(cell.get("owner", "")) if isinstance(cell, dict) else ""
        if owner:
            affected_cells.append(cid)
            impacted_users.add(owner)

    pipe = client.pipeline(transaction=False)
    if affected_cells:
        pipe.hdel(grid_key(code), *affected_cells)

    pipe.srem(mines_key(code), cell_id)
    trigger_stats = game_stats_key(code, user_id)
    pipe.hincrby(trigger_stats, "wipes_caused", 1)
    pipe.expire(trigger_stats, LOBBY_TTL_SECONDS)

    for impacted_user in impacted_users:
        stats_k = game_stats_key(code, impacted_user)
        pipe.hincrby(stats_k, "times_wiped", 1)
        pipe.expire(stats_k, LOBBY_TTL_SECONDS)

    pipe.set(cooldown_key(code, user_id), "1", px=1500)
    await pipe.execute()

    await recalculate_scores(code)
    lb = await leaderboard.get_in_game_leaderboard(code)
    watching = await spectator_count(code)

    return {
        "type": "mine_triggered",
        "cell_id": cell_id,
        "owner_id": user_id,
        "affected_cells": affected_cells,
        "leaderboard": lb,
        "spectator_count": watching,
    }


async def capture(code: str, user_id: str, cell_id: str) -> dict[str, Any]:
    client = get_redis()
    code = code.upper()

    lobby_data = await read_hash(lobby_key(code))
    if lobby_data.get("status") != "playing":
        return {"type": "error", "reason": "game not active"}

    map_size = lobby_data.get("map_size", "medium")
    config = MAP_SIZES.get(map_size, MAP_SIZES["medium"])
    parsed = _parse_cell_id(cell_id)
    if parsed is None:
        return {"type": "error", "reason": "invalid cell"}

    x, y = parsed
    if not (0 <= x < int(config["cols"]) and 0 <= y < int(config["rows"])):
        return {"type": "error", "reason": "cell out of bounds"}

    ttl_ms = int(await client.pttl(cooldown_key(code, user_id)))
    if ttl_ms > 0:
        return {"type": "cooldown", "remaining_ms": ttl_ms}

    player_info = await read_hash(user_key(code, user_id))
    username = player_info.get("name", "Anonymous")
    color = player_info.get("color", "#888888")

    if await client.sismember(mines_key(code), cell_id):
        return await _trigger_mine(code, user_id, cell_id)

    capture_ts = time.time()
    return await _capture_with_watch(code, user_id, username, color, cell_id, capture_ts)


async def respawn_mine_after_delay(code: str, delay_seconds: int = 10) -> bool:
    await asyncio.sleep(delay_seconds)

    client = get_redis()
    lobby_data = await read_hash(lobby_key(code))
    if not lobby_data or lobby_data.get("status") != "playing":
        return False

    map_size = lobby_data.get("map_size", "medium")
    config = MAP_SIZES.get(map_size, MAP_SIZES["medium"])
    cols = int(config["cols"])
    rows = int(config["rows"])

    existing_mines = {
        mine.decode("utf-8") if isinstance(mine, bytes) else str(mine)
        for mine in await client.smembers(mines_key(code))
    }
    claimed_cells = {
        cell.decode("utf-8") if isinstance(cell, bytes) else str(cell)
        for cell in await client.hkeys(grid_key(code))
    }

    available: list[str] = []
    for x in range(cols):
        for y in range(rows):
            cid = _cell_id(x, y)
            if cid in existing_mines or cid in claimed_cells:
                continue
            available.append(cid)

    if not available:
        return False

    new_cell = random.choice(available)
    await client.sadd(mines_key(code), new_cell)
    await client.expire(mines_key(code), LOBBY_TTL_SECONDS)
    return True


async def get_remaining_seconds(code: str) -> int:
    lobby_data = await read_hash(lobby_key(code))
    ends_at = float(lobby_data.get("ends_at", "0") or 0)
    if ends_at <= 0:
        return 0
    return max(0, int(ends_at - time.time()))


async def run_game_timer(code: str, duration: int) -> None:
    remaining = int(duration)

    while remaining > 0:
        await asyncio.sleep(1)
        remaining -= 1

        status = await read_hash(lobby_key(code))
        if status.get("status") != "playing":
            return

        await publish_event(code, {"type": "timer_tick", "remaining_seconds": remaining})

    await end_game(code)


async def end_game(code: str) -> None:
    client = get_redis()
    current = await read_hash(lobby_key(code))
    if not current:
        return

    if current.get("status") == "ended":
        return

    await client.hset(lobby_key(code), "status", "ended")
    final_lb = await leaderboard.get_in_game_leaderboard(code)
    watching = await spectator_count(code)

    await leaderboard.write_global_stats_on_game_end(code)

    await publish_event(
        code,
        {
            "type": "game_over",
            "leaderboard": final_lb,
            "spectator_count": watching,
        },
    )

    await expire_game_keys(code)
