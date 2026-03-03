"""Cluster management endpoints."""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Annotated, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import CurrentUser, require_group, require_permission
from app.middleware.rate_limit import check_rate_limit
from app.models.cluster import Cluster, ClusterUserAccess
from app.models.deployment import Deployment
from app.redis_client import RedisSessionStore, get_redis
from app.schemas.cluster import (
    ClusterCreate,
    ClusterDetailResponse,
    ClusterListKPIs,
    ClusterResponse,
    ClusterTestRequest,
    ClusterTestResponse,
)
from app.services.cache_service import get_cached_resource, set_cached_resource
from app.utils.audit import record_audit
from app.utils.response import error_response, paginate, success_response

router = APIRouter(prefix="/api/clusters", tags=["clusters"])
GENERAL_RATE_LIMIT = 200
WRITE_RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 60


def _cluster_to_response(cluster: Cluster, active_models: int = 0) -> dict:
    return ClusterResponse(
        id=cluster.id,
        name=cluster.name,
        provider=cluster.provider,
        region=cluster.region,
        k8s_version=cluster.k8s_version,
        node_count=0,
        gpu_count=0,
        active_models=active_models,
        utilization_pct=0.0,
        status=cluster.status,
        kubeapi_endpoint=cluster.kubeapi_endpoint,
        gitops_tool=cluster.gitops_tool,
        gitops_repo_url=cluster.gitops_repo_url,
        gitops_branch=cluster.gitops_branch,
        cache_ttl_seconds=cluster.cache_ttl_seconds,
        created_at=cluster.created_at,
        updated_at=cluster.updated_at,
    ).model_dump(mode="json")


async def _probe_cluster_connection(kubeapi_endpoint: str) -> ClusterTestResponse:
    import httpx

    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            resp = await client.get(f"{kubeapi_endpoint}/version")
            if resp.status_code == 200:
                version_data = resp.json()
                return ClusterTestResponse(
                    success=True,
                    k8s_version=version_data.get("gitVersion", "unknown"),
                    node_count=None,
                    message="Connection successful",
                )
    except Exception:
        pass

    return ClusterTestResponse(
        success=False,
        message="Unable to connect to cluster. Verify endpoint and credentials.",
    )


def _can_access_cluster(
    current_user: CurrentUser, cluster_id: uuid.UUID, added_by: uuid.UUID | None = None
) -> bool:
    if current_user.connectk_group == "admin":
        return True
    if added_by and str(added_by) == current_user.id:
        return True
    return str(cluster_id) in (current_user.accessible_cluster_ids or [])


async def _read_through_cluster_cache(
    db: AsyncSession,
    cluster: Cluster,
    resource_type: str,
    producer: Callable[[], Awaitable[dict]],
) -> tuple[dict, bool, int | None]:
    cached_data, cache_hit, cache_age_seconds = await get_cached_resource(db, cluster.id, resource_type)
    if cache_hit and cached_data is not None:
        return cached_data, True, cache_age_seconds

    redis = await get_redis()
    store = RedisSessionStore(redis)
    lock_key = f"cache_refresh:{cluster.id}:{resource_type}"
    lock_acquired = await store.acquire_lock(lock_key, ttl_seconds=30)

    if lock_acquired:
        try:
            fresh_data = await producer()
            await set_cached_resource(db, cluster.id, resource_type, fresh_data, cluster.cache_ttl_seconds)
            return fresh_data, False, 0
        finally:
            await store.release_lock(lock_key)

    # Another worker is refreshing. Serve stale cache if available, otherwise briefly wait and retry cache.
    if cached_data is not None:
        return cached_data, False, cache_age_seconds

    await asyncio.sleep(0.1)
    cached_retry, cache_hit_retry, age_retry = await get_cached_resource(db, cluster.id, resource_type)
    if cached_retry is not None:
        return cached_retry, cache_hit_retry, age_retry

    # Last-resort direct fetch.
    fresh_data = await producer()
    return fresh_data, False, None


