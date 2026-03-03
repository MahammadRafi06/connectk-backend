import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class ModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    custom_name: str | None = Field(default=None, max_length=255)
    source_type: Literal["huggingface", "s3", "gcs", "azure_blob", "custom"]
    source_uri: str = Field(..., min_length=1, max_length=1000)
    architecture: str = Field(..., min_length=1, max_length=100)
    param_count_b: Decimal = Field(..., gt=0)
    size_fp32_gb: Decimal = Field(..., gt=0)
    supported_platforms: list[Literal["cuda", "rocm", "cpu"]] = Field(..., min_length=1)
    supported_backends: list[Literal["sglang", "vllm", "trtllm"]] = Field(..., min_length=1)
    tokenizer_path: str | None = Field(default=None, max_length=1000)
    description: str | None = None


class ModelUpdate(BaseModel):
    custom_name: str | None = None
    architecture: str | None = None
    description: str | None = None
    tokenizer_path: str | None = None
    supported_platforms: list[str] | None = None
    supported_backends: list[str] | None = None


class ModelResponse(BaseModel):
    id: uuid.UUID
    name: str
    custom_name: str | None
    source_type: str
    source_uri: str
    architecture: str
    param_count_b: Decimal
    size_fp32_gb: Decimal
    supported_platforms: list[str]
    supported_backends: list[str]
    tokenizer_path: str | None
    description: str | None
    is_active: bool
    active_deployments: int = 0
    added_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ModelListKPIs(BaseModel):
    total_models: int
    most_deployed_model: str | None
    model_sources: dict[str, int]
    avg_model_size_gb: float


class ModelQueryParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    source_type: str | None = None
    architecture: str | None = None
    search: str | None = None
    sort_by: str = "name"
    sort_order: Literal["asc", "desc"] = "asc"
