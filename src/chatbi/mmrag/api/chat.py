"""图文问答接口。

    POST /api/mmrag/chat/stream    multipart: question / session_id / image? / files[]?

附件就在这一条请求里一起送（DeepSeek 那种「传几个文件 + 打一句话 → 发送」）：
后端收下 → 落盘 → scp 到实例 → 投 Kafka → 等这些文档解析完 → 检索 → 流式回答。
等待期间持续发 ``waiting`` 帧，前端能显示「正在解析 1/2」，而不是卡住不动。

为什么是 SSE 而不是普通 JSON：检索 + 模型首字要 1–3 秒，加载态有个字往外蹦，
体感差很多。前端用 ``fetch`` 读流（``EventSource`` 不支持 POST 和文件上传）。

事件顺序：
    event: start     {session_id}
    event: attached  {docs:[{doc_id, filename, status}]}     ← 带了附件才有
    event: waiting   {pending, docs:[...]}                   ← 每秒一次，直到解析完
    event: meta      {session_id, citations:[...], hits:n}
    event: delta     {text:"..."}                            ← 多次
    event: done      {blocks:[...], message_id}
    event: error     {message}
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from chatbi.mmrag.api.deps import Context, Publisher, get_context, get_publisher, get_settings
from chatbi.mmrag.api.uploads import ingest_files
from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.db import add_message, get_document, list_messages
from chatbi.mmrag.models import AnswerBlock, DocStatus, Document, Role
from chatbi.mmrag.rag import answer as answer_mod
from chatbi.mmrag.rag import llm as llm_mod
from chatbi.mmrag.rag import prompt as prompt_mod
from chatbi.mmrag.rag.retriever import Hit, search

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mmrag", tags=["mmrag-chat"])

MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 附件要等 MinerU 跑完才能问。20 页的论文大约 1–3 分钟，给 4 分钟上限，
# 超时就先按「已解析的部分」回答，并在前端标一句提示。
PARSE_WAIT_SECONDS = 240
PARSE_POLL_SECONDS = 2.0


def _frame(event: str, payload: object) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def _brief(document: Document) -> dict[str, object]:
    return {
        "doc_id": document.id,
        "filename": document.filename,
        "status": document.status.value,
        "pages": document.pages,
        "error": document.error,
    }


async def _wait_for_parse(ctx: Context, doc_ids: list[str]) -> AsyncIterator[dict[str, object]]:
    """轮询本机库里的状态；每轮吐一帧 waiting，让前端进度条动起来。"""
    pending = list(doc_ids)
    waited = 0.0
    while pending and waited < PARSE_WAIT_SECONDS:
        await asyncio.sleep(PARSE_POLL_SECONDS)
        waited += PARSE_POLL_SECONDS
        for doc_id in list(pending):
            document = get_document(ctx.conn, doc_id)
            if document is None or document.status in {DocStatus.DONE, DocStatus.FAILED}:
                pending.remove(doc_id)
        yield {
            "pending": len(pending),
            "waited": int(waited),
            "docs": [_brief(d) for d in (get_document(ctx.conn, i) for i in doc_ids) if d],
        }


def _save_user_image(settings: MMRagSettings, session_id: str, data: bytes) -> str:
    folder = settings.paths.session_dir(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"q_{uuid.uuid4().hex[:8]}.png"
    target.write_bytes(data)
    return str(target.relative_to(settings.paths.base).as_posix())


def _history(ctx: Context, session_id: str) -> list[dict[str, str]]:
    """带最近几轮纯文本消息当上下文；图片不再回放，模型看图很贵。"""
    messages = list_messages(ctx.conn, session_id)
    recent = [m for m in messages if m.text.strip()][-4:]
    return [{"role": m.role.value, "content": m.text} for m in recent]


def _search(ctx: Context, settings: MMRagSettings, question: str) -> list[Hit]:
    return search(ctx.conn, settings, question)


@router.post("/chat/stream")
async def stream(
    ctx: Annotated[Context, Depends(get_context)],
    settings: Annotated[MMRagSettings, Depends(get_settings)],
    publisher: Annotated[Publisher, Depends(get_publisher)],
    question: Annotated[str, Form()] = "",
    session_id: Annotated[str, Form()] = "",
    image: Annotated[UploadFile | None, File()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> StreamingResponse:
    attachments = [item for item in (files or []) if item.filename]
    text = question.strip()
    if not text and image is None and not attachments:
        raise HTTPException(status_code=400, detail="问题、图片、附件至少要有一个")

    session = session_id.strip() or uuid.uuid4().hex[:12]
    image_rel: str | None = None
    image_abs = None
    if image is not None:
        data = await image.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="图片超过 10MB")
        image_rel = _save_user_image(settings, session, data)
        image_abs = settings.paths.base / image_rel

    # 先把用户消息落库：模型挂了也不能让这一问消失
    add_message(
        ctx.conn,
        message_id=uuid.uuid4().hex[:12],
        session_id=session,
        role=Role.USER,
        text=text,
        image_path=image_rel,
    )

    history = _history(ctx, session)

    async def events() -> AsyncIterator[str]:
        # 先给一帧「流已建立」：检索要十几秒（CPU 编码），别让前端白屏
        yield _frame("start", {"session_id": session})

        failed: list[str] = []
        try:
            if attachments:
                docs = await ingest_files(
                    attachments, ctx=ctx, settings=settings, publisher=publisher
                )
                yield _frame("attached", {"docs": [_brief(d) for d in docs]})
                async for progress in _wait_for_parse(ctx, [d.id for d in docs]):
                    yield _frame("waiting", progress)
                    failed = [
                        str(d["doc_id"])
                        for d in progress["docs"]  # type: ignore[index]
                        if d.get("status") == DocStatus.FAILED.value
                    ]

            hits = await to_thread.run_sync(lambda: _search(ctx, settings, text))
            citations = [c.model_dump(mode="json") for c in answer_mod.citations_from(hits)]
            yield _frame(
                "meta",
                {
                    "session_id": session,
                    "citations": citations,
                    "hits": len(hits),
                    "failed_docs": failed,
                },
            )

            messages = prompt_mod.build_messages(
                settings.llm, text, hits, user_image=image_abs, history=history
            )
            pieces: list[str] = []
            async for piece in llm_mod.stream(messages, settings.llm):
                pieces.append(piece)
                yield _frame("delta", {"text": piece})

            body = "".join(pieces).strip()
            blocks: list[AnswerBlock] = []
            if body:
                from chatbi.mmrag.models import TextBlock

                blocks.append(TextBlock(text=body, citations=answer_mod.citations_from(hits)))
            blocks.extend(answer_mod.image_blocks(hits, limit=settings.llm.max_images))

            message_id = uuid.uuid4().hex[:12]
            add_message(
                ctx.conn,
                message_id=message_id,
                session_id=session,
                role=Role.ASSISTANT,
                text=body,
                blocks=[block.model_dump(mode="json") for block in blocks],
            )
            yield _frame(
                "done",
                {"message_id": message_id, "blocks": [b.model_dump(mode="json") for b in blocks]},
            )
        except llm_mod.LLMError as exc:
            yield _frame("error", {"message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，别让前端一直转圈
            log.exception("chat failed session=%s", session)
            yield _frame("error", {"message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
