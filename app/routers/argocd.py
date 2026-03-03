"""ArgoCD API proxy router.

Proxies requests to the ArgoCD server, forwarding the user's AAD access token
from their ConnectK session. This allows the ArgoCD UI to work without a
separate login since ArgoCD is configured with the same Azure AD OAuth app.
"""

import structlog
import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from app.config import get_settings
from app.middleware.auth import get_current_user
from app.redis_client import RedisSessionStore, get_redis
from app.utils.crypto import decrypt

settings = get_settings()
logger = structlog.get_logger()

router = APIRouter(prefix="/api/argocd", tags=["argocd"])

PROXY_TIMEOUT = 30.0
STREAM_TIMEOUT = 300.0

FORWARD_HEADERS = {"accept", "content-type", "accept-encoding"}


async def _get_argocd_token(session_id: str) -> str:
    """Decrypt the user's AAD ID token from their Redis session.

    ArgoCD validates OIDC ID tokens from the configured provider, not access tokens.
    The ID token has the correct audience (client_id) that ArgoCD expects.
    """
    redis = await get_redis()
    store = RedisSessionStore(redis)
    session_data = await store.get_session(session_id)

    if not session_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ARGOCD_TOKEN_MISSING", "message": "No session available for ArgoCD."},
        )

    # Prefer id_token (ArgoCD validates OIDC ID tokens), fall back to access_token
    encrypted_token = session_data.get("id_token") or session_data.get("access_token")
    if not encrypted_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ARGOCD_TOKEN_MISSING", "message": "No token available for ArgoCD."},
        )

    try:
        return decrypt(encrypted_token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ARGOCD_TOKEN_INVALID", "message": "Failed to decrypt token."},
        )


def _is_streaming_request(request: Request, path: str) -> bool:
    """Check if this is a streaming request (watch, SSE, or follow-logs)."""
    if request.query_params.get("watch", "").lower() == "true":
        return True
    if request.query_params.get("follow", "").lower() == "true":
        return True
    if "/stream/" in path or path.startswith("stream/"):
        return True
    if "text/event-stream" in request.headers.get("accept", ""):
        return True
    return False


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def argocd_proxy(
    path: str,
    request: Request,
    user=Depends(get_current_user),
    session_cookie: str | None = Cookie(default=None, alias="connectk_session"),
):
    if not settings.ARGOCD_SERVER_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ARGOCD_NOT_CONFIGURED", "message": "ArgoCD server URL is not configured."},
        )

    token = await _get_argocd_token(session_cookie)

    target_url = f"{settings.ARGOCD_SERVER_URL.rstrip('/')}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    headers = {"Authorization": f"Bearer {token}"}
    for key, value in request.headers.items():
        if key.lower() in FORWARD_HEADERS:
            headers[key] = value

    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None

    is_stream = _is_streaming_request(request, path)
    timeout = httpx.Timeout(STREAM_TIMEOUT if is_stream else PROXY_TIMEOUT)

    try:
        client = httpx.AsyncClient(verify=False, timeout=timeout)

        if is_stream:
            req = client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            resp = await client.send(req, stream=True)

            async def stream_body():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                content=stream_body(),
                status_code=resp.status_code,
                headers={
                    k: v for k, v in resp.headers.items()
                    if k.lower() in ("content-type", "cache-control", "transfer-encoding")
                },
            )
        else:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            await client.aclose()
            response_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() in ("content-type", "cache-control")
            }
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=response_headers,
            )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "ARGOCD_UNREACHABLE", "message": "Cannot connect to ArgoCD server."},
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "ARGOCD_TIMEOUT", "message": "ArgoCD server request timed out."},
        )
