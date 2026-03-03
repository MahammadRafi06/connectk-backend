"""Server-Sent Events endpoint for real-time updates."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.middleware.auth import CurrentUser
from app.services.sse_service import sse_event_generator

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/stream")
async def event_stream(
    request: Request,
    current_user: CurrentUser,
    last_event_id: str | None = Query(default=None),
):
    """SSE endpoint for real-time deployment status and cluster health updates."""
    event_id = last_event_id or request.headers.get("Last-Event-ID")

    async def event_generator():
        async for message in sse_event_generator(current_user.id, event_id):
            if await request.is_disconnected():
                break
            yield message

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
