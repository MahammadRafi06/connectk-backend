"""Authentication endpoints: OIDC Authorization Code + PKCE flow."""
import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.middleware.auth import CurrentUser, _idle_ttl, _remaining_absolute_ttl
from app.models.cluster import ClusterUserAccess
from app.models.permissions import GroupPermission
from app.redis_client import RedisSessionStore, get_redis
from app.schemas.auth import UserProfile
from app.services.auth_service import AuthService, DEFAULT_PERMISSIONS, determine_connectk_group
from app.utils.audit import record_audit
from app.utils.crypto import decrypt, encrypt
from app.utils.response import success_response

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()
logger = structlog.get_logger()


async def _resolve_permissions(db: AsyncSession, connectk_group: str) -> dict[str, list[str]]:
    result = await db.execute(
        select(GroupPermission).where(
            GroupPermission.group_name == connectk_group,
            GroupPermission.enabled.is_(True),
        )
    )
    rows = result.scalars().all()
    if not rows:
        return DEFAULT_PERMISSIONS.get(connectk_group, DEFAULT_PERMISSIONS["developer"])

    perms: dict[str, list[str]] = {}
    for row in rows:
        perms.setdefault(row.page, []).append(row.action)
    return perms


async def _resolve_accessible_cluster_ids(db: AsyncSession, groups: list[str]) -> list[str]:
    if not groups:
        return []
    result = await db.execute(
        select(ClusterUserAccess.cluster_id).where(ClusterUserAccess.entra_group_id.in_(groups))
    )
    return list({str(cid) for (cid,) in result.all()})


@router.get("/login")
async def login(request: Request, response: Response):
    """Initiate OIDC Authorization Code + PKCE flow."""
    redis = await get_redis()
    store = RedisSessionStore(redis)
    auth_service = AuthService(store)

    code_verifier, code_challenge = auth_service.generate_pkce_pair()
    state = secrets.token_urlsafe(32)

    await redis.setex(f"pkce:{state}", 600, code_verifier)

    redirect_uri = settings.OIDC_REDIRECT_URI
    auth_url = auth_service.build_authorization_url(state, code_challenge, redirect_uri)

    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback")
