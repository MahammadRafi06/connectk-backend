import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class DeploymentCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    cluster_id: uuid.UUID
    namespace: str = Field(..., min_length=1, max_length=255)
    model_id: uuid.UUID
    backend: Literal["sglang", "vllm", "trtllm"]
    deployment_type: Literal["aggregated", "aggregated_route", "disaggregated_route"]
    replicas: int = Field(..., ge=1)
    gpu_per_replica: int = Field(..., ge=1)
    quantization: Literal["FP16", "INT8", "INT4", "None"] | None = None
    kv_cache_gb: Decimal | None = Field(default=None, ge=0)
    max_batch_size: int | None = Field(default=None, ge=1)
    runtime_optimizations: list[str] = []


class DeploymentUpdate(BaseModel):
    replicas: int | None = Field(default=None, ge=1)
    backend: Literal["sglang", "vllm", "trtllm"] | None = None
    quantization: Literal["FP16", "INT8", "INT4", "None"] | None = None
    max_batch_size: int | None = Field(default=None, ge=1)
    kv_cache_gb: Decimal | None = Field(default=None, ge=0)
    runtime_optimizations: list[str] | None = None


class DeploymentResponse(BaseModel):
    id: uuid.UUID
    name: str
    cluster_id: uuid.UUID
    cluster_name: str = ""
    cluster_provider: str = ""
    cluster_region: str = ""
    namespace: str
    model_id: uuid.UUID
    model_name: str = ""
    backend: str
    deployment_type: str
    replicas: int
    gpu_per_replica: int
    quantization: str | None
    kv_cache_gb: Decimal | None
    max_batch_size: int | None
    runtime_optimizations: list[str]
    gitops_commit_sha: str | None
    status: str
    status_message: str | None
    status_changed_at: datetime | None
    owner_id: uuid.UUID
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    throughput_tps: float | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DeploymentMetrics(BaseModel):
    deployment_id: uuid.UUID
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    ttft_ms: float
    tpot_ms: float
    throughput_tps: float
    requests_per_second: float
    queue_depth: int
    gpu_utilization_pct: float


class DeploymentListKPIs(BaseModel):
    total_deployments: int
    total_models_in_use: int
    top_used_model: str | None
    avg_latency_ms: float
    avg_throughput_tps: float
    est_total_cost_usd: float


class DeploymentQueryParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    cluster_id: uuid.UUID | None = None
    status: str | None = None
    sort_by: str = "created_at"
    sort_order: Literal["asc", "desc"] = "desc"
    search: str | None = None
