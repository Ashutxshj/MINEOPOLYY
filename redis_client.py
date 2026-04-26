from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

LOBBY_TTL_SECONDS = 3600
GAME_EXPIRY_SECONDS = 600

# Required key patterns.
LOBBY_KEY = "lobby:{code}"
LOBBY_PLAYERS_KEY = "lobby:{code}:players"
LOBBY_SPECTATORS_KEY = "lobby:{code}:spectators"
LOBBY_CHANNEL = "lobby:{code}:events"
GRID_KEY = "grid:{code}"
MINES_KEY = "mines:{code}"
COOLDOWN_KEY = "cooldown:{code}:{user_id}"
LEADERBOARD_KEY = "leaderboard:{code}"
GLOBAL_LB_KEY = "global:leaderboard"
GLOBAL_STATS_KEY = "stats:{username}"
USER_KEY = "user:{code}:{user_id}"

# Internal helper keys for per-match stats.
GAME_STATS_KEY = "game_stats:{code}:{user_id}"


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def dumps_json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=True)


def loads_json(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(_decode(value))
    except (json.JSONDecodeError, TypeError):
        return fallback


@dataclass
class _Entry:
    kind: str
    value: Any
    expires_at: float | None = None


@dataclass
class _Store:
    data: dict[str, _Entry] = field(default_factory=dict)
    subscribers: dict[str, set["PubSub"]] = field(default_factory=lambda: defaultdict(set))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def sweep_expired(self) -> None:
        now = time.time()
        expired = [key for key, entry in self.data.items() if entry.expires_at is not None and entry.expires_at <= now]
        for key in expired:
            self.data.pop(key, None)


_store = _Store()
_redis: "InMemoryRedis | None" = None


class PubSub:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._channels: set[str] = set()
        self._closed = False

    async def subscribe(self, channel: str) -> None:
        async with self._store.lock:
            self._store.subscribers[channel].add(self)
            self._channels.add(channel)

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float | None = None) -> dict[str, Any] | None:
        if self._closed:
            return None
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def unsubscribe(self) -> None:
        async with self._store.lock:
            for channel in list(self._channels):
                subscribers = self._store.subscribers.get(channel)
                if subscribers is not None:
                    subscribers.discard(self)
                    if not subscribers:
                        self._store.subscribers.pop(channel, None)
            self._channels.clear()

    async def aclose(self) -> None:
        self._closed = True
        await self.unsubscribe()

    async def put(self, payload: dict[str, Any]) -> None:
        if not self._closed:
            await self._queue.put(payload)


class Pipeline:
    def __init__(self, client: "InMemoryRedis", transaction: bool = True) -> None:
        self._client = client
        self._commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> "Pipeline":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def watch(self, *keys: str) -> None:
        return None

    async def unwatch(self) -> None:
        return None

    def multi(self) -> None:
        return None

    def _queue(self, method: str, *args: Any, **kwargs: Any) -> "Pipeline":
        self._commands.append((method, args, kwargs))
        return self

    def delete(self, *keys: str) -> "Pipeline":
        return self._queue("delete", *keys)

    def rpush(self, key: str, *values: Any) -> "Pipeline":
        return self._queue("rpush", key, *values)

    def expire(self, key: str, ttl: int) -> "Pipeline":
        return self._queue("expire", key, ttl)

    def hset(self, key: str, *args: Any, **kwargs: Any) -> "Pipeline":
        return self._queue("hset", key, *args, **kwargs)

    def set(self, key: str, value: Any, px: int | None = None) -> "Pipeline":
        return self._queue("set", key, value, px=px)

    def hincrby(self, key: str, field: str, amount: int) -> "Pipeline":
        return self._queue("hincrby", key, field, amount)

    def hdel(self, key: str, *fields: str) -> "Pipeline":
        return self._queue("hdel", key, *fields)

    def sadd(self, key: str, *values: Any) -> "Pipeline":
        return self._queue("sadd", key, *values)

    def srem(self, key: str, *values: Any) -> "Pipeline":
        return self._queue("srem", key, *values)

    def zadd(self, key: str, mapping: dict[str, int]) -> "Pipeline":
        return self._queue("zadd", key, mapping)

    def zincrby(self, key: str, amount: int, member: str) -> "Pipeline":
        return self._queue("zincrby", key, amount, member)

    async def hget(self, key: str, field: str) -> Any:
        return await self._client.hget(key, field)

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        async with self._client.lock:
            self._client._sweep_expired()
            for method, args, kwargs in self._commands:
                fn = getattr(self._client, f"_locked_{method}")
                results.append(fn(*args, **kwargs))
        self._commands.clear()
        return results


