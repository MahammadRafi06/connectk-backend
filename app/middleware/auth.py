"""
Auth dependency for FastAPI endpoints.
Validates session cookie, refreshes tokens if needed, and injects user context.
"""
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import Cookie, Depends, HTTPException, Request, status

from app.config import get_settings
from app.redis_client import RedisSessionStore, get_redis
from app.schemas.auth import UserProfile
from app.utils.crypto import decrypt, encrypt

settings = get_settings()

SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"


def _idle_ttl() -> int:
    return settings.SESSION_TIMEOUT_MINUTES * 60


def _remaining_absolute_ttl(created_at_iso: str) -> int:
    """Seconds remaining until the absolute session expiry (SESSION_MAX_AGE_HOURS)."""
    created_at = datetime.fromisoformat(created_at_iso)
    max_age = timedelta(hours=settings.SESSION_MAX_AGE_HOURS)
    remaining = (created_at + max_age) - datetime.now(timezone.utc)
    return max(int(remaining.total_seconds()), 0)


async def get_current_user(
    request: Request,
    session_cookie: str | None = Cookie(default=None, alias="connectk_session"),
) -> UserProfile:
    if not session_cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_SESSION_EXPIRED", "message": "Your session has expired. Please log in again."},
        )

    redis = await get_redis()
    store = RedisSessionStore(redis)
    session_data = await store.get_session(session_cookie)

    if not session_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_SESSION_EXPIRED", "message": "Your session has expired. Please log in again."},
        )

    now = datetime.now(timezone.utc)

    created_at = datetime.fromisoformat(session_data["created_at"])
    if now - created_at > timedelta(hours=settings.SESSION_MAX_AGE_HOURS):
        await store.delete_session(session_cookie)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_SESSION_EXPIRED", "message": "Your session has reached its maximum lifetime. Please log in again."},
        )

    last_activity = datetime.fromisoformat(session_data["last_activity_at"])
    if now - last_activity > timedelta(minutes=settings.SESSION_TIMEOUT_MINUTES):
        await store.delete_session(session_cookie)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_SESSION_EXPIRED", "message": "Your session has expired due to inactivity."},
        )

    session_data["last_activity_at"] = now.isoformat()
    ttl = min(_idle_ttl(), _remaining_absolute_ttl(session_data["created_at"]))
    if ttl <= 0:
        await store.delete_session(session_cookie)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_SESSION_EXPIRED", "message": "Your session has expired. Please log in again."},
        )
    await store.set_session(session_cookie, session_data, ttl)

    token_expires_at_str = session_data.get("token_expires_at")
    if token_expires_at_str:
        token_expires_at = datetime.fromisoformat(token_expires_at_str)
        if now >= token_expires_at - timedelta(minutes=5):
            await _refresh_tokens(session_cookie, session_data, store)

    claims = session_data.get("id_token_claims", {})
    user_profile = UserProfile(
        id=session_data["user_id"],
        email=claims.get("email", claims.get("preferred_username", "")),
        name=claims.get("name", ""),
        groups=claims.get("groups", []),
        roles=claims.get("roles", []),
        connectk_group=session_data.get("connectk_group", "developer"),
        accessible_cluster_ids=session_data.get("accessible_cluster_ids", []),
        permissions=session_data.get("permissions", {}),
    )

    request.state.session_id = session_cookie
    request.state.user = user_profile
    return user_profile


async def _refresh_tokens(
    session_id: str, session_data: dict, store: RedisSessionStore
) -> None:
    encrypted_refresh = session_data.get("refresh_token")
    if not encrypted_refresh:
        return

    try:
        refresh_token = decrypt(encrypted_refresh)
    except Exception:
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                settings.oidc_token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.AZURE_CLIENT_ID,
                    "client_secret": settings.AZURE_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "scope": "openid profile email offline_access",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                tokens = resp.json()
                session_data["access_token"] = encrypt(tokens["access_token"])
                if "refresh_token" in tokens:
                    session_data["refresh_token"] = encrypt(tokens["refresh_token"])
                expires_in = tokens.get("expires_in", 3600)
                session_data["token_expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                ).isoformat()
                ttl = min(_idle_ttl(), _remaining_absolute_ttl(session_data["created_at"]))
                await store.set_session(session_id, session_data, max(ttl, 1))
    except Exception:
        pass


CurrentUser = Annotated[UserProfile, Depends(get_current_user)]


def require_group(*groups: str):
    """Dependency factory to require specific group membership."""
    async def check_group(user: CurrentUser) -> UserProfile:
        if user.connectk_group not in groups:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "AUTH_INSUFFICIENT_PERMISSION",
                    "message": "You do not have permission to perform this action.",
                },
            )
        return user
    return check_group


def require_permission(page: str, action: str):
    """Dependency factory to check page/action permission."""
    async def check_permission(user: CurrentUser) -> UserProfile:
        page_perms = user.permissions.get(page, [])
        if action not in page_perms and user.connectk_group != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "AUTH_INSUFFICIENT_PERMISSION",
                    "message": "You do not have permission to perform this action.",
                },
            )
        return user
    return check_permission
