"""Model Registry endpoints."""
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import CurrentUser, require_permission
from app.middleware.rate_limit import check_rate_limit
from app.models.deployment import Deployment
from app.models.model_registry import Model
from app.schemas.model import (
    ModelCreate,
    ModelListKPIs,
    ModelResponse,
    ModelUpdate,
)
from app.utils.audit import record_audit
from app.utils.response import error_response, paginate, success_response

router = APIRouter(prefix="/api/models", tags=["models"])
GENERAL_RATE_LIMIT = 200
WRITE_RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 60


def _model_to_response(model: Model, active_deployments: int = 0) -> dict:
    return ModelResponse(
        id=model.id,
        name=model.name,
        custom_name=model.custom_name,
        source_type=model.source_type,
        source_uri=model.source_uri,
        architecture=model.architecture,
        param_count_b=model.param_count_b,
        size_fp32_gb=model.size_fp32_gb,
        supported_platforms=model.supported_platforms,
        supported_backends=model.supported_backends,
        tokenizer_path=model.tokenizer_path,
        description=model.description,
        is_active=model.is_active,
        active_deployments=active_deployments,
        added_by=model.added_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    ).model_dump(mode="json")


@router.get("")
async def list_models(
    _perm: Annotated[CurrentUser, Depends(require_permission("models", "list"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    search: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    sort_by: str = Query(default="name"),
    sort_order: str = Query(default="asc"),
):
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    query = select(Model).where(Model.is_active == True)

    if search:
        query = query.where(Model.name.ilike(f"%{search}%"))
    if source_type:
        query = query.where(Model.source_type == source_type)

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    col = getattr(Model, sort_by, Model.name)
    if sort_order == "desc":
        col = col.desc()
    query = query.order_by(col).offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    models = result.scalars().all()

    # Count active deployments per model
    dep_result = await db.execute(
        select(Deployment.model_id, func.count(Deployment.id))
        .where(Deployment.status == "running", Deployment.deleted_at.is_(None))
        .group_by(Deployment.model_id)
    )
    dep_count_map = {str(row[0]): row[1] for row in dep_result.all()}

    items = [_model_to_response(m, dep_count_map.get(str(m.id), 0)) for m in models]
    items_list, pagination = paginate(items, page, page_size, total)

    source_counts: dict = {}
    for m in models:
        source_counts[m.source_type] = source_counts.get(m.source_type, 0) + 1

    kpis = ModelListKPIs(
        total_models=total,
        most_deployed_model=None,
        model_sources=source_counts,
        avg_model_size_gb=0.0,
    )
    return JSONResponse(content=success_response(items_list, pagination=pagination, kpis=kpis.model_dump()))


@router.get("/{model_id}")
async def get_model(
    model_id: uuid.UUID,
    _perm: Annotated[CurrentUser, Depends(require_permission("models", "view"))],
    current_user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await check_rate_limit(request, current_user.id, GENERAL_RATE_LIMIT, RATE_WINDOW_SECONDS, "general")
    result = await db.execute(select(Model).where(Model.id == model_id, Model.is_active == True))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Model not found"))
    return JSONResponse(content=success_response(_model_to_response(model)))


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_model(
    payload: ModelCreate,
    current_user: Annotated[CurrentUser, Depends(require_permission("models", "create"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Register a new model, validate source URI."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    existing = await db.execute(select(Model).where(Model.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_response("MODEL_DUPLICATE_NAME", "A model with this name already exists."),
        )

    # Validate source URI (HEAD request)
    if payload.source_type in ("s3", "gcs", "azure_blob", "custom"):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.head(payload.source_uri)
                if resp.status_code >= 400:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=error_response("MODEL_SOURCE_UNREACHABLE", "Unable to validate model source URI."),
                    )
        except httpx.RequestError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error_response("MODEL_SOURCE_UNREACHABLE", "Unable to reach model source URI."),
            )

    model = Model(
        name=payload.name,
        custom_name=payload.custom_name,
        source_type=payload.source_type,
        source_uri=payload.source_uri,
        architecture=payload.architecture,
        param_count_b=payload.param_count_b,
        size_fp32_gb=payload.size_fp32_gb,
        supported_platforms=payload.supported_platforms,
        supported_backends=payload.supported_backends,
        tokenizer_path=payload.tokenizer_path,
        description=payload.description,
        is_active=True,
        added_by=uuid.UUID(current_user.id),
    )
    db.add(model)
    await db.flush()

    await record_audit(
        db, current_user.id, "create", "model", model.id,
        details={"name": model.name},
        ip_address=request.client.host if request.client else None,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=success_response(_model_to_response(model)),
    )


@router.put("/{model_id}")
async def update_model(
    model_id: uuid.UUID,
    payload: ModelUpdate,
    current_user: Annotated[CurrentUser, Depends(require_permission("models", "edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    result = await db.execute(select(Model).where(Model.id == model_id, Model.is_active == True))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Model not found"))

    if payload.custom_name is not None:
        model.custom_name = payload.custom_name
    if payload.architecture is not None:
        model.architecture = payload.architecture
    if payload.description is not None:
        model.description = payload.description
    if payload.tokenizer_path is not None:
        model.tokenizer_path = payload.tokenizer_path
    if payload.supported_platforms is not None:
        model.supported_platforms = payload.supported_platforms
    if payload.supported_backends is not None:
        model.supported_backends = payload.supported_backends

    await record_audit(
        db, current_user.id, "update", "model", model_id,
        details=payload.model_dump(exclude_none=True),
        ip_address=request.client.host if request.client else None,
    )
    return JSONResponse(content=success_response(_model_to_response(model)))


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    model_id: uuid.UUID,
    current_user: Annotated[CurrentUser, Depends(require_permission("models", "delete"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
):
    """Soft-delete a model. Blocked if active deployments exist."""
    await check_rate_limit(request, current_user.id, WRITE_RATE_LIMIT, RATE_WINDOW_SECONDS, "write")
    result = await db.execute(select(Model).where(Model.id == model_id, Model.is_active == True))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail=error_response("NOT_FOUND", "Model not found"))

    active_deps = await db.execute(
        select(func.count(Deployment.id)).where(
            Deployment.model_id == model_id,
            Deployment.status.in_(["running", "creating", "updating", "provisioning"]),
            Deployment.deleted_at.is_(None),
        )
    )
    if active_deps.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_response("MODEL_HAS_ACTIVE_DEPLOYMENTS", "Model has active deployments. Remove them first."),
        )

    model.is_active = False
    await record_audit(
        db, current_user.id, "delete", "model", model_id,
        ip_address=request.client.host if request.client else None,
    )
