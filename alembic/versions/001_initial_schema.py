"""Initial schema with all ConnectK tables

Revision ID: 001
Revises: 
Create Date: 2026-02-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enums (IF NOT EXISTS prevents failures when multiple pods race)
    op.execute("DO $$ BEGIN CREATE TYPE cloud_provider AS ENUM ('GKE', 'AKS', 'EKS'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE cluster_status AS ENUM ('active', 'unreachable', 'pending'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE gitops_tool AS ENUM ('argocd', 'fluxcd'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE deployment_backend AS ENUM ('sglang', 'vllm', 'trtllm'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE deployment_type_enum AS ENUM ('aggregated', 'aggregated_route', 'disaggregated_route'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("""DO $$ BEGIN CREATE TYPE deployment_status AS ENUM (
        'creating', 'provisioning', 'running', 'updating', 'degraded',
        'failed', 'deleting', 'deleted', 'delete_failed', 'rolling_back'
    ); EXCEPTION WHEN duplicate_object THEN null; END $$""")
    op.execute("DO $$ BEGIN CREATE TYPE model_source_type AS ENUM ('huggingface', 's3', 'gcs', 'azure_blob', 'custom'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE access_level AS ENUM ('list', 'view', 'deploy', 'admin'); EXCEPTION WHEN duplicate_object THEN null; END $$")
    op.execute("""DO $$ BEGIN CREATE TYPE audit_action AS ENUM (
        'create', 'read', 'update', 'delete', 'login', 'logout',
        'permission_change', 'test_connection'
    ); EXCEPTION WHEN duplicate_object THEN null; END $$""")

    # clusters
    op.create_table(
        "clusters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("provider", sa.Enum("GKE", "AKS", "EKS", name="cloud_provider"), nullable=False),
        sa.Column("region", sa.String(100), nullable=False),
        sa.Column("auth_config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("kubeapi_endpoint", sa.String(500), nullable=False),
        sa.Column("k8s_version", sa.String(20)),
        sa.Column("status", sa.Enum("active", "unreachable", "pending", name="cluster_status"), nullable=False, server_default="pending"),
        sa.Column("cache_ttl_seconds", sa.Integer, server_default="300"),
        sa.Column("gitops_tool", sa.Enum("argocd", "fluxcd", name="gitops_tool"), nullable=False),
        sa.Column("gitops_repo_url", sa.String(500), nullable=False),
        sa.Column("gitops_branch", sa.String(100), server_default="main"),
        sa.Column("added_by", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    # models
    op.create_table(
        "models",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("custom_name", sa.String(255)),
        sa.Column("source_type", sa.Enum("huggingface", "s3", "gcs", "azure_blob", "custom", name="model_source_type"), nullable=False),
        sa.Column("source_uri", sa.String(1000), nullable=False),
        sa.Column("architecture", sa.String(100), nullable=False),
        sa.Column("param_count_b", sa.Numeric(10, 2), nullable=False),
        sa.Column("size_fp32_gb", sa.Numeric(10, 2), nullable=False),
        sa.Column("supported_platforms", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("supported_backends", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("tokenizer_path", sa.String(1000)),
        sa.Column("description", sa.Text),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("added_by", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    # deployments
    op.create_table(
        "deployments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clusters.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("namespace", sa.String(255), nullable=False),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("backend", sa.Enum("sglang", "vllm", "trtllm", name="deployment_backend"), nullable=False),
        sa.Column("deployment_type", sa.Enum("aggregated", "aggregated_route", "disaggregated_route", name="deployment_type_enum"), nullable=False),
        sa.Column("replicas", sa.Integer, nullable=False),
        sa.Column("gpu_per_replica", sa.Integer, nullable=False),
        sa.Column("quantization", sa.String(20)),
        sa.Column("kv_cache_gb", sa.Numeric(10, 2)),
        sa.Column("max_batch_size", sa.Integer),
        sa.Column("runtime_optimizations", postgresql.JSONB, server_default="[]"),
        sa.Column("gitops_commit_sha", sa.String(64)),
        sa.Column("status", sa.Enum("creating", "provisioning", "running", "updating", "degraded", "failed", "deleting", "deleted", "delete_failed", "rolling_back", name="deployment_status"), nullable=False, server_default="creating"),
        sa.Column("status_message", sa.Text),
        sa.Column("status_changed_at", sa.DateTime(timezone=True)),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.execute("""
        CREATE UNIQUE INDEX uq_active_deployment_name
        ON deployments (cluster_id, namespace, name)
        WHERE deleted_at IS NULL
    """)

    # audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Enum("create", "read", "update", "delete", "login", "logout", "permission_change", "test_connection", name="audit_action"), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True)),
        sa.Column("details", postgresql.JSONB),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("user_agent", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    # cluster_cache
    op.create_table(
        "cluster_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cluster_id", "resource_type", name="uq_cluster_resource_cache"),
    )
    op.create_index("ix_cluster_cache_expires_at", "cluster_cache", ["expires_at"])

    # group_permissions
    op.create_table(
        "group_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("group_name", sa.String(100), nullable=False),
        sa.Column("page", sa.String(100), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("group_name", "page", "action", name="uq_group_page_action"),
    )

    # cluster_user_access
    op.create_table(
        "cluster_user_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entra_group_id", sa.String(255), nullable=False),
        sa.Column("entra_group_name", sa.String(255), nullable=False),
        sa.Column("access_level", sa.Enum("list", "view", "deploy", "admin", name="access_level"), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("cluster_id", "entra_group_id", name="uq_cluster_group_access"),
    )

    # Seed default group permissions
    _seed_permissions(op)


def _seed_permissions(op) -> None:
    permissions = [
        # Admin: full access
        *[("admin", page, action, True) for page in ["clusters", "deployments", "models", "nodes", "gpus", "admin", "audit"] for action in ["list", "view", "create", "edit", "delete"]],
        # Manager: limited
        ("manager", "clusters", "list", True), ("manager", "clusters", "view", True), ("manager", "clusters", "create", True), ("manager", "clusters", "delete", False),
        ("manager", "deployments", "list", True), ("manager", "deployments", "view", True), ("manager", "deployments", "create", True), ("manager", "deployments", "edit", True), ("manager", "deployments", "delete", True),
        ("manager", "models", "list", True), ("manager", "models", "view", True), ("manager", "models", "create", True), ("manager", "models", "edit", True), ("manager", "models", "delete", False),
        ("manager", "nodes", "list", True), ("manager", "nodes", "view", True),
        ("manager", "gpus", "list", True), ("manager", "gpus", "view", True),
        ("manager", "admin", "list", False), ("manager", "admin", "view", False), ("manager", "admin", "edit", False),
        ("manager", "audit", "list", True), ("manager", "audit", "view", True), ("manager", "audit", "delete", False),
        # Developer: minimal
        ("developer", "clusters", "list", True), ("developer", "clusters", "view", True), ("developer", "clusters", "create", False), ("developer", "clusters", "edit", False), ("developer", "clusters", "delete", False),
        ("developer", "deployments", "list", True), ("developer", "deployments", "view", True), ("developer", "deployments", "create", True), ("developer", "deployments", "edit", True), ("developer", "deployments", "delete", True),
        ("developer", "models", "list", True), ("developer", "models", "view", True), ("developer", "models", "create", False), ("developer", "models", "edit", False), ("developer", "models", "delete", False),
        ("developer", "nodes", "list", True), ("developer", "nodes", "view", True),
        ("developer", "gpus", "list", True), ("developer", "gpus", "view", True),
        ("developer", "admin", "list", False), ("developer", "admin", "view", False), ("developer", "admin", "edit", False),
        ("developer", "audit", "list", False), ("developer", "audit", "view", True), ("developer", "audit", "delete", False),
    ]
    import uuid as _uuid
    from datetime import datetime, timezone
    op.bulk_insert(
        sa.table(
            "group_permissions",
            sa.column("id"), sa.column("group_name"), sa.column("page"), sa.column("action"), sa.column("enabled"),
        ),
        [{"id": str(_uuid.uuid4()), "group_name": g, "page": p, "action": a, "enabled": e} for g, p, a, e in permissions],
    )


def downgrade() -> None:
    op.drop_table("cluster_user_access")
    op.drop_table("group_permissions")
    op.drop_table("cluster_cache")
    op.drop_table("audit_logs")
    op.execute("DROP INDEX IF EXISTS uq_active_deployment_name")
    op.drop_table("deployments")
    op.drop_table("models")
    op.drop_table("clusters")

    op.execute("DROP TYPE IF EXISTS cloud_provider")
    op.execute("DROP TYPE IF EXISTS cluster_status")
    op.execute("DROP TYPE IF EXISTS gitops_tool")
    op.execute("DROP TYPE IF EXISTS deployment_backend")
    op.execute("DROP TYPE IF EXISTS deployment_type_enum")
    op.execute("DROP TYPE IF EXISTS deployment_status")
    op.execute("DROP TYPE IF EXISTS model_source_type")
    op.execute("DROP TYPE IF EXISTS access_level")
    op.execute("DROP TYPE IF EXISTS audit_action")
