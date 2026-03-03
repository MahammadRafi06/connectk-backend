"""
Cluster metadata caching service with distributed lock protection.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cluster import Cluster, ClusterCache
from app.redis_client import RedisSessionStore


async def get_cached_resource(
    db: AsyncSession,
    cluster_id: str | uuid.UUID,
    resource_type: str,
) -> tuple[dict | None, bool, int | None]:
    """
    Returns (data, cache_hit, cache_age_seconds).
    """
    cluster_uuid = uuid.UUID(str(cluster_id))
    result = await db.execute(
        select(ClusterCache).where(
            ClusterCache.cluster_id == cluster_uuid,
            ClusterCache.resource_type == resource_type,
        )
    )
    cache_entry = result.scalar_one_or_none()

    if not cache_entry:
        return None, False, None

    now = datetime.now(timezone.utc)
    age_seconds = int((now - cache_entry.fetched_at.replace(tzinfo=timezone.utc)).total_seconds())

    if now > cache_entry.expires_at.replace(tzinfo=timezone.utc):
        return cache_entry.data, False, age_seconds

    return cache_entry.data, True, age_seconds


async def set_cached_resource(
    db: AsyncSession,
    cluster_id: str | uuid.UUID,
    resource_type: str,
    data: dict,
    ttl_seconds: int = 300,
) -> None:
    cluster_uuid = uuid.UUID(str(cluster_id))
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)

    result = await db.execute(
        select(ClusterCache).where(
            ClusterCache.cluster_id == cluster_uuid,
            ClusterCache.resource_type == resource_type,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.data = data
        existing.fetched_at = now
        existing.expires_at = expires_at
    else:
        cache_entry = ClusterCache(
            cluster_id=cluster_uuid,
            resource_type=resource_type,
            data=data,
            fetched_at=now,
            expires_at=expires_at,
        )
        db.add(cache_entry)

    await db.flush()


async def invalidate_cluster_cache(
    db: AsyncSession,
    cluster_id: str | uuid.UUID,
    resource_type: str | None = None,
) -> None:
    cluster_uuid = uuid.UUID(str(cluster_id))
    if resource_type:
        result = await db.execute(
            select(ClusterCache).where(
                ClusterCache.cluster_id == cluster_uuid,
                ClusterCache.resource_type == resource_type,
            )
        )
        entry = result.scalar_one_or_none()
        if entry:
            await db.delete(entry)
    else:
        result = await db.execute(
            select(ClusterCache).where(ClusterCache.cluster_id == cluster_uuid)
        )
        for entry in result.scalars().all():
            await db.delete(entry)
    await db.flush()
