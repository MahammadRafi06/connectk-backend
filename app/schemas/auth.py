from datetime import datetime

from pydantic import BaseModel, EmailStr


class UserProfile(BaseModel):
    id: str
    email: str
    name: str
    groups: list[str] = []
    roles: list[str] = []
    connectk_group: str = "developer"
    accessible_cluster_ids: list[str] = []
    permissions: dict[str, list[str]] = {}


class SessionInfo(BaseModel):
    session_id: str
    user_id: str
    user_email: str
    user_name: str
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_activity_at: datetime
    is_current: bool = False


class TokenRefreshRequest(BaseModel):
    pass


class OIDCCallbackParams(BaseModel):
    code: str
    state: str
    session_state: str | None = None