class InMemoryRedis:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.lock = store.lock

    def _sweep_expired(self) -> None:
        self._store.sweep_expired()

    def _get_entry(self, key: str) -> _Entry | None:
        self._sweep_expired()
        return self._store.data.get(key)

    def _ensure_entry(self, key: str, kind: str, default: Any) -> _Entry:
        self._sweep_expired()
        entry = self._store.data.get(key)
        if entry is None or entry.kind != kind:
            entry = _Entry(kind=kind, value=default)
            self._store.data[key] = entry
        return entry

    def _set_expiry(self, key: str, ttl_seconds: float | None) -> bool:
        entry = self._store.data.get(key)
        if entry is None:
            return False
        entry.expires_at = time.time() + ttl_seconds if ttl_seconds is not None else None
        return True

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    def pipeline(self, transaction: bool = True) -> Pipeline:
        return Pipeline(self, transaction=transaction)

    def pubsub(self) -> PubSub:
        return PubSub(self._store)

    async def exists(self, key: str) -> int:
        async with self.lock:
            self._sweep_expired()
            return 1 if key in self._store.data else 0

    async def lrange(self, key: str, start: int, end: int) -> list[Any]:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "list":
                return []
            values = list(entry.value)
            if end == -1:
                end = len(values) - 1
            return values[start : end + 1]

    async def delete(self, *keys: str) -> int:
        async with self.lock:
            return self._locked_delete(*keys)

    def _locked_delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self._store.data:
                self._store.data.pop(key, None)
                deleted += 1
        return deleted

    async def rpush(self, key: str, *values: Any) -> int:
        async with self.lock:
            return self._locked_rpush(key, *values)

    def _locked_rpush(self, key: str, *values: Any) -> int:
        entry = self._ensure_entry(key, "list", [])
        entry.value.extend(values)
        return len(entry.value)

    async def expire(self, key: str, ttl: int) -> bool:
        async with self.lock:
            return self._locked_expire(key, ttl)

    def _locked_expire(self, key: str, ttl: int) -> bool:
        return self._set_expiry(key, float(ttl))

    async def hset(self, key: str, *args: Any, mapping: dict[str, Any] | None = None) -> int:
        async with self.lock:
            return self._locked_hset(key, *args, mapping=mapping)

    def _locked_hset(self, key: str, *args: Any, mapping: dict[str, Any] | None = None) -> int:
        entry = self._ensure_entry(key, "hash", {})
        changes = 0
        updates: dict[str, Any] = {}
        if mapping is not None:
            updates.update({str(k): v for k, v in mapping.items()})
        elif len(args) == 2:
            updates[str(args[0])] = args[1]
        else:
            raise TypeError("hset expects either mapping=... or field/value")

        for field, value in updates.items():
            if field not in entry.value:
                changes += 1
            entry.value[field] = value
        return changes

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "hash":
                return {}
            return {
                str(field).encode("utf-8"): str(value).encode("utf-8")
                for field, value in entry.value.items()
            }

    async def hget(self, key: str, field: str) -> bytes | None:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "hash":
                return None
            value = entry.value.get(field)
            if value is None:
                return None
            return str(value).encode("utf-8")

    async def hmget(self, key: str, fields: list[str]) -> list[bytes | None]:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "hash":
                return [None for _ in fields]
            output: list[bytes | None] = []
            for field in fields:
                value = entry.value.get(field)
                output.append(None if value is None else str(value).encode("utf-8"))
            return output

    async def hdel(self, key: str, *fields: str) -> int:
        async with self.lock:
            return self._locked_hdel(key, *fields)

    def _locked_hdel(self, key: str, *fields: str) -> int:
        entry = self._get_entry(key)
        if entry is None or entry.kind != "hash":
            return 0
        deleted = 0
        for field in fields:
            if field in entry.value:
                entry.value.pop(field, None)
                deleted += 1
        return deleted

    async def hkeys(self, key: str) -> list[bytes]:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "hash":
                return []
            return [str(field).encode("utf-8") for field in entry.value.keys()]

    async def set(self, key: str, value: Any, px: int | None = None) -> bool:
        async with self.lock:
            return self._locked_set(key, value, px=px)

    def _locked_set(self, key: str, value: Any, px: int | None = None) -> bool:
        expires_at = None if px is None else time.time() + (float(px) / 1000.0)
        self._store.data[key] = _Entry(kind="string", value=str(value), expires_at=expires_at)
        return True

    async def pttl(self, key: str) -> int:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None:
                return -2
            if entry.expires_at is None:
                return -1
            return max(0, int((entry.expires_at - time.time()) * 1000))

    async def sadd(self, key: str, *values: Any) -> int:
        async with self.lock:
            return self._locked_sadd(key, *values)

    def _locked_sadd(self, key: str, *values: Any) -> int:
        entry = self._ensure_entry(key, "set", set())
        added = 0
        for value in values:
            if value not in entry.value:
                entry.value.add(value)
                added += 1
        return added

    async def srem(self, key: str, *values: Any) -> int:
        async with self.lock:
            return self._locked_srem(key, *values)

    def _locked_srem(self, key: str, *values: Any) -> int:
        entry = self._get_entry(key)
        if entry is None or entry.kind != "set":
            return 0
        removed = 0
        for value in values:
            if value in entry.value:
                entry.value.remove(value)
                removed += 1
        return removed

    async def scard(self, key: str) -> int:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "set":
                return 0
            return len(entry.value)

    async def sismember(self, key: str, value: Any) -> bool:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "set":
                return False
            return value in entry.value

    async def smembers(self, key: str) -> set[bytes]:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "set":
                return set()
            return {str(value).encode("utf-8") for value in entry.value}

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        async with self.lock:
            return self._locked_hincrby(key, field, amount)

    def _locked_hincrby(self, key: str, field: str, amount: int) -> int:
        entry = self._ensure_entry(key, "hash", {})
        next_value = int(entry.value.get(field, 0)) + int(amount)
        entry.value[field] = next_value
        return next_value

    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        async with self.lock:
            return self._locked_zadd(key, mapping)

    def _locked_zadd(self, key: str, mapping: dict[str, int]) -> int:
        entry = self._ensure_entry(key, "zset", {})
        added = 0
        for member, score in mapping.items():
            if member not in entry.value:
                added += 1
            entry.value[member] = float(score)
        return added

    async def zincrby(self, key: str, amount: int, member: str) -> float:
        async with self.lock:
            return self._locked_zincrby(key, amount, member)

    def _locked_zincrby(self, key: str, amount: int, member: str) -> float:
        entry = self._ensure_entry(key, "zset", {})
        next_value = float(entry.value.get(member, 0)) + float(amount)
        entry.value[member] = next_value
        return next_value

    async def zrevrange(self, key: str, start: int, end: int, withscores: bool = False) -> list[Any]:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "zset":
                return []
            rows = sorted(entry.value.items(), key=lambda item: (-item[1], item[0]))
            if end == -1:
                end = len(rows) - 1
            rows = rows[start : end + 1]
            if withscores:
                return [(member.encode("utf-8"), score) for member, score in rows]
            return [member.encode("utf-8") for member, _ in rows]

    async def zscore(self, key: str, member: str) -> float | None:
        async with self.lock:
            entry = self._get_entry(key)
            if entry is None or entry.kind != "zset":
                return None
            value = entry.value.get(member)
            return None if value is None else float(value)

    async def publish(self, channel: str, payload: str) -> int:
        async with self.lock:
            subscribers = list(self._store.subscribers.get(channel, set()))
        message = {"type": "message", "data": payload}
        for subscriber in subscribers:
            await subscriber.put(message)
        return len(subscribers)

    async def scan(self, cursor: int = 0, match: str | None = None, count: int = 100) -> tuple[int, list[bytes]]:
        async with self.lock:
            self._sweep_expired()
            keys = sorted(self._store.data.keys())
            if match:
                keys = [key for key in keys if fnmatch.fnmatch(key, match)]
            start = int(cursor)
            batch = keys[start : start + count]
            next_cursor = 0 if start + count >= len(keys) else start + count
            return next_cursor, [key.encode("utf-8") for key in batch]


