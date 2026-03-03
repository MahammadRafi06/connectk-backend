from fastapi import HTTPException, Request, status

from app.redis_client import get_redis, RedisSessionStore


async def check_rate_limit(
    request: Request,
    user_id: str,
    limit: int,
    window_seconds: int,
    scope: str = "general",
) -> None:
    redis = await get_redis()
    store = RedisSessionStore(redis)
    key = f"{user_id}:{scope}"
    count = await store.increment_rate_limit(key, window_seconds)
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Too many requests. Please wait and try again.",
            },
            headers={"Retry-After": str(window_seconds)},
        )
