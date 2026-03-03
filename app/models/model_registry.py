import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Model(Base):
    __tablename__ = "models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    custom_name: Mapped[str | None] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(
        Enum("huggingface", "s3", "gcs", "azure_blob", "custom", name="model_source_type"),
        nullable=False,
    )
    source_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    architecture: Mapped[str] = mapped_column(String(100), nullable=False)
    param_count_b: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    size_fp32_gb: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    supported_platforms: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    supported_backends: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    tokenizer_path: Mapped[str | None] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    deployments: Mapped[list["Deployment"]] = relationship(  # noqa: F821
        "Deployment", back_populates="model", lazy="select"
    )
