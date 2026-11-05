"""Thin HTTP boundary; configuration and clients are created only at startup."""

import base64
import binascii
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request

from chatbi.api.langchain_runs import router as langchain_runs_router
from chatbi.api.sse import (
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    ChatStreamingResponse,
    event_frames,
)
from chatbi.fire_risk.bootstrap import open_fire_risk_service
from chatbi.fire_risk.models import (
    RISK_LEVEL_LABELS,
    FireRiskRequest,
    FireRiskResponse,
)
from chatbi.fire_risk.service import FireRiskError, FireRiskService
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
FIRE_RISK_PATH = "/api/v1/fire-risk"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[3]
ServiceFactory = Callable[[Path], AbstractAsyncContextManager[SQLAgentService]]
FireRiskFactory = Callable[[Path], AbstractAsyncContextManager[FireRiskService | None]]


def get_service(request: Request) -> SQLAgentService:
    return request.app.state.sql_agent


def get_fire_risk_service(request: Request) -> FireRiskService | None:
    return request.app.state.fire_risk


def create_langchain_app() -> FastAPI:
    """Uvicorn factory for the native LangChain/LangGraph backend."""
    from chatbi.sql_agent_langchain import open_sql_agent_langchain

    return create_app(service_factory=open_sql_agent_langchain)


def create_app(
    project_root: Path | None = None,
    *,
    service_factory: ServiceFactory = open_sql_agent,
    fire_risk_factory: FireRiskFactory = open_fire_risk_service,
) -> FastAPI:
    """Build the FastAPI application.

    ``service_factory`` selects the model runtime behind every SQL endpoint:

    - ``open_sql_agent`` (default) drives the workflow with the OpenAI Agents SDK.
    - ``open_sql_agent_langchain`` drives native agents in a checkpointed LangGraph.

    Both expose identical endpoints; choose the runtime at startup by passing the
    factory instead of relying on the default:

        from chatbi.sql_agent_langchain import open_sql_agent_langchain
        app = create_app(service_factory=open_sql_agent_langchain)

    ``fire_risk_factory`` wires the optional corridor fire-safety assessor. It
    yields ``None`` when the vision model is unconfigured, leaving the SQL
    endpoints fully functional.

    See ``README.md`` for the two runtimes and their configuration.
    """
    root = project_root.resolve() if project_root is not None else DEFAULT_PROJECT_ROOT

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with service_factory(root) as service, fire_risk_factory(root) as fire_risk:
            app.state.sql_agent = service
            app.state.fire_risk = fire_risk
            try:
                yield
            finally:
                del app.state.sql_agent
                del app.state.fire_risk

    app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
    app.include_router(langchain_runs_router)

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

    @app.post(FIRE_RISK_PATH, response_model=FireRiskResponse)
    async def fire_risk(
        body: FireRiskRequest,
        service: Annotated[FireRiskService | None, Depends(get_fire_risk_service)],
    ) -> FireRiskResponse:
        if service is None:
            raise HTTPException(status_code=503, detail="视觉模型未配置")
        if not body.mime_type.startswith("image/"):
            raise HTTPException(status_code=422, detail="仅支持图片")
        try:
            image_bytes = base64.b64decode(body.image_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(status_code=422, detail="无效的图片数据") from None
        try:
            assessment = await service.assess(image_bytes, body.mime_type)
        except FireRiskError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return FireRiskResponse(
            level=assessment.level,
            level_label=RISK_LEVEL_LABELS[assessment.level],
            reasoning=assessment.reasoning,
            hazards=assessment.hazards,
        )

    return app
