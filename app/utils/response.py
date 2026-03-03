import math
import uuid
from datetime import datetime, timezone
from typing import Any

from app.schemas.response import ErrorDetail, ErrorResponse, PaginationMeta, RequestMeta, SuccessResponse


def make_meta(cache_hit: bool = False, cache_age_seconds: int | None = None) -> RequestMeta:
    return RequestMeta(
        request_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        cache_hit=cache_hit,
        cache_age_seconds=cache_age_seconds,
    )


def success_response(
    data: Any,
    cache_hit: bool = False,
    cache_age_seconds: int | None = None,
    pagination: PaginationMeta | None = None,
    kpis: dict | None = None,
) -> dict:
    return SuccessResponse(
        data=data,
        meta=make_meta(cache_hit, cache_age_seconds),
        pagination=pagination,
        kpis=kpis,
    ).model_dump(mode="json")


def paginate(
    items: list, page: int, page_size: int, total_items: int
) -> tuple[list, PaginationMeta]:
    total_pages = math.ceil(total_items / page_size) if total_items > 0 else 1
    pagination = PaginationMeta(
        page=page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )
    return items, pagination


def error_response(
    code: str,
    message: str,
    details: str | None = None,
    field: str | None = None,
) -> dict:
    return ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details, field=field),
        meta=make_meta(),
    ).model_dump(mode="json")
