from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class RequestMeta(BaseModel):
    request_id: str
    timestamp: datetime
    cache_hit: bool = False
    cache_age_seconds: int | None = None


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_previous: bool


class SuccessResponse(BaseModel, Generic[T]):
    status: str = "success"
    data: T
    meta: RequestMeta
    pagination: PaginationMeta | None = None
    kpis: dict[str, Any] | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: str | None = None
    field: str | None = None


class ErrorResponse(BaseModel):
    status: str = "error"
    error: ErrorDetail
    meta: RequestMeta
