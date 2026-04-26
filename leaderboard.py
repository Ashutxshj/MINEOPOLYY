from __future__ import annotations

from typing import Any

from redis_client import (
    GLOBAL_LB_KEY,
    LOBBY_TTL_SECONDS,
    game_stats_key,
    get_redis,
    global_stats_key,
    leaderboard_key,
    read_hash,
    read_players,
    user_key,
)


async def initialize_in_game_leaderboard(code: str) -> None:
    client = get_redis()
    players = await read_players(code)
    key = leaderboard_key(code)
    pipe = client.pipeline(transaction=True)
    pipe.delete(key)
    if players:
        mapping = {str(player["user_id"]): 0 for player in players}
        pipe.zadd(key, mapping)
    pipe.expire(key, LOBBY_TTL_SECONDS)
    await pipe.execute()


async def set_in_game_scores(code: str, scores: dict[str, int]) -> None:
    client = get_redis()
    key = leaderboard_key(code)
    pipe = client.pipeline(transaction=True)
    pipe.delete(key)
    if scores:
        pipe.zadd(key, {user_id: max(0, int(score)) for user_id, score in scores.items()})
    pipe.expire(key, LOBBY_TTL_SECONDS)
    await pipe.execute()


async def _player_lookup(code: str) -> dict[str, dict[str, Any]]:
    players = await read_players(code)
    lookup: dict[str, dict[str, Any]] = {}
    for player in players:
        lookup[str(player.get("user_id"))] = player
    return lookup


async def get_in_game_leaderboard(code: str, limit: int = 10) -> list[dict[str, Any]]:
    client = get_redis()
    results = await client.zrevrange(leaderboard_key(code), 0, max(0, limit - 1), withscores=True)
    players = await _player_lookup(code)

    leaderboard: list[dict[str, Any]] = []
    for raw_user_id, raw_score in results:
        user_id = raw_user_id.decode("utf-8") if isinstance(raw_user_id, bytes) else str(raw_user_id)
        player = players.get(user_id)

        if not player:
            user_hash = await read_hash(user_key(code, user_id))
            player = {
                "user_id": user_id,
                "name": user_hash.get("name", "Unknown"),
                "color": user_hash.get("color", "#888888"),
                "online": False,
            }

        leaderboard.append(
            {
                "user_id": user_id,
                "name": str(player.get("name", "Unknown")),
                "color": str(player.get("color", "#888888")),
                "score": int(raw_score),
                "online": bool(player.get("online", False)),
            }
        )
    return leaderboard


async def incr_game_stat(code: str, user_id: str, field: str, amount: int = 1) -> None:
    client = get_redis()
    key = game_stats_key(code, user_id)
    pipe = client.pipeline(transaction=False)
    pipe.hincrby(key, field, int(amount))
    pipe.expire(key, LOBBY_TTL_SECONDS)
    await pipe.execute()


async def write_global_stats_on_game_end(code: str) -> None:
    client = get_redis()
    players = await read_players(code)
    if not players:
        return

    player_scores = {
        str(player["user_id"]): int(
            await client.zscore(leaderboard_key(code), str(player["user_id"])) or 0
        )
        for player in players
    }

    pipe = client.pipeline(transaction=False)
    for player in players:
        user_id = str(player["user_id"])
        username = str(player.get("name", "Unknown"))
        final_score = max(0, int(player_scores.get(user_id, 0)))

        stats = await read_hash(game_stats_key(code, user_id))
        captures = int(stats.get("captures", 0))
        wipes_caused = int(stats.get("wipes_caused", 0))
        times_wiped = int(stats.get("times_wiped", 0))

        pipe.zincrby(GLOBAL_LB_KEY, final_score, username)
        stats_key = global_stats_key(username)
        pipe.hincrby(stats_key, "games", 1)
        pipe.hincrby(stats_key, "captures", captures)
        pipe.hincrby(stats_key, "wipes_caused", wipes_caused)
        pipe.hincrby(stats_key, "times_wiped", times_wiped)

    await pipe.execute()


async def get_global_leaderboard(limit: int = 20) -> list[dict[str, Any]]:
    client = get_redis()
    rows = await client.zrevrange(GLOBAL_LB_KEY, 0, max(0, limit - 1), withscores=True)
    output: list[dict[str, Any]] = []

    for idx, (raw_username, raw_score) in enumerate(rows, start=1):
        username = raw_username.decode("utf-8") if isinstance(raw_username, bytes) else str(raw_username)
        stats = await read_hash(global_stats_key(username))
        output.append(
            {
                "rank": idx,
                "username": username,
                "score": int(raw_score),
                "games": int(stats.get("games", 0)),
                "captures": int(stats.get("captures", 0)),
                "wipes_caused": int(stats.get("wipes_caused", 0)),
                "times_wiped": int(stats.get("times_wiped", 0)),
            }
        )

    return output