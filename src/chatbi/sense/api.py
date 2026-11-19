"""情感流水线的 HTTP 层。

三个接口：
- ``POST /api/sense``：收一句话，返回 request_id 与事件地址（202）
- ``GET /api/sense/{request_id}/events``：SSE，推 judged / delta / done / error
- ``GET /api/sense/sessions``：历史会话列表
- ``GET /api/sense/sessions/{sid}/messages``：某段对话的完整记录

这一层不碰模型，只做三件事：抢会话锁、写用户消息、把 SenseRequest 塞进
stream:sense。判定和生成都在 worker 里，所以这里毫秒级返回。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from chatbi.sense.config import SenseSettings, load_settings
from chatbi.sense.contracts import AcceptedEvent, SenseRequest
from chatbi.sense.history import append, connect, list_sessions
from chatbi.sense.streams import StreamBus

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15.0
MAX_MESSAGES = 500


class SubmitRequest(BaseModel):
    """请求体。session_id 留空就新开一段对话。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


# 依赖必须放模块级：本文件开了 `from __future__ import annotations`，FastAPI 拿字符串
# 去模块 globals 里找类型，写在 create_app 里找不到，会把它们当成普通查询参数
# （实测 `422 loc: ["query", "settings"]`）。
def get_settings(request: Request) -> SenseSettings:
    return request.app.state.sense


def get_bus(request: Request) -> StreamBus:
    return request.app.state.bus


Settings = Annotated[SenseSettings, Depends(get_settings)]
Bus = Annotated[StreamBus, Depends(get_bus)]


def create_app(project_root: Path | None = None) -> FastAPI:
    # parents[3]：api.py → sense → chatbi → src → 项目根。多算一级会跑到 E:\project，
    # 那里没有 .env，于是 Redis 没带密码，报 AuthenticationError（已踩）。
    root = project_root or Path(__file__).resolve().parents[3]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = load_settings(root)
        redis = Redis.from_url(settings.redis.url, decode_responses=True)
        app.state.sense = settings
        app.state.redis = redis
        app.state.bus = StreamBus(redis)
        log.info("sense api up (root=%s)", root)
        try:
            yield
        finally:
            await app.state.bus.close()

    app = FastAPI(title="sense pipeline", lifespan=lifespan)

    @app.get("/health")
    def health(settings: Settings, bus: Bus) -> dict[str, Any]:
        return {"ok": True, "redis": settings.redis.host, "events": True}

    @app.post("/api/sense", status_code=202)
    async def submit(body: SubmitRequest, settings: Settings, bus: Bus) -> JSONResponse:
        session_id = body.session_id or new_session_id()
        request_id = new_request_id()

        # 同一会话串行：上一条还没说完就再问，生成会互相打架。
        # 锁的值写 request_id，回复说完由 reply worker 比对后释放；这里的 TTL 只是
        # 兜底（worker 挂了、消息进了死信，锁也会在 30 秒后自己过期）。
        if not await bus.session_lock(
            settings.redis.lock_key(session_id),
            ttl_seconds=settings.pipeline.session_lock_seconds,
            token=request_id,
        ):
            raise HTTPException(status_code=409, detail="这段对话还有一条在生成，稍等一下")

        _remember(settings, session_id, body.text)
        request = SenseRequest(request_id=request_id, session_id=session_id, text=body.text)
        try:
            await bus.add(settings.redis.sense_stream, request, max_len=settings.redis.max_len)
            await bus.publish(
                settings.redis.events_channel(request_id),
                AcceptedEvent(request_id=request_id, session_id=session_id),
            )
        except Exception:
            # 消息压根没进队列，锁必须马上还回去，否则用户白等 30 秒
            await bus.release_lock(settings.redis.lock_key(session_id), request_id)
            raise
        return JSONResponse(
            status_code=202,
            content={
                "request_id": request_id,
                "session_id": session_id,
                "events": f"/api/sense/{request_id}/events",
            },
        )

    @app.get("/api/sense/{request_id}/events")
    async def events(request_id: str, settings: Settings, bus: Bus) -> StreamingResponse:
        channel = settings.redis.events_channel(request_id)

        async def stream():
            yield ": connected\n\n"
            async for event in bus.subscribe(channel):
                if event.get("type") == "tick":
                    yield ": ping\n\n"
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                name = event.get("type", "message")
                yield f"event: {name}\ndata: {payload}\n\n"
                if event.get("type") in {"done", "error"}:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/sense/sessions")
    def sessions(settings: Settings) -> list[dict[str, Any]]:
        return _with_db(settings, lambda conn: list_sessions(conn, limit=50))

    @app.get("/api/sense/sessions/{session_id}/messages")
    def messages(session_id: str, settings: Settings) -> list[dict[str, Any]]:
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空")

        def query(conn):
            rows = conn.execute(
                "SELECT id, role, text, sense_label, created_at FROM messages"
                " WHERE session_id = ? ORDER BY created_at, rowid LIMIT ?",
                (session_id, MAX_MESSAGES),
            ).fetchall()
            return [dict(row) for row in rows]

        return _with_db(settings, query)

    return app


# ------------------------------------------------------------------ 内部
def _remember(settings: SenseSettings, session_id: str, text: str) -> None:
    """先落用户消息：worker 崩了的时候至少知道用户说过什么。"""
    try:
        conn = connect(settings.db_path)
    except Exception:  # noqa: BLE001 - 落库失败不该让请求失败
        log.warning("user message not persisted", exc_info=True)
        return
    try:
        append(conn, session_id=session_id, role="user", text=text)
    finally:
        conn.close()


def _with_db(settings: SenseSettings, fn):
    conn = connect(settings.db_path)
    try:
        return fn(conn)
    finally:
        conn.close()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("SENSE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        create_app(),
        host=os.environ.get("SENSE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SENSE_PORT", "8800")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