async def callback(
    request: Request,
    response: Response,
    code: str,
    state: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Handle Entra ID redirect; exchange code for tokens; create session."""
    redis = await get_redis()
    store = RedisSessionStore(redis)
    auth_service = AuthService(store)

    code_verifier = await redis.get(f"pkce:{state}")
    if not code_verifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "AUTH_CSRF_INVALID", "message": "Invalid or expired state parameter."},
        )
    await redis.delete(f"pkce:{state}")

    try:
        tokens = await auth_service.exchange_code_for_tokens(
            code=code,
            code_verifier=code_verifier,
            redirect_uri=settings.OIDC_REDIRECT_URI,
        )
    except httpx.HTTPStatusError as e:
        provider_error = "Token exchange failed."
        try:
            error_json = e.response.json()
            err = error_json.get("error")
            err_desc = error_json.get("error_description")
            provider_error = f"{err}: {err_desc}" if err and err_desc else (err_desc or err or provider_error)
        except Exception:
            if e.response is not None and e.response.text:
                provider_error = e.response.text[:500]

        logger.error(
            "auth_token_exchange_failed",
            status_code=e.response.status_code if e.response is not None else None,
            detail=provider_error,
            redirect_uri=settings.OIDC_REDIRECT_URI,
        )

        message = "Token exchange failed."
        if not settings.is_production and provider_error:
            message = f"Token exchange failed: {provider_error}"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REFRESH_FAILED", "message": message},
        ) from e
    except Exception as e:
        logger.error("auth_token_exchange_unexpected_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REFRESH_FAILED", "message": "Token exchange failed."},
        ) from e

    id_token_claims = await auth_service.decode_id_token(tokens.get("id_token", ""))

    user_id = id_token_claims.get("oid") or id_token_claims.get("sub") or str(uuid.uuid4())
    groups = id_token_claims.get("groups", [])
    group_map = {
        "admin_groups": settings.admin_group_ids_list,
        "manager_groups": settings.manager_group_ids_list,
    }
    if settings.INITIAL_ADMIN_ENTRA_GROUP_ID and settings.INITIAL_ADMIN_ENTRA_GROUP_ID in groups:
        connectk_group = "admin"
    else:
        connectk_group = determine_connectk_group(groups, group_map)
    permissions = await _resolve_permissions(db, connectk_group)
    accessible_cluster_ids = await _resolve_accessible_cluster_ids(db, groups)

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    session_id = await auth_service.create_session(
        user_id=user_id,
        tokens=tokens,
        id_token_claims=id_token_claims,
        connectk_group=connectk_group,
        permissions=permissions,
        accessible_cluster_ids=accessible_cluster_ids,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    await record_audit(
        db, user_id, "login", "user_session", None,
        details={"connectk_group": connectk_group, "email": id_token_claims.get("email", "")},
        ip_address=ip_address,
        user_agent=user_agent,
    )

    redirect_resp = RedirectResponse(url=f"{settings.FRONTEND_URL}/clusters", status_code=302)
    redirect_resp.set_cookie(
        key="connectk_session",
        value=session_id,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        max_age=settings.SESSION_MAX_AGE_HOURS * 3600,
        path="/",
    )
    return redirect_resp


@router.post("/refresh")
async def refresh_token(
    request: Request,
    current_user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_id: str | None = Cookie(default=None, alias="connectk_session"),
):
    """Refresh access token using stored refresh token."""
    if not session_id:
        raise HTTPException(status_code=401, detail={"code": "AUTH_SESSION_EXPIRED", "message": "No session."})

    redis = await get_redis()
    store = RedisSessionStore(redis)
    session_data = await store.get_session(session_id)

    if not session_data:
        raise HTTPException(status_code=401, detail={"code": "AUTH_SESSION_EXPIRED", "message": "Session not found."})

    encrypted_refresh = session_data.get("refresh_token")
    if not encrypted_refresh:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REFRESH_FAILED", "message": "No refresh token available."})

    try:
        refresh_tok = decrypt(encrypted_refresh)
    except Exception:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REFRESH_FAILED", "message": "Invalid refresh token."})

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            settings.oidc_token_endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.AZURE_CLIENT_ID,
                "client_secret": settings.AZURE_CLIENT_SECRET,
                "refresh_token": refresh_tok,
                "scope": "openid profile email offline_access",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail={"code": "AUTH_REFRESH_FAILED", "message": "Unable to renew your session. Please log in again."})

        tokens = resp.json()
        from datetime import timedelta
        session_data["access_token"] = encrypt(tokens["access_token"])
        if "refresh_token" in tokens:
            session_data["refresh_token"] = encrypt(tokens["refresh_token"])
        expires_in = tokens.get("expires_in", 3600)
        session_data["token_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()
        ttl = min(_idle_ttl(), _remaining_absolute_ttl(session_data["created_at"]))
        await store.set_session(session_id, session_data, max(ttl, 1))

    return JSONResponse(content=success_response({"refreshed": True}))


@router.post("/logout")
async def logout(
    request: Request,
    current_user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_id: str | None = Cookie(default=None, alias="connectk_session"),
):
    """Invalidate session, revoke refresh token, redirect to Entra logout."""
    redis = await get_redis()
    store = RedisSessionStore(redis)
    auth_service = AuthService(store)

    if session_id:
        session_data = await store.get_session(session_id)
        if session_data and session_data.get("refresh_token"):
            await auth_service.revoke_refresh_token(session_data["refresh_token"])
        await store.delete_session(session_id)

    await record_audit(
        db, current_user.id, "logout", "user_session", None,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    post_logout_uri = f"{settings.FRONTEND_URL}/login"
    logout_url = auth_service.build_logout_url(post_logout_uri)

    resp = JSONResponse(content={"logout_url": logout_url})
    resp.delete_cookie(key="connectk_session", path="/")
    resp.delete_cookie(key="connectk_csrf", path="/")
    return resp


@router.get("/me")
async def get_me(current_user: CurrentUser):
    """Return current user profile, groups, permissions."""
    return JSONResponse(content=success_response(current_user.model_dump()))


@router.get("/sessions")
async def get_my_sessions(current_user: CurrentUser):
    """Return current user's active sessions (accessible by all authenticated users)."""
    redis = await get_redis()
    store = RedisSessionStore(redis)
    sessions = await store.get_user_sessions(current_user.id)
    items = [
        {
            "session_id": s.get("session_id"),
            "ip_address": s.get("ip_address"),
            "user_agent": s.get("user_agent"),
            "created_at": s.get("created_at"),
            "last_activity_at": s.get("last_activity_at"),
        }
        for s in sessions
    ]
    return JSONResponse(content=success_response(items))
