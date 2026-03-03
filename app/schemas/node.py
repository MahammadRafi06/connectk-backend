import uuid
from pydantic import BaseModel, Field
from typing import Literal


class NodeResponse(BaseModel):
    id: str
    name: str
    cluster_id: uuid.UUID
    cluster_name: str
    provider: str
    os: str
    kernel_version: str
    architecture: str
    instance_type: str
    cpu_cores: int
    cpu_model: str
    memory_gb: float
    gpu_count: int
    gpu_model: str | None
    gpu_vram_gb: float | None
    utilization_pct: float
    status: str
    cost_per_hour: float
    kubelet_version: str
    internal_ip: str
    labels: dict = {}
    taints: list = []


class NodeDetailResponse(NodeResponse):
    hostname: str
    clock_speed_mhz: float | None
    numa_topology: str | None
    total_memory_gb: float
    allocatable_memory_gb: float
    huge_pages: str | None
    gpu_driver_version: str | None
    cuda_version: str | None
    gpu_utilization_pct: float | None
    gpu_temperature_c: float | None
    gpu_power_draw_w: float | None
    containerd_version: str | None
    pod_cidr: str | None
    external_ip: str | None
    cni_plugin: str | None
    running_pods: int


class NodeListKPIs(BaseModel):
    total_nodes: int
    total_cpus: int
    total_gpus: int
    avg_utilization_pct: float
    nodes_with_gpu: int
    est_hourly_cost: float


class GPUResponse(BaseModel):
    id: str
    node_name: str
    node_id: str
    cluster_id: uuid.UUID
    cluster_name: str
    provider: str
    gpu_model: str
    vram_gb: float
    utilization_pct: float
    temperature_c: float | None
    power_draw_w: float | None
    assigned_workload: str | None
    status: str


class GPUListKPIs(BaseModel):
    total_gpus: int
    gpu_models_in_use: int
    avg_utilization_pct: float
    total_vram_tb: float
    gpus_available: int
    est_gpu_cost_per_hour: float


class NodeQueryParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    cluster_id: uuid.UUID | None = None
    sort_by: str = "name"
    sort_order: Literal["asc", "desc"] = "asc"
    search: str | None = None