@router.get("")
async def list_clusters(
    _perm: Annotated[CurrentUser, Depends(require_permission("clusters", "list"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    provider: str | None = Query(default=None),
    region: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str = Query(default="name"),
    sort_order: str = Query(default="asc"),
):
    """List clusters accessible to current user."""
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    query = select(Cluster)

    if current_user.connectk_group != "admin":
        access_conditions = [Cluster.added_by == uuid.UUID(current_user.id)]
        if current_user.accessible_cluster_ids:
            cluster_uuids = [uuid.UUID(cid) for cid in current_user.accessible_cluster_ids]
            access_conditions.append(Cluster.id.in_(cluster_uuids))
        query = query.where(or_(*access_conditions))

    if provider:
        query = query.where(Cluster.provider == provider)
    if region:
        query = query.where(Cluster.region.ilike(f"%{region}%"))
    if search:
        query = query.where(Cluster.name.ilike(f"%{search}%"))

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    col = getattr(Cluster, sort_by, Cluster.name)
    if sort_order == "desc":
        col = col.desc()
    query = query.order_by(col).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    clusters = result.scalars().all()

    # Count active models per cluster
    dep_result = await db.execute(
        select(Deployment.cluster_id, func.count(Deployment.id))
        .where(Deployment.status == "running", Deployment.deleted_at.is_(None))
        .group_by(Deployment.cluster_id)
    )
    active_models_map = {str(row[0]): row[1] for row in dep_result.all()}

    items = [_cluster_to_response(c, active_models_map.get(str(c.id), 0)) for c in clusters]
    items_list, pagination = paginate(items, page, page_size, total)

    kpis = ClusterListKPIs(
        total_clusters=total,
        total_nodes=0,
        total_gpus=0,
        avg_utilization_pct=0.0,
        active_deployments=sum(active_models_map.values()),
        est_monthly_cost_usd=0.0,
    )

    return JSONResponse(content=success_response(items_list, pagination=pagination, kpis=kpis.model_dump()))


@router.get("/{cluster_id}")
async def get_cluster(
    cluster_id: uuid.UUID,
    _perm: Annotated[CurrentUser, Depends(require_permission("clusters", "view"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Cluster not found"))
    if not _can_access_cluster(current_user, cluster_id, cluster.added_by):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    async def produce() -> dict:
        return {"cluster": _cluster_to_response(cluster)}

    payload, cache_hit, cache_age_seconds = await _read_through_cluster_cache(
        db, cluster, "cluster_detail", produce
    )
    return JSONResponse(
        content=success_response(
            payload["cluster"], cache_hit=cache_hit, cache_age_seconds=cache_age_seconds
        )
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_cluster(
    payload: ClusterCreate,
    current_user: Annotated[CurrentUser, Depends(require_permission("clusters", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Register an existing cluster with ConnectK."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    existing = await db.execute(select(Cluster).where(Cluster.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_response("CLUSTER_DUPLICATE_NAME", "A cluster with this name already exists."),
        )

    cluster = Cluster(
        name=payload.name,
        provider=payload.provider,
        region=payload.region,
        kubeapi_endpoint=payload.kubeapi_endpoint,
        auth_config=payload.auth_config.model_dump(exclude_none=True),
        gitops_tool=payload.gitops_tool,
        gitops_repo_url=payload.gitops_repo_url,
        gitops_branch=payload.gitops_branch,
        cache_ttl_seconds=payload.cache_ttl_seconds,
        status="pending",
        added_by=uuid.UUID(current_user.id),
    )
    db.add(cluster)
    await db.flush()

    await record_audit(
        db, current_user.id, "create", "cluster", cluster.id,
        details={"name": cluster.name, "provider": cluster.provider},
        ip_address=request.client.host if request.client else None,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=success_response(_cluster_to_response(cluster)),
    )


@router.post("/test")
async def test_cluster_connection_unregistered(
    payload: ClusterTestRequest,
    current_user: Annotated[CurrentUser, Depends(require_permission("clusters", "create"))],
    request: Request,
):
    """Test KubeAPI connectivity before registering a cluster."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    result = await _probe_cluster_connection(payload.kubeapi_endpoint)
    return JSONResponse(content=success_response(result.model_dump()))


@router.post("/{cluster_id}/test")
async def test_cluster_connection(
    cluster_id: uuid.UUID,
    payload: ClusterTestRequest,
    current_user: Annotated[CurrentUser, Depends(require_permission("clusters", "create"))],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Test KubeAPI connectivity for a cluster."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    result = await _probe_cluster_connection(payload.kubeapi_endpoint)
    return JSONResponse(content=success_response(result.model_dump()))


@router.delete("/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def dissociate_cluster(
    cluster_id: uuid.UUID,
    current_user: Annotated[CurrentUser, Depends(require_permission("clusters", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Dissociate a cluster from ConnectK (does not delete the cloud cluster)."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Cluster not found"))

    active_deps = await db.execute(
        select(func.count(Deployment.id)).where(
            Deployment.cluster_id == cluster_id,
            Deployment.deleted_at.is_(None),
            Deployment.status.in_(["running", "creating", "updating", "provisioning"]),
        )
    )
    if active_deps.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_response("CLUSTER_HAS_ACTIVE_DEPLOYMENTS", "Cluster has active deployments. Remove them first."),
        )

    await record_audit(db, current_user.id, "delete", "cluster", cluster_id,
                       ip_address=request.client.host if request.client else None)
    await db.delete(cluster)


@router.get("/{cluster_id}/nodes")
async def list_cluster_nodes(
    cluster_id: uuid.UUID,
    _perm: Annotated[CurrentUser, Depends(require_permission("clusters", "view"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all nodes in a cluster (returns cached/mock data)."""
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Cluster not found"))
    if not _can_access_cluster(current_user, cluster_id, cluster.added_by):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    async def produce() -> dict:
        mock_nodes = [
            {
                "id": f"node-{i}",
                "name": f"{cluster.name}-node-{i:02d}",
                "cluster_id": str(cluster_id),
                "status": "Ready",
                "cpu_cores": 32,
                "memory_gb": 128,
                "gpu_count": 8,
                "gpu_model": "NVIDIA A100 80GB",
            }
            for i in range(1, 4)
        ]
        return {"items": mock_nodes, "total_items": len(mock_nodes)}

    payload, cache_hit, cache_age_seconds = await _read_through_cluster_cache(
        db, cluster, "cluster_nodes", produce
    )
    items, pagination = paginate(payload["items"], 1, 25, payload["total_items"])
    return JSONResponse(
        content=success_response(
            items, pagination=pagination, cache_hit=cache_hit, cache_age_seconds=cache_age_seconds
        )
    )


@router.get("/{cluster_id}/gpus")
async def list_cluster_gpus(
    cluster_id: uuid.UUID,
    _perm: Annotated[CurrentUser, Depends(require_permission("clusters", "view"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List GPU resources in a cluster."""
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Cluster not found"))
    if not _can_access_cluster(current_user, cluster_id, cluster.added_by):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    async def produce() -> dict:
        mock_gpus = [
            {
                "id": f"gpu-{i}",
                "node_name": f"{cluster.name}-node-{(i // 8) + 1:02d}",
                "gpu_model": "NVIDIA A100 80GB",
                "vram_gb": 80,
                "utilization_pct": round(45.0 + i * 3.5, 1),
                "status": "Active",
            }
            for i in range(1, 9)
        ]
        return {"items": mock_gpus, "total_items": len(mock_gpus)}

    payload, cache_hit, cache_age_seconds = await _read_through_cluster_cache(
        db, cluster, "cluster_gpus", produce
    )
    items, pagination = paginate(payload["items"], 1, 25, payload["total_items"])
    return JSONResponse(
        content=success_response(
            items, pagination=pagination, cache_hit=cache_hit, cache_age_seconds=cache_age_seconds
        )
    )


@router.get("/{cluster_id}/deployments")
async def list_cluster_deployments(
    cluster_id: uuid.UUID,
    _perm: Annotated[CurrentUser, Depends(require_permission("clusters", "view"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List AI deployments in a specific cluster."""
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    cluster_result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = cluster_result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Cluster not found"))
    if not _can_access_cluster(current_user, cluster_id, cluster.added_by):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    async def produce() -> dict:
        result = await db.execute(
            select(Deployment).where(
                Deployment.cluster_id == cluster_id,
                Deployment.deleted_at.is_(None),
            )
        )
        deployments = result.scalars().all()
        items = [
            {
                "id": str(d.id),
                "name": d.name,
                "namespace": d.namespace,
                "backend": d.backend,
                "replicas": d.replicas,
                "status": d.status,
            }
            for d in deployments
        ]
        return {"items": items, "total_items": len(items)}

    payload, cache_hit, cache_age_seconds = await _read_through_cluster_cache(
        db, cluster, "cluster_deployments", produce
    )
    items_list, pagination = paginate(payload["items"], 1, 25, payload["total_items"])
    return JSONResponse(
        content=success_response(
            items_list, pagination=pagination, cache_hit=cache_hit, cache_age_seconds=cache_age_seconds
        )
    )
