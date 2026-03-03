import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(
        Enum("GKE", "AKS", "EKS", name="cloud_provider"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    auth_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    kubeapi_endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    k8s_version: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(
        Enum("active", "unreachable", "pending", name="cluster_status"),
        nullable=False,
        default="pending",
    )
    cache_ttl_seconds: Mapped[int] = mapped_column(Integer, default=300)
    gitops_tool: Mapped[str] = mapped_column(
        Enum("argocd", "fluxcd", name="gitops_tool"), nullable=False
    )
    gitops_repo_url: Mapped[str] = mapped_column(String(500), nullable=False)
    gitops_branch: Mapped[str] = mapped_column(String(100), default="main")
    added_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    deployments: Mapped[list["Deployment"]] = relationship(  # noqa: F821
        "Deployment", back_populates="cluster", lazy="select"
    )
    cache_entries: Mapped[list["ClusterCache"]] = relationship(
        "ClusterCache", back_populates="cluster", cascade="all, delete-orphan"
    )
    user_access: Mapped[list["ClusterUserAccess"]] = relationship(
        "ClusterUserAccess", back_populates="cluster", cascade="all, delete-orphan"
    )


class ClusterCache(Base):
    __tablename__ = "cluster_cache"

    __table_args__ = (
        UniqueConstraint("cluster_id", "resource_type", name="uq_cluster_resource_cache"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    cluster: Mapped["Cluster"] = relationship("Cluster", back_populates="cache_entries")


class ClusterUserAccess(Base):
    __tablename__ = "cluster_user_access"

    __table_args__ = (
        UniqueConstraint("cluster_id", "entra_group_id", name="uq_cluster_group_access"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False
    )
    entra_group_id: Mapped[str] = mapped_column(String(255), nullable=False)
    entra_group_name: Mapped[str] = mapped_column(String(255), nullable=False)
    access_level: Mapped[str] = mapped_column(
        Enum("list", "view", "deploy", "admin", name="access_level"), nullable=False
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    cluster: Mapped["Cluster"] = relationship("Cluster", back_populates="user_access")
