"""依赖注入：每个请求拿一份 settings、一个 sqlite 连接、共享一个 Kafka 生产者。

sqlite 连接不跨线程复用（``check_same_thread=False`` 只是给 SSE 长连接留路），
写操作都在请求内完成。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Request

from chatbi.mmrag.api.publisher import RequestPublisher
from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.db import connect


@dataclass(frozen=True, slots=True)
class Context:
    settings: MMRagSettings
    conn: sqlite3.Connection


def get_settings(request: Request) -> MMRagSettings:
    settings: MMRagSettings = request.app.state.mmrag
    return settings


def get_publisher(request: Request) -> RequestPublisher:
    publisher: RequestPublisher = request.app.state.publisher
    return publisher


def get_context(request: Request) -> Iterator[Context]:
    settings = get_settings(request)
    conn = connect(settings.paths.db_file)
    try:
        yield Context(settings=settings, conn=conn)
    finally:
        conn.close()


# 供路由直接当类型标注用，省得每处都写 Annotated[...]
Settings = MMRagSettings
Publisher = RequestPublisher
