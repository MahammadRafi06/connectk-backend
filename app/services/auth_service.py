"""
OIDC Authorization Code + PKCE authentication service for Azure Entra ID.
"""
import hashlib
import secrets
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from jose import JWTError, jwt

from app.config import get_settings
from app.redis_client import RedisSessionStore
from app.utils.crypto import decrypt, encrypt

settings = get_settings()

SCOPES = "openid profile email offline_access"


class AuthService:
    def __init__(self, session_store: RedisSessionStore):
        self.store = session_store

    def generate_pkce_pair(self) -> tuple[str, str]:
        """Return (code_verifier, code_challenge)."""
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = (
            __import__("base64").urlsafe_b64encode(digest).rstrip(b"=").decode()
        )
        return verifier, challenge

    def build_authorization_url(
        self, state: str, code_challenge: str, redirect_uri: str
    ) -> str:
        params = {
            "client_id": settings.AZURE_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
        }
        return f"{settings.oidc_authorization_endpoint}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_tokens(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                settings.oidc_token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.AZURE_CLIENT_ID,
                    "client_secret": settings.AZURE_CLIENT_SECRET,
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                    "scope": SCOPES,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()

    async def decode_id_token(self, id_token: str) -> dict:
        """Decode and verify ID token. In non-production, signature check is skipped."""
        try:
            if settings.is_production:
                jwks = await self._fetch_jwks()
                header = jwt.get_unverified_header(id_token)
                key = self._find_key(jwks, header.get("kid"))
                claims = jwt.decode(
                    id_token,
                    key=key,
                    algorithms=["RS256"],
                    audience=settings.AZURE_CLIENT_ID,
                    issuer=f"{settings.oidc_authority}",
                )
            else:
                claims = jwt.decode(
                    id_token,
                    key="",
                    options={
                        "verify_signature": False,
                        "verify_aud": False,
                        "verify_exp": False,
                    },
                )
            return claims
        except JWTError:
            return {}

    async def _fetch_jwks(self) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.oidc_jwks_uri, timeout=10)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _find_key(jwks: dict, kid: str | None) -> dict:
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
        raise JWTError("Matching signing key not found in JWKS")

    async def create_session(
        self,
        user_id: str,
        tokens: dict,
        id_token_claims: dict,
        connectk_group: str,
        permissions: dict,
        accessible_cluster_ids: list[str],
        ip_address: str | None,
        user_agent: str | None,
    ) -> str:
        session_id = secrets.token_urlsafe(32)
        expires_in = tokens.get("expires_in", 3600)
        now = datetime.now(timezone.utc)

        session_data = {
            "user_id": user_id,
            "access_token": encrypt(tokens["access_token"]),
            "refresh_token": encrypt(tokens.get("refresh_token", "")),
            "id_token": encrypt(tokens.get("id_token", "")),
            "id_token_claims": id_token_claims,
            "token_expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
            "connectk_group": connectk_group,
            "permissions": permissions,
            "accessible_cluster_ids": accessible_cluster_ids,
            "created_at": now.isoformat(),
            "last_activity_at": now.isoformat(),
            "ip_address": ip_address,
            "user_agent": user_agent,
        }

        ttl = settings.SESSION_MAX_AGE_HOURS * 3600
        await self.store.set_session(session_id, session_data, ttl)

        # Enforce concurrent session limit
        if settings.MAX_CONCURRENT_SESSIONS > 0:
            await self._enforce_session_limit(user_id)

        return session_id

    async def _enforce_session_limit(self, user_id: str) -> None:
        sessions = await self.store.get_user_sessions(user_id)
        if len(sessions) > settings.MAX_CONCURRENT_SESSIONS:
            sessions.sort(key=lambda s: s.get("last_activity_at", ""))
            oldest = sessions[0]
            await self.store.delete_session(oldest["session_id"])

    async def revoke_refresh_token(self, refresh_token_encrypted: str) -> None:
        try:
            refresh_token = decrypt(refresh_token_encrypted)
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}/oauth2/v2.0/revoke",
                    data={
                        "client_id": settings.AZURE_CLIENT_ID,
                        "client_secret": settings.AZURE_CLIENT_SECRET,
                        "token": refresh_token,
                    },
                    timeout=10,
                )
        except Exception:
            pass

    def build_logout_url(self, post_logout_redirect_uri: str) -> str:
        params = {
            "client_id": settings.AZURE_CLIENT_ID,
            "post_logout_redirect_uri": post_logout_redirect_uri,
        }
        return f"{settings.oidc_end_session_endpoint}?{urllib.parse.urlencode(params)}"


def determine_connectk_group(groups: list[str], entra_config: dict) -> str:
    """Map Entra ID group memberships to ConnectK roles."""
    admin_groups = entra_config.get("admin_groups", [])
    manager_groups = entra_config.get("manager_groups", [])

    for g in groups:
        if g in admin_groups:
            return "admin"
    for g in groups:
        if g in manager_groups:
            return "manager"
    return "developer"


DEFAULT_PERMISSIONS: dict[str, dict[str, list[str]]] = {
    "admin": {
        "clusters": ["list", "view", "create", "delete"],
        "deployments": ["list", "view", "create", "edit", "delete"],
        "models": ["list", "view", "create", "edit", "delete"],
        "nodes": ["list", "view"],
        "gpus": ["list", "view"],
        "admin": ["list", "view", "edit"],
        "audit": ["list", "view", "export"],
    },
    "manager": {
        "clusters": ["list", "view", "create"],
        "deployments": ["list", "view", "create", "edit", "delete"],
        "models": ["list", "view", "create", "edit"],
        "nodes": ["list", "view"],
        "gpus": ["list", "view"],
        "audit": ["list", "view"],
    },
    "developer": {
        "clusters": ["list", "view"],
        "deployments": ["list", "view", "create", "edit"],
        "models": ["list", "view"],
        "nodes": ["list", "view"],
        "gpus": ["list", "view"],
        "audit": ["view"],
    },
}
