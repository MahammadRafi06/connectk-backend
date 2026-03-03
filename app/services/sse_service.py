"""
Server-Sent Events service for real-time cluster/deployment updates.
Uses Redis Pub/Sub for cross-instance event delivery.
"""
import asyncio
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from app.config import get_settings
from app.redis_client import get_redis

settings = get_settings()

_connections: dict[str, list[asyncio.Queue]] = defaultdict(list)
_stats = {"total_connections": 0}
MAX_CONNECTIONS_PER_USER = 3
_CLOSE_SIGNAL = "__CONNECTK_SSE_CLOSE__"
REDIS_SSE_CHANNEL = "connectk:sse_events"

_event_counter = 0


def _next_event_id() -> str:
    global _event_counter
    _event_counter += 1
    return f"{_event_counter}-{uuid.uuid4().hex[:8]}"


def register_connection(user_id: str) -> asyncio.Queue:
    if len(_connections[user_id]) >= MAX_CONNECTIONS_PER_USER:
        old_queue = _connections[user_id].pop(0)
        try:
            old_queue.put_nowait(_CLOSE_SIGNAL)
        except asyncio.QueueFull:
            pass

    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _connections[user_id].append(q)
    _stats["total_connections"] = sum(len(v) for v in _connections.values())
    return q


def deregister_connection(user_id: str, queue: asyncio.Queue) -> None:
    if user_id in _connections:
        try:
            _connections[user_id].remove(queue)
        except ValueError:
            pass
        if not _connections[user_id]:
            del _connections[user_id]
    _stats["total_connections"] = sum(len(v) for v in _connections.values())


def _dispatch_to_local_queues(
    event_type: str, payload: dict, event_id: str,
    target_user_ids: list[str] | None = None,
) -> None:
    """Push a formatted SSE message to in-memory queues for local connections."""
    data = json.dumps({
        "type": event_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    sse_message = f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"

    users = target_user_ids if target_user_ids else list(_connections.keys())
    for user_id in users:
        for q in list(_connections.get(user_id, [])):
            try:
                q.put_nowait(sse_message)
            except asyncio.QueueFull:
                pass


async def broadcast_event(
    event_type: str,
    payload: dict,
    target_user_ids: list[str] | None = None,
) -> None:
    """Publish event via Redis Pub/Sub so all instances receive it."""
    event_id = _next_event_id()
    message = json.dumps({
        "event_type": event_type,
        "payload": payload,
        "event_id": event_id,
        "target_user_ids": target_user_ids,
    })
    try:
        redis = await get_redis()
        await redis.publish(REDIS_SSE_CHANNEL, message)
    except Exception:
        _dispatch_to_local_queues(event_type, payload, event_id, target_user_ids)


async def broadcast_deployment_status_change(
    deployment_id: str,
    old_status: str,
    new_status: str,
    accessible_by_users: list[str] | None = None,
) -> None:
    await broadcast_event(
        "deployment.status_changed",
        {
            "dep_id": deployment_id,
            "old_status": old_status,
            "new_status": new_status,
        },
        target_user_ids=accessible_by_users,
    )


async def broadcast_cluster_connectivity_change(
    cluster_id: str, status: str
) -> None:
    await broadcast_event(
        "cluster.connectivity_changed",
        {"cluster_id": cluster_id, "status": status},
    )


def get_active_connections_count() -> int:
    return _stats["total_connections"]


async def _redis_subscriber() -> None:
    """Background task: listen on Redis Pub/Sub and dispatch to local queues."""
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(REDIS_SSE_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                _dispatch_to_local_queues(
                    data["event_type"],
                    data["payload"],
                    data["event_id"],
                    data.get("target_user_ids"),
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    except Exception:
        pass

_subscriber_task: asyncio.Task | None = None


def _ensure_subscriber() -> None:
    global _subscriber_task
    if _subscriber_task is None or _subscriber_task.done():
        _subscriber_task = asyncio.create_task(_redis_subscriber())


async def sse_event_generator(user_id: str, last_event_id: str | None = None):
    """Async generator for SSE streaming."""
    _ensure_subscriber()
    queue = register_connection(user_id)
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    queue.get(), timeout=settings.SSE_HEARTBEAT_SECONDS
                )
                if message == _CLOSE_SIGNAL:
                    break
                yield message
            except asyncio.TimeoutError:
                yield ": ping\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        deregister_connection(user_id, queue)
