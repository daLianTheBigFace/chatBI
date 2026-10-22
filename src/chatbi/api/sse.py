"""SSE framing and deterministic cleanup on disconnect, including ASGI send errors."""

from collections.abc import AsyncIterator
from contextlib import aclosing

from anyio import CancelScope
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from chatbi.sql_agent.events import ChatEvent

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
KEEPALIVE_FRAME = ": keep-alive\n\n"


def encode_event(event: ChatEvent) -> str:
    return f"id: {event.event_id}\nevent: {event.payload.type}\ndata: {event.model_dump_json()}\n\n"


async def event_frames(events: AsyncIterator[ChatEvent | None]) -> AsyncIterator[str]:
    async with aclosing(events):
        async for event in events:
            yield KEEPALIVE_FRAME if event is None else encode_event(event)


class ChatStreamingResponse(StreamingResponse):
    media_type = SSE_MEDIA_TYPE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette's disconnect task group uses level cancellation; shield cleanup only.
            with CancelScope(shield=True):
                await self.body_iterator.aclose()
