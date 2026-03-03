import json
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings

settings = get_settings()

_redis_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
    return _redis_pool


async def close_redis() -> None:
    global _redis_pool
    if _redis_pool:
        await _redis_pool.aclose()
        _redis_pool = None


class RedisSessionStore:
    SESSION_PREFIX = "session:"
    SESSION_INDEX_PREFIX = "user_sessions:"

    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def set_session(self, session_id: str, data: dict, ttl_seconds: int) -> None:
        key = f"{self.SESSION_PREFIX}{session_id}"
        await self.redis.setex(key, ttl_seconds, json.dumps(data))
        user_id = data.get("user_id")
        if user_id:
            index_key = f"{self.SESSION_INDEX_PREFIX}{user_id}"
            await self.redis.sadd(index_key, session_id)
            await self.redis.expire(index_key, ttl_seconds + 3600)

    async def get_session(self, session_id: str) -> dict | None:
        key = f"{self.SESSION_PREFIX}{session_id}"
        data = await self.redis.get(key)
        return json.loads(data) if data else None

    async def delete_session(self, session_id: str) -> None:
        key = f"{self.SESSION_PREFIX}{session_id}"
        session_data = await self.get_session(session_id)
        if session_data and session_data.get("user_id"):
            index_key = f"{self.SESSION_INDEX_PREFIX}{session_data['user_id']}"
            await self.redis.srem(index_key, session_id)
        await self.redis.delete(key)

    async def get_user_sessions(self, user_id: str) -> list[dict]:
        index_key = f"{self.SESSION_INDEX_PREFIX}{user_id}"
        session_ids = await self.redis.smembers(index_key)
        sessions = []
        for sid in session_ids:
            data = await self.get_session(sid)
            if data:
                sessions.append({**data, "session_id": sid})
            else:
                await self.redis.srem(index_key, sid)
        return sessions

    async def refresh_session_ttl(self, session_id: str, ttl_seconds: int) -> bool:
        key = f"{self.SESSION_PREFIX}{session_id}"
        return bool(await self.redis.expire(key, ttl_seconds))

    async def acquire_lock(self, lock_key: str, ttl_seconds: int = 30) -> bool:
        return bool(await self.redis.set(f"lock:{lock_key}", "1", nx=True, ex=ttl_seconds))

    async def release_lock(self, lock_key: str) -> None:
        await self.redis.delete(f"lock:{lock_key}")

    async def publish_event(self, channel: str, event_data: dict) -> None:
        await self.redis.publish(channel, json.dumps(event_data))

    async def get_rate_limit_count(self, key: str) -> int:
        val = await self.redis.get(f"ratelimit:{key}")
        return int(val) if val else 0

    async def increment_rate_limit(self, key: str, window_seconds: int) -> int:
        pipe = self.redis.pipeline()
        full_key = f"ratelimit:{key}"
        pipe.incr(full_key)
        pipe.expire(full_key, window_seconds)
        results = await pipe.execute()
        return results[0]
