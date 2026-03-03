import uuid
from datetime import datetime

from pydantic import BaseModel


class GroupPermissionResponse(BaseModel):
    id: uuid.UUID
    group_name: str
    page: str
    action: str
    enabled: bool
    updated_by: uuid.UUID | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class GroupPermissionUpdate(BaseModel):
    enabled: bool


class GroupPermissionsMatrix(BaseModel):
    group_name: str
    permissions: dict[str, dict[str, bool]]


class SystemHealth(BaseModel):
    status: str
    database: str
    redis: str
    clusters: list[dict]
    cache_hit_rate: float
    avg_api_response_ms: float
    active_sse_connections: int
    uptime_seconds: float


class AuditLogResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str | None
    action: str
    resource_type: str
    resource_id: uuid.UUID | None
    details: dict | None
    ip_address: str | None
    user_agent: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogQueryParams(BaseModel):
    page: int = 1
    page_size: int = 25
    user_id: uuid.UUID | None = None
    action: str | None = None
    resource_type: str | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None


class ClusterGroupAccessCreate(BaseModel):
    cluster_id: uuid.UUID
    entra_group_id: str
    entra_group_name: str
    access_level: str
