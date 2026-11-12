"""文档相关接口。

    POST /api/mmrag/docs/upload            多文件上传，逐个落盘 + 投 Kafka，立刻返回 pending
    GET  /api/mmrag/docs                   列表（带状态徽标）
    GET  /api/mmrag/docs/status?ids=a,b    批量补状态（刷新 / 断线重连用）
    GET  /api/mmrag/docs/events            SSE：pending → parsing → done/failed
    GET  /api/mmrag/docs/{doc_id}          单文档详情
    POST /api/mmrag/docs/{doc_id}/reparse  重新投递（attempt +1）

顺序很重要：``/status``、``/events`` 必须声明在 ``/{doc_id}`` 之前，
否则会被当成 doc_id 匹配走。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from chatbi.mmrag.api.deps import (
    Context,
    Publisher,
    get_context,
    get_publisher,
    get_settings,
)
from chatbi.mmrag.api.sse import StatusBus
from chatbi.mmrag.api.uploads import ingest_files
from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.contracts import ParseRequest, StatusEvent
from chatbi.mmrag.db import (
    bump_attempt,
    get_document,
    list_documents,
    set_status,
)
from chatbi.mmrag.models import DocStatus, Document

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mmrag/docs", tags=["mmrag-docs"])

Ctx = Annotated[Context, Depends(get_context)]
SettingsDep = Annotated[MMRagSettings, Depends(get_settings)]
PublisherDep = Annotated[Publisher, Depends(get_publisher)]

HEARTBEAT_SECONDS = 15.0


def _to_event(document: Document) -> StatusEvent:
    return StatusEvent(
        doc_id=document.id,
        status=document.status.value,
        pages=document.pages,
        error=document.error,
    )


@router.post("/upload", response_model=list[Document])
async def upload(
    files: list[UploadFile], ctx: Ctx, settings: SettingsDep, publisher: PublisherDep
) -> list[Document]:
    """落盘 → 建行(pending) → 投 Kafka。一份失败不影响其他份。"""
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")
    return await ingest_files(files, ctx=ctx, settings=settings, publisher=publisher)


@router.get("", response_model=list[Document])
def list_all(ctx: Ctx) -> list[Document]:
    """最近 200 份，新的在前。"""
    return list_documents(ctx.conn, limit=200)


@router.get("/status", response_model=list[StatusEvent])
def batch_status(ids: str, ctx: Ctx) -> list[StatusEvent]:
    """批量补状态：``ids=d1,d2,d3``。

    SSE 断线重连或页面刷新后用它一次补齐，比逐个 GET /{doc_id} 少一半请求。
    """
    wanted = [part.strip() for part in ids.split(",") if part.strip()]
    if not wanted:
        return []
    if len(wanted) > 200:
        raise HTTPException(status_code=400, detail="一次最多查 200 个")
    events: list[StatusEvent] = []
    for doc_id in wanted:
        document = get_document(ctx.conn, doc_id)
        if document is not None:
            events.append(_to_event(document))
    return events


@router.get("/events")
async def events(settings: SettingsDep, ctx: Ctx, doc_id: str | None = None) -> StreamingResponse:
    """SSE：先补一份当前快照，再持续推增量。

    快照很重要——只推增量的话，页面刚打开时所有徽标都是空的，要等下一次事件。
    """
    snapshot = [_to_event(document) for document in list_documents(ctx.conn, limit=200)]
    if doc_id is not None:
        snapshot = [event for event in snapshot if event.doc_id == doc_id]

    async def stream():
        yield "retry: 3000\n\n"
        for event in snapshot:
            yield f"event: status\ndata: {event.model_dump_json()}\n\n"
        bus = StatusBus(settings.redis.url)
        try:
            while True:
                try:
                    iterator = bus.listen(doc_id).__aiter__()
                    while True:
                        try:
                            event = await asyncio.wait_for(
                                iterator.__anext__(), timeout=HEARTBEAT_SECONDS
                            )
                        except TimeoutError:
                            yield ": ping\n\n"  # 注释行保活，浏览器会忽略
                            continue
                        yield f"event: status\ndata: {event.model_dump_json()}\n\n"
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - redis 断开后重建订阅，别把 SSE 打死
                    log.exception("sse listen failed, reconnecting")
                    await asyncio.sleep(1.0)
        finally:
            await bus.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{doc_id}", response_model=Document)
def detail(doc_id: str, ctx: Ctx) -> Document:
    document = get_document(ctx.conn, doc_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return document


@router.post("/{doc_id}/reparse", response_model=Document)
async def reparse(
    doc_id: str, ctx: Ctx, settings: SettingsDep, publisher: PublisherDep
) -> Document:
    """重新投递：parsing 中拒绝（幂等锁还在），其余允许。"""
    document = get_document(ctx.conn, doc_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if document.status is DocStatus.PARSING:
        raise HTTPException(status_code=409, detail="正在解析中，稍后再试")

    raw = settings.paths.raw_pdf(doc_id)
    if not raw.exists():
        raise HTTPException(status_code=409, detail="原始文件丢了，请重新上传")

    set_status(ctx.conn, doc_id, DocStatus.PENDING, error=None)
    attempt = bump_attempt(ctx.conn, doc_id)
    try:
        await publisher.send(
            ParseRequest(
                doc_id=doc_id,
                filename=document.filename,
                raw_path=str(raw),
                attempt=attempt,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("reparse publish failed doc=%s", doc_id)
        set_status(ctx.conn, doc_id, DocStatus.FAILED, error=f"重投失败：{exc}")
        raise HTTPException(status_code=502, detail="投递 Kafka 失败") from exc

    updated = get_document(ctx.conn, doc_id)
    if updated is None:  # pragma: no cover - 刚查过
        raise HTTPException(status_code=404, detail="文档不存在")
    return updated



