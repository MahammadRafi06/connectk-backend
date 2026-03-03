import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class ClusterAuthConfig(BaseModel):
    # EKS
    oidc_provider_url: str | None = None
    iam_role_arn: str | None = None
    # AKS
    service_principal_id: str | None = None
    # GKE
    workload_identity_pool: str | None = None
    gcp_service_account: str | None = None
    # Common
    service_account_token: str | None = None
    ca_certificate: str | None = None


class ClusterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    provider: Literal["GKE", "AKS", "EKS"]
    region: str = Field(..., min_length=1, max_length=100)
    kubeapi_endpoint: str
    auth_config: ClusterAuthConfig
    gitops_tool: Literal["argocd", "fluxcd"]
    gitops_repo_url: str
    gitops_branch: str = "main"
    cache_ttl_seconds: int = Field(default=300, ge=30, le=3600)


class ClusterTestRequest(BaseModel):
    kubeapi_endpoint: str
    provider: Literal["GKE", "AKS", "EKS"]
    auth_config: ClusterAuthConfig


class ClusterTestResponse(BaseModel):
    success: bool
    k8s_version: str | None = None
    node_count: int | None = None
    message: str


class ClusterResponse(BaseModel):
    id: uuid.UUID
    name: str
    provider: str
    region: str
    k8s_version: str | None
    node_count: int = 0
    gpu_count: int = 0
    active_models: int = 0
    utilization_pct: float = 0.0
    status: str
    kubeapi_endpoint: str
    gitops_tool: str
    gitops_repo_url: str
    gitops_branch: str
    cache_ttl_seconds: int
    created_at: datetime
    updated_at: datetime
    last_cache_refresh: datetime | None = None

    model_config = {"from_attributes": True}


class ClusterDetailResponse(ClusterResponse):
    auth_config: dict[str, Any] = {}
    namespaces: list[str] = []


class ClusterListKPIs(BaseModel):
    total_clusters: int
    total_nodes: int
    total_gpus: int
    avg_utilization_pct: float
    active_deployments: int
    est_monthly_cost_usd: float


class ClusterQueryParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    provider: Literal["GKE", "AKS", "EKS"] | None = None
    region: str | None = None
    sort_by: str = "name"
    sort_order: Literal["asc", "desc"] = "asc"
    search: str | None = None
