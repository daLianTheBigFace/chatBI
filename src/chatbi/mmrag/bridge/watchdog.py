"""看门狗：把卡死在「解析中」的文档判成失败。

什么时候需要它：worker 被 OOM 杀掉、实例被回收、Kafka 消息重投不上来。
这些情况下 result 永远不来，文档就永远停在 parsing，前端一直转圈，
用户以为是自己网络的问题。

判据只有一条：``status='parsing'`` 且 ``updated_at`` 超过阈值。
不用心跳——worker 每 30 秒写一次 Redis 看着更准，但要多一层状态，
而且实例挂了以后心跳也会跟着消失，最后还是要靠时间兜底。

跑在本机 bridge 进程里（它是唯一能写 rag.db 的常驻进程），
每次轮询消息前顺手扫一遍，不需要单独的定时器。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import timedelta

from chatbi.mmrag.contracts import StatusEvent
from chatbi.mmrag.db import set_status, stale_parsing
from chatbi.mmrag.models import DocStatus, utcnow

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MINUTES = 30


def sweep(
    conn: sqlite3.Connection,
    *,
    notify: Callable[[StatusEvent], None] | None = None,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> list[str]:
    """扫一遍并返回被判失败的 doc_id；``notify`` 用来推 SSE。"""
    deadline = utcnow() - timedelta(minutes=timeout_minutes)
    expired = stale_parsing(conn, older_than=deadline)
    if not expired:
        return []

    doc_ids: list[str] = []
    for document in expired:
        reason = f"解析超时（超过 {timeout_minutes} 分钟没有结果），可重试"
        set_status(conn, document.id, DocStatus.FAILED, error=reason)
        doc_ids.append(document.id)
        log.warning("watchdog: doc=%s -> failed (%s)", document.id, reason)
        if notify is not None:
            notify(StatusEvent(doc_id=document.id, status="failed", error=reason))
    return doc_ids

