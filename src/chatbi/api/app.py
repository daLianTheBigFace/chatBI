"""Thin HTTP boundary; configuration and clients are created only at startup."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from chatbi.api.sse import (
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    ChatStreamingResponse,
    event_frames,
)
from chatbi.sql_agent.bootstrap import open_sql_agent
from chatbi.sql_agent.events import ChatEvent, ChatRequest
from chatbi.sql_agent.models import SQLAgentRequest, SQLAgentResponse
from chatbi.sql_agent.service import SQLAgentService

APP_TITLE = "ChatBI SQL Agent"
APP_VERSION = "0.1.0"
QUERY_PATH = "/api/v1/query"
HEALTH_PATH = "/health"
CHAT_STREAM_PATH = "/api/v1/chat/stream"
EVENT_SCHEMA_PATH = "/api/v1/chat/events/schema"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[3]
ServiceFactory = Callable[[Path], AbstractAsyncContextManager[SQLAgentService]]


def get_service(request: Request) -> SQLAgentService:
    return request.app.state.sql_agent


def create_app(
    project_root: Path | None = None, *, service_factory: ServiceFactory = open_sql_agent
) -> FastAPI:
    root = project_root.resolve() if project_root is not None else DEFAULT_PROJECT_ROOT

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with service_factory(root) as service:
            app.state.sql_agent = service
            try:
                yield
            finally:
                del app.state.sql_agent

    app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)

    @app.get(HEALTH_PATH)
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.post(QUERY_PATH, response_model=SQLAgentResponse)
    async def query(
        body: SQLAgentRequest, service: Annotated[SQLAgentService, Depends(get_service)]
    ) -> SQLAgentResponse:
        return await service.ask(body)

    @app.get(EVENT_SCHEMA_PATH)
    async def event_schema() -> dict:
        return ChatEvent.model_json_schema()

    @app.post(
        CHAT_STREAM_PATH,
        response_class=ChatStreamingResponse,
        responses={200: {"content": {SSE_MEDIA_TYPE: {"schema": {"type": "string"}}}}},
    )
    async def chat_stream(
        body: ChatRequest, service: Annotated[SQLAgentService, Depends(get_service)]
    ) -> ChatStreamingResponse:
        return ChatStreamingResponse(
            event_frames(service.stream(body)), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
        )

    return app
