"""Deployment management endpoints with GitOps write path."""
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import CurrentUser, require_permission
from app.middleware.rate_limit import check_rate_limit
from app.models.cluster import Cluster
from app.models.deployment import Deployment
from app.models.model_registry import Model
from app.schemas.deployment import (
    DeploymentCreate,
    DeploymentListKPIs,
    DeploymentResponse,
    DeploymentUpdate,
)
from app.services.gitops_service import GitOpsService, _render_deployment_manifest
from app.services.sse_service import broadcast_deployment_status_change
from app.services.cache_service import invalidate_cluster_cache
from app.utils.audit import record_audit
from app.utils.response import error_response, paginate, success_response

router = APIRouter(prefix="/api/deployments", tags=["deployments"])
GENERAL_RATE_LIMIT = 200
WRITE_RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 60


def _dep_to_response(dep: Deployment, cluster: Cluster | None = None, model: Model | None = None) -> dict:
    return DeploymentResponse(
        id=dep.id,
        name=dep.name,
        cluster_id=dep.cluster_id,
        cluster_name=cluster.name if cluster else "",
        cluster_provider=cluster.provider if cluster else "",
        cluster_region=cluster.region if cluster else "",
        namespace=dep.namespace,
        model_id=dep.model_id,
        model_name=model.name if model else "",
        backend=dep.backend,
        deployment_type=dep.deployment_type,
        replicas=dep.replicas,
        gpu_per_replica=dep.gpu_per_replica,
        quantization=dep.quantization,
        kv_cache_gb=dep.kv_cache_gb,
        max_batch_size=dep.max_batch_size,
        runtime_optimizations=dep.runtime_optimizations or [],
        gitops_commit_sha=dep.gitops_commit_sha,
        status=dep.status,
        status_message=dep.status_message,
        status_changed_at=dep.status_changed_at,
        owner_id=dep.owner_id,
        created_at=dep.created_at,
        updated_at=dep.updated_at,
    ).model_dump(mode="json")


def _cluster_access_filter(current_user: CurrentUser) -> list[uuid.UUID]:
    return [uuid.UUID(cid) for cid in (current_user.accessible_cluster_ids or [])]


def _can_access_cluster(current_user: CurrentUser, cluster_id: uuid.UUID) -> bool:
    if current_user.connectk_group == "admin":
        return True
    return str(cluster_id) in (current_user.accessible_cluster_ids or [])


