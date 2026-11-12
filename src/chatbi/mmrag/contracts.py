"""Kafka 消息契约。

三条 topic，key 一律用 ``doc_id``：
- ``doc.parse.request``：接口层投递，worker 消费
- ``doc.parse.result`` ：worker 回写，接口层消费后推 SSE
- ``doc.parse.dlq``    ：重试耗尽，人工排查后重投

跨机不传产物本体（Kafka 单条默认上限 1MB）：worker 先把产物 rsync 回本机，
result 里只带路径、页数、耗时。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from chatbi.mmrag.models import utcnow

TOPIC_REQUEST = "doc.parse.request"
TOPIC_RESULT = "doc.parse.result"
TOPIC_DLQ = "doc.parse.dlq"

SCHEMA_VERSION = 1


class ParseRequest(BaseModel):
    """接口层 → worker。``attempt`` 由重投时递增，用于幂等与死信判定。"""

    schema_version: int = SCHEMA_VERSION
    doc_id: str
    filename: str
    raw_path: str
    attempt: int = Field(default=1, ge=1)
    enqueued_at: datetime = Field(default_factory=utcnow)


class ParseResult(BaseModel):
    """worker → 接口层。失败时 ``error`` 必填，``parsed_dir`` 为空。

    与 ``StatusEvent`` 共用 ``doc.parse.result`` topic，靠 ``kind`` 区分：
    worker 一开跑先发 status(parsing)，跑完再发 result(done/failed)。
    这样"解析中"是 worker 真开始才算，不是投递时乐观置位。
    """

    kind: Literal["result"] = "result"
    schema_version: int = SCHEMA_VERSION
    doc_id: str
    ok: bool
    pages: int = 0
    parsed_dir: str | None = None
    markdown_path: str | None = None
    elapsed_ms: int = 0
    attempt: int = 1
    error: str | None = None
    finished_at: datetime = Field(default_factory=utcnow)


class DeadLetter(BaseModel):
    """进 DLQ 时带原始 payload 与失败原因，人工排查后可以原样重投。"""

    schema_version: int = SCHEMA_VERSION
    original_topic: str
    payload: dict[str, object]
    attempts: int
    error: str
    failed_at: datetime = Field(default_factory=utcnow)


class StatusEvent(BaseModel):
    """SSE 推送体，也是 worker 开跑时发的第一条消息（走 result topic）。"""

    kind: Literal["status"] = "status"
    doc_id: str
    status: Literal["pending", "parsing", "done", "failed"]
    pages: int = 0
    error: str | None = None
    at: datetime = Field(default_factory=utcnow)
