"""接口层往 Kafka 投递（同步生产者）。

upload 是 ``async def``，但 kafka-python 只有同步客户端。这里用
``anyio.to_thread`` 把它扔到线程池，别把事件循环卡住——一次投递要等 ack，
直接 ``await`` 会让整个进程停几十毫秒。
"""

from __future__ import annotations

import logging

from anyio import to_thread
from kafka import KafkaProducer
from kafka.errors import KafkaError

from chatbi.mmrag.config import KafkaSettings
from chatbi.mmrag.contracts import ParseRequest

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """投递失败：文档已经落盘但消息没进去，前端要看到原因。"""


class RequestPublisher:
    """生产者常驻，进程内复用一个连接。"""

    def __init__(self, settings: KafkaSettings, *, timeout: float = 10.0) -> None:
        self._settings = settings
        self._timeout = timeout
        self._producer: KafkaProducer | None = None

    def _ensure(self) -> KafkaProducer:
        if self._producer is None:
            self._producer = KafkaProducer(
                bootstrap_servers=self._settings.bootstrap_servers,
                acks="all",
                retries=3,
                linger_ms=0,
            )
        return self._producer

    def _send(self, request: ParseRequest) -> None:
        try:
            future = self._ensure().send(
                self._settings.request_topic,
                key=request.doc_id.encode("utf-8"),
                value=request.model_dump_json().encode("utf-8"),
            )
            meta = future.get(timeout=self._timeout)
            self._ensure().flush(timeout=self._timeout)
        except KafkaError as exc:
            raise PublishError(f"投递 Kafka 失败：{type(exc).__name__}: {exc}") from exc
        log.info(
            "published doc=%s partition=%s offset=%s", request.doc_id, meta.partition, meta.offset
        )

    async def send(self, request: ParseRequest) -> None:
        await to_thread.run_sync(self._send, request)

    def close(self) -> None:
        if self._producer is not None:
            self._producer.close(timeout=self._timeout)
            self._producer = None