async def init_redis() -> InMemoryRedis:
    global _redis
    if _redis is None:
        _redis = InMemoryRedis(_store)
        await _redis.ping()
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def get_redis() -> InMemoryRedis:
    if _redis is None:
        raise RuntimeError("In-memory store is not initialized")
    return _redis


def get_store_lock() -> asyncio.Lock:
    return _store.lock


def lobby_key(code: str) -> str:
    return LOBBY_KEY.format(code=code)


def lobby_players_key(code: str) -> str:
    return LOBBY_PLAYERS_KEY.format(code=code)


def lobby_spectators_key(code: str) -> str:
    return LOBBY_SPECTATORS_KEY.format(code=code)


def lobby_channel(code: str) -> str:
    return LOBBY_CHANNEL.format(code=code)


def grid_key(code: str) -> str:
    return GRID_KEY.format(code=code)


def mines_key(code: str) -> str:
    return MINES_KEY.format(code=code)


def cooldown_key(code: str, user_id: str) -> str:
    return COOLDOWN_KEY.format(code=code, user_id=user_id)


def leaderboard_key(code: str) -> str:
    return LEADERBOARD_KEY.format(code=code)


def global_stats_key(username: str) -> str:
    return GLOBAL_STATS_KEY.format(username=username)


def user_key(code: str, user_id: str) -> str:
    return USER_KEY.format(code=code, user_id=user_id)