@router.get("")
async def list_deployments(
    _perm: Annotated[CurrentUser, Depends(require_permission("deployments", "list"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    cluster_id: uuid.UUID | None = Query(default=None),
    dep_status: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
):
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    query = select(Deployment).where(Deployment.deleted_at.is_(None))

    if cluster_id:
        query = query.where(Deployment.cluster_id == cluster_id)
    if dep_status:
        query = query.where(Deployment.status == dep_status)
    if search:
        query = query.where(Deployment.name.ilike(f"%{search}%"))

    if current_user.connectk_group != "admin":
        accessible = _cluster_access_filter(current_user)
        if not accessible:
            items, pagination = paginate([], page, page_size, 0)
            kpis = DeploymentListKPIs(
                total_deployments=0,
                total_models_in_use=0,
                top_used_model=None,
                avg_latency_ms=0.0,
                avg_throughput_tps=0.0,
                est_total_cost_usd=0.0,
            )
            return JSONResponse(content=success_response(items, pagination=pagination, kpis=kpis.model_dump()))
        query = query.where(Deployment.cluster_id.in_(accessible))

    # Non-admins can only see their own + running deployments
    if current_user.connectk_group == "developer":
        query = query.where(
            (Deployment.owner_id == uuid.UUID(current_user.id)) |
            (Deployment.status == "running")
        )

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    col = getattr(Deployment, sort_by, Deployment.created_at)
    if sort_order == "desc":
        col = col.desc()
    query = query.order_by(col).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    deployments = result.scalars().all()

    # Load related cluster/model data
    cluster_ids = list({d.cluster_id for d in deployments})
    model_ids = list({d.model_id for d in deployments})

    clusters_result = await db.execute(select(Cluster).where(Cluster.id.in_(cluster_ids)))
    clusters_map = {c.id: c for c in clusters_result.scalars().all()}

    models_result = await db.execute(select(Model).where(Model.id.in_(model_ids)))
    models_map = {m.id: m for m in models_result.scalars().all()}

    items = [_dep_to_response(d, clusters_map.get(d.cluster_id), models_map.get(d.model_id)) for d in deployments]
    items_list, pagination = paginate(items, page, page_size, total)

    active_count = await db.execute(
        select(func.count(Deployment.id)).where(Deployment.status == "running", Deployment.deleted_at.is_(None))
    )

    kpis = DeploymentListKPIs(
        total_deployments=total,
        total_models_in_use=len({d.model_id for d in deployments}),
        top_used_model=None,
        avg_latency_ms=0.0,
        avg_throughput_tps=0.0,
        est_total_cost_usd=0.0,
    )
    return JSONResponse(content=success_response(items_list, pagination=pagination, kpis=kpis.model_dump()))


@router.get("/{dep_id}")
async def get_deployment(
    dep_id: uuid.UUID,
    _perm: Annotated[CurrentUser, Depends(require_permission("deployments", "view"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    result = await db.execute(
        select(Deployment).where(Deployment.id == dep_id, Deployment.deleted_at.is_(None))
    )
    dep = result.scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Deployment not found"))
    if not _can_access_cluster(current_user, dep.cluster_id):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    cluster_result = await db.execute(select(Cluster).where(Cluster.id == dep.cluster_id))
    cluster = cluster_result.scalar_one_or_none()

    model_result = await db.execute(select(Model).where(Model.id == dep.model_id))
    model = model_result.scalar_one_or_none()

    return JSONResponse(content=success_response(_dep_to_response(dep, cluster, model)))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_deployment(
    payload: DeploymentCreate,
    _perm: Annotated[CurrentUser, Depends(require_permission("deployments", "create"))],
    current_user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Create deployment via GitOps commit."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    if not _can_access_cluster(current_user, payload.cluster_id):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    # Check for duplicate name in cluster/namespace
    existing = await db.execute(
        select(Deployment).where(
            Deployment.cluster_id == payload.cluster_id,
            Deployment.namespace == payload.namespace,
            Deployment.name == payload.name,
            Deployment.deleted_at.is_(None),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_response("DEPLOYMENT_DUPLICATE_NAME", "A deployment with this name already exists in the cluster."),
        )

    # Verify cluster and model exist
    cluster_result = await db.execute(select(Cluster).where(Cluster.id == payload.cluster_id))
    cluster = cluster_result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Cluster not found"))

    model_result = await db.execute(select(Model).where(Model.id == payload.model_id, Model.is_active == True))
    model = model_result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Model not found"))

    if payload.backend not in (model.supported_backends or []):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_response(
                "VALIDATION_ERROR",
                f"Backend '{payload.backend}' is not supported by model '{model.name}'. "
                f"Supported backends: {', '.join(model.supported_backends)}.",
            ),
        )

    dep = Deployment(
        name=payload.name,
        cluster_id=payload.cluster_id,
        namespace=payload.namespace,
        model_id=payload.model_id,
        backend=payload.backend,
        deployment_type=payload.deployment_type,
        replicas=payload.replicas,
        gpu_per_replica=payload.gpu_per_replica,
        quantization=payload.quantization,
        kv_cache_gb=payload.kv_cache_gb,
        max_batch_size=payload.max_batch_size,
        runtime_optimizations=payload.runtime_optimizations,
        status="creating",
        status_changed_at=datetime.now(timezone.utc),
        owner_id=uuid.UUID(current_user.id),
    )
    db.add(dep)
    await db.flush()

    # Generate manifest and commit to GitOps
    manifest = _render_deployment_manifest(
        deployment_name=payload.name,
        namespace=payload.namespace,
        model_name=model.name,
        backend=payload.backend,
        replicas=payload.replicas,
        gpu_per_replica=payload.gpu_per_replica,
        quantization=payload.quantization,
        kv_cache_gb=float(payload.kv_cache_gb) if payload.kv_cache_gb else None,
        max_batch_size=payload.max_batch_size,
        runtime_optimizations=payload.runtime_optimizations,
        deployment_id=str(dep.id),
        owner_email=current_user.email,
        model_id=str(payload.model_id),
    )

    try:
        gitops = GitOpsService(
            repo_url=cluster.gitops_repo_url,
            branch=cluster.gitops_branch,
        )
        commit_sha = await gitops.commit_deployment(
            cluster_name=cluster.name,
            namespace=payload.namespace,
            deployment_name=payload.name,
            manifest_content=manifest,
            action="create",
            user_email=current_user.email,
        )
        dep.gitops_commit_sha = commit_sha
    except Exception as e:
        dep.status = "failed"
        dep.status_message = f"GitOps commit failed: {e}"

    await record_audit(
        db, current_user.id, "create", "deployment", dep.id,
        details={"name": dep.name, "cluster": str(payload.cluster_id)},
        ip_address=request.client.host if request.client else None,
    )
    await invalidate_cluster_cache(db, dep.cluster_id, "cluster_deployments")
    await broadcast_deployment_status_change(str(dep.id), "pending", dep.status)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=success_response(_dep_to_response(dep, cluster, model)),
    )


@router.put("/{dep_id}")
async def update_deployment(
    dep_id: uuid.UUID,
    payload: DeploymentUpdate,
    _perm: Annotated[CurrentUser, Depends(require_permission("deployments", "edit"))],
    current_user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Edit deployment via GitOps commit. Non-editable fields are enforced."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    result = await db.execute(
        select(Deployment).where(Deployment.id == dep_id, Deployment.deleted_at.is_(None))
    )
    dep = result.scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Deployment not found"))
    if not _can_access_cluster(current_user, dep.cluster_id):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    # Permission: developer can only edit own deployments
    if current_user.connectk_group == "developer" and str(dep.owner_id) != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "You can only edit your own deployments."),
        )

    old_state = {
        "replicas": dep.replicas, "backend": dep.backend, "quantization": dep.quantization
    }

    if payload.replicas is not None:
        dep.replicas = payload.replicas
    if payload.backend is not None:
        dep.backend = payload.backend
    if payload.quantization is not None:
        dep.quantization = payload.quantization
    if payload.max_batch_size is not None:
        dep.max_batch_size = payload.max_batch_size
    if payload.kv_cache_gb is not None:
        dep.kv_cache_gb = payload.kv_cache_gb
    if payload.runtime_optimizations is not None:
        dep.runtime_optimizations = payload.runtime_optimizations

    old_status = dep.status
    dep.status = "updating"
    dep.status_changed_at = datetime.now(timezone.utc)

    await record_audit(
        db, current_user.id, "update", "deployment", dep_id,
        details={"before": old_state, "after": payload.model_dump(exclude_none=True)},
        ip_address=request.client.host if request.client else None,
    )

    cluster_result = await db.execute(select(Cluster).where(Cluster.id == dep.cluster_id))
    cluster = cluster_result.scalar_one_or_none()
    model_result = await db.execute(select(Model).where(Model.id == dep.model_id))
    model = model_result.scalar_one_or_none()

    if cluster and model:
        manifest = _render_deployment_manifest(
            deployment_name=dep.name,
            namespace=dep.namespace,
            model_name=model.name,
            backend=dep.backend,
            replicas=dep.replicas,
            gpu_per_replica=dep.gpu_per_replica,
            quantization=dep.quantization,
            kv_cache_gb=float(dep.kv_cache_gb) if dep.kv_cache_gb is not None else None,
            max_batch_size=dep.max_batch_size,
            runtime_optimizations=dep.runtime_optimizations or [],
            deployment_id=str(dep.id),
            owner_email=current_user.email,
            model_id=str(dep.model_id),
        )
        try:
            gitops = GitOpsService(
                repo_url=cluster.gitops_repo_url,
                branch=cluster.gitops_branch,
            )
            commit_sha = await gitops.commit_deployment(
                cluster_name=cluster.name,
                namespace=dep.namespace,
                deployment_name=dep.name,
                manifest_content=manifest,
                action="update",
                user_email=current_user.email,
            )
            dep.gitops_commit_sha = commit_sha
        except Exception as e:
            dep.status = "failed"
            dep.status_message = f"GitOps update failed: {e}"

    await invalidate_cluster_cache(db, dep.cluster_id, "cluster_deployments")
    await broadcast_deployment_status_change(str(dep.id), old_status, dep.status)

    return JSONResponse(content=success_response(_dep_to_response(dep, cluster, model)))


@router.delete("/{dep_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deployment(
    dep_id: uuid.UUID,
    current_user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Delete deployment via GitOps commit."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    result = await db.execute(
        select(Deployment).where(Deployment.id == dep_id, Deployment.deleted_at.is_(None))
    )
    dep = result.scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Deployment not found"))
    if not _can_access_cluster(current_user, dep.cluster_id):
        raise HTTPException(status_code=403, detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "Access denied"))

    has_delete_perm = "delete" in current_user.permissions.get("deployments", [])
    is_owner = str(dep.owner_id) == current_user.id

    if current_user.connectk_group == "admin":
        pass
    elif has_delete_perm:
        pass
    elif is_owner:
        pass
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_response("AUTH_INSUFFICIENT_PERMISSION", "You do not have permission to delete this deployment."),
        )

    old_status = dep.status
    dep.status = "deleting"
    dep.status_changed_at = datetime.now(timezone.utc)
    dep.deleted_at = datetime.now(timezone.utc)

    cluster_result = await db.execute(select(Cluster).where(Cluster.id == dep.cluster_id))
    cluster = cluster_result.scalar_one_or_none()

    if cluster:
        try:
            gitops = GitOpsService(repo_url=cluster.gitops_repo_url, branch=cluster.gitops_branch)
            await gitops.commit_deployment(
                cluster_name=cluster.name,
                namespace=dep.namespace,
                deployment_name=dep.name,
                manifest_content="",
                action="delete",
                user_email=current_user.email,
            )
        except Exception:
            pass

    await record_audit(
        db, current_user.id, "delete", "deployment", dep_id,
        ip_address=request.client.host if request.client else None,
    )
    await invalidate_cluster_cache(db, dep.cluster_id, "cluster_deployments")
    await broadcast_deployment_status_change(str(dep.id), old_status, dep.status)
