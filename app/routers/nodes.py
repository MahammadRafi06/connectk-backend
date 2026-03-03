"""Node and GPU resource endpoints."""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import CurrentUser, require_permission
from app.models.cluster import Cluster
from app.schemas.node import GPUListKPIs, NodeListKPIs
from app.utils.response import error_response, paginate, success_response

router = APIRouter(tags=["nodes"])

MOCK_NODES = [
    {
        "id": f"node-{i:03d}",
        "name": f"gke-prod-node-{i:02d}",
        "cluster_id": "00000000-0000-0000-0000-000000000001",
        "cluster_name": "gke-prod-us-east1",
        "provider": "GKE",
        "os": "Ubuntu 22.04",
        "kernel_version": "5.15.0-89-generic",
        "architecture": "amd64",
        "instance_type": "a2-highgpu-8g",
        "cpu_cores": 96,
        "cpu_model": "Intel Xeon Platinum 8273CL",
        "memory_gb": 680.0,
        "gpu_count": 8,
        "gpu_model": "NVIDIA A100 80GB",
        "gpu_vram_gb": 80.0,
        "utilization_pct": round(40.0 + i * 5.3, 1),
        "status": "Ready",
        "cost_per_hour": 32.77,
        "kubelet_version": "v1.29.6",
        "internal_ip": f"10.0.{i}.10",
        "labels": {"cloud.google.com/gke-nodepool": "gpu-pool"},
        "taints": [],
    }
    for i in range(1, 7)
]

MOCK_GPUS = [
    {
        "id": f"gpu-{node_idx:02d}-{gpu_idx}",
        "node_name": f"gke-prod-node-{node_idx:02d}",
        "node_id": f"node-{node_idx:03d}",
        "cluster_id": "00000000-0000-0000-0000-000000000001",
        "cluster_name": "gke-prod-us-east1",
        "provider": "GKE",
        "gpu_model": "NVIDIA A100 80GB",
        "vram_gb": 80.0,
        "utilization_pct": round(35.0 + (node_idx * gpu_idx * 2.3) % 60, 1),
        "temperature_c": round(42.0 + gpu_idx * 1.5, 1),
        "power_draw_w": round(210 + gpu_idx * 15.0, 1),
        "assigned_workload": f"llama3-deploy-{gpu_idx}" if gpu_idx % 3 != 0 else None,
        "status": "Active",
    }
    for node_idx in range(1, 4)
    for gpu_idx in range(1, 9)
]


def _is_cluster_accessible(current_user: CurrentUser, cluster_id: str) -> bool:
    if current_user.connectk_group == "admin":
        return True
    return cluster_id in (current_user.accessible_cluster_ids or [])


@router.get("/api/nodes")
async def list_nodes(
    _perm: Annotated[CurrentUser, Depends(require_permission("nodes", "list"))],
    current_user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    cluster_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(default=None),
):
    nodes = [n for n in MOCK_NODES if _is_cluster_accessible(current_user, n["cluster_id"])]
    if cluster_id:
        nodes = [n for n in nodes if n["cluster_id"] == str(cluster_id)]
    if search:
        nodes = [n for n in nodes if search.lower() in n["name"].lower()]

    items, pagination = paginate(nodes, page, page_size, len(nodes))
    kpis = NodeListKPIs(
        total_nodes=len(nodes),
        total_cpus=sum(n["cpu_cores"] for n in nodes),
        total_gpus=sum(n["gpu_count"] for n in nodes),
        avg_utilization_pct=(sum(n["utilization_pct"] for n in nodes) / len(nodes)) if nodes else 0.0,
        nodes_with_gpu=len([n for n in nodes if n["gpu_count"] > 0]),
        est_hourly_cost=sum(n["cost_per_hour"] for n in nodes),
    )
    return JSONResponse(content=success_response(items, pagination=pagination, kpis=kpis.model_dump()))


@router.get("/api/nodes/{node_id}")
async def get_node(
    node_id: str,
    _perm: Annotated[CurrentUser, Depends(require_permission("nodes", "view"))],
    current_user: CurrentUser,
):
    node = next((n for n in MOCK_NODES if n["id"] == node_id), None)
    if not node:
        node = {**MOCK_NODES[0], "id": node_id}
    if not _is_cluster_accessible(current_user, node["cluster_id"]):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))
    detail = {
        **node,
        "hostname": f"node-{node_id}.internal",
        "clock_speed_mhz": 3200.0,
        "numa_topology": "2 nodes",
        "total_memory_gb": node["memory_gb"],
        "allocatable_memory_gb": node["memory_gb"] * 0.95,
        "huge_pages": "1Gi: 32",
        "gpu_driver_version": "535.104.05",
        "cuda_version": "12.2",
        "gpu_utilization_pct": node["utilization_pct"],
        "gpu_temperature_c": 48.2,
        "gpu_power_draw_w": 280.5,
        "containerd_version": "1.7.11",
        "pod_cidr": f"10.244.{node_id.split('-')[-1] if '-' in node_id else '1'}.0/24",
        "external_ip": None,
        "cni_plugin": "Calico",
        "running_pods": 14,
    }
    return JSONResponse(content=success_response(detail))


@router.get("/api/gpus")
async def list_gpus(
    _perm: Annotated[CurrentUser, Depends(require_permission("gpus", "list"))],
    current_user: CurrentUser,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    visible_gpus = [g for g in MOCK_GPUS if _is_cluster_accessible(current_user, g["cluster_id"])]
    items, pagination = paginate(visible_gpus, page, page_size, len(visible_gpus))
    gpu_models = list({g["gpu_model"] for g in visible_gpus})
    kpis = GPUListKPIs(
        total_gpus=len(visible_gpus),
        gpu_models_in_use=len(gpu_models),
        avg_utilization_pct=(sum(g["utilization_pct"] for g in visible_gpus) / len(visible_gpus)) if visible_gpus else 0.0,
        total_vram_tb=sum(g["vram_gb"] for g in visible_gpus) / 1024,
        gpus_available=len([g for g in visible_gpus if not g["assigned_workload"]]),
        est_gpu_cost_per_hour=len(visible_gpus) * 2.5,
    )
    return JSONResponse(content=success_response(items, pagination=pagination, kpis=kpis.model_dump()))