def game_stats_key(code: str, user_id: str) -> str:
    return GAME_STATS_KEY.format(code=code, user_id=user_id)


async def read_players(code: str) -> list[dict[str, Any]]:
    client = get_redis()
    raw_players = await client.lrange(lobby_players_key(code), 0, -1)
    players: list[dict[str, Any]] = []
    for raw in raw_players:
        player = loads_json(raw, {})
        if isinstance(player, dict):
            players.append(player)
    return players


async def write_players(code: str, players: list[dict[str, Any]], ttl: int = LOBBY_TTL_SECONDS) -> None:
    client = get_redis()
    key = lobby_players_key(code)
    pipe = client.pipeline(transaction=True)
    pipe.delete(key)
    if players:
        pipe.rpush(key, *[dumps_json(player) for player in players])
    pipe.expire(key, ttl)
    await pipe.execute()


async def apply_lobby_ttl(code: str, ttl: int = LOBBY_TTL_SECONDS) -> None:
    client = get_redis()
    key_groups = [
        lobby_key(code),
        lobby_players_key(code),
        lobby_spectators_key(code),
        grid_key(code),
        mines_key(code),
        leaderboard_key(code),
    ]
    pipe = client.pipeline(transaction=False)
    for key in key_groups:
        pipe.expire(key, ttl)
    await pipe.execute()


async def _scan_iter(match: str) -> AsyncIterator[str]:
    client = get_redis()
    cursor = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=match, count=100)
        for key in keys:
            yield _decode(key)
        if cursor == 0:
            break


async def expire_game_keys(code: str, ttl: int = GAME_EXPIRY_SECONDS) -> None:
    client = get_redis()
    fixed_keys = [
        lobby_key(code),
        lobby_players_key(code),
        lobby_spectators_key(code),
        grid_key(code),
        mines_key(code),
        leaderboard_key(code),
    ]

    dynamic_patterns = [
        USER_KEY.format(code=code, user_id="*"),
        COOLDOWN_KEY.format(code=code, user_id="*"),
        GAME_STATS_KEY.format(code=code, user_id="*"),
    ]

    keys_to_expire = set(fixed_keys)
    for pattern in dynamic_patterns:
        async for key in _scan_iter(pattern):
            keys_to_expire.add(key)

    if not keys_to_expire:
        return

    pipe = client.pipeline(transaction=False)
    for key in keys_to_expire:
        pipe.expire(key, ttl)
    await pipe.execute()


async def publish_event(code: str, payload: dict[str, Any]) -> None:
    client = get_redis()
    await client.publish(lobby_channel(code), dumps_json(payload))


async def create_subscription(code: str) -> PubSub:
    client = get_redis()
    pubsub = client.pubsub()
    await pubsub.subscribe(lobby_channel(code))
    return pubsub


async def close_subscription(pubsub: PubSub) -> None:
    try:
        await pubsub.unsubscribe()
    finally:
        await pubsub.aclose()


async def spectator_count(code: str) -> int:
    client = get_redis()
    return int(await client.scard(lobby_spectators_key(code)))


async def read_hash(key: str) -> dict[str, str]:
    client = get_redis()
    raw = await client.hgetall(key)
    parsed: dict[str, str] = {}
    for k, v in raw.items():
        parsed[str(_decode(k))] = str(_decode(v))
    return parsed
