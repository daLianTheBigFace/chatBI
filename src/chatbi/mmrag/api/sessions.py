"""会话历史：刷新页面靠它把整段对话（含图片块）重新画出来。

    GET /api/mmrag/sessions                      会话列表（左侧「历史对话」）
    GET /api/mmrag/sessions/{session_id}/messages 某会话的全部消息

assistant 消息带 ``blocks``：文本块 + 图片块（只有 url，没有 base64）。
前端拿到 blocks 直接渲染，不用自己去拼图和引用。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from chatbi.mmrag.api.deps import Context, get_context
from chatbi.mmrag.db import list_messages, list_sessions
from chatbi.mmrag.models import ChatMessage

router = APIRouter(prefix="/api/mmrag/sessions", tags=["mmrag-sessions"])

Ctx = Annotated[Context, Depends(get_context)]

MAX_MESSAGES = 500
MAX_SESSIONS = 50

# 路由顺序：``""`` 和 ``/{session_id}/messages`` 不冲突，但别把 ``/{session_id}``
# 单独放前面，否则 ``/sessions`` 会被当成会话 id。
@router.get("")
def sessions(ctx: Ctx) -> list[dict[str, object]]:
    """会话列表，最近活动在前。标题取该会话第一句用户提问（前端再截断）。"""
    return list_sessions(ctx.conn, limit=MAX_SESSIONS)


@router.get("/{session_id}/messages", response_model=list[ChatMessage])
def messages(session_id: str, ctx: Ctx) -> list[ChatMessage]:
    """按时间正序返回；空会话返回空数组而不是 404，前端不用写分支。"""
    if not session_id.strip():
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    return list_messages(ctx.conn, session_id, limit=MAX_MESSAGES)
