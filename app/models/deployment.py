import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

DEPLOYMENT_STATUS_ENUM = Enum(
    "creating",
    "provisioning",
    "running",
    "updating",
    "degraded",
    "failed",
    "deleting",
    "deleted",
    "delete_failed",
    "rolling_back",
    name="deployment_status",
)


class Deployment(Base):
    __tablename__ = "deployments"

    __table_args__ = (
        Index(
            "uq_active_deployment_name",
            "cluster_id", "namespace", "name",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="RESTRICT"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    backend: Mapped[str] = mapped_column(
        Enum("sglang", "vllm", "trtllm", name="deployment_backend"), nullable=False
    )
    deployment_type: Mapped[str] = mapped_column(
        Enum(
            "aggregated",
            "aggregated_route",
            "disaggregated_route",
            name="deployment_type_enum",
        ),
        nullable=False,
    )
    replicas: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_per_replica: Mapped[int] = mapped_column(Integer, nullable=False)
    quantization: Mapped[str | None] = mapped_column(String(20))
    kv_cache_gb: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    max_batch_size: Mapped[int | None] = mapped_column(Integer)
    runtime_optimizations: Mapped[list] = mapped_column(JSONB, default=list)
    gitops_commit_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(DEPLOYMENT_STATUS_ENUM, nullable=False, default="creating")
    status_message: Mapped[str | None] = mapped_column(Text)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cluster: Mapped["Cluster"] = relationship("Cluster", back_populates="deployments")  # noqa: F821
    model: Mapped["Model"] = relationship("Model", back_populates="deployments")  # noqa: F821
