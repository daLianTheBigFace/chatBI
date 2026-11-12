"""worker 侧 Kafka 生产者：回写 result、失败进 DLQ。

所有发送都同步等 ack（``acks='all'``），worker 是短生命周期进程，不能靠攒批：
result 一旦丢了，文档就永远卡在 parsing，只能等看门狗兜底。

key/value 一律自己 encode 成 bytes：kafka-python 3.x 传入裸函数会报
``DeprecationWarning: ... does not implement kafka.serializer.Serializer``。
"""

from __future__ import annotations

from typing import Any

from kafka import KafkaProducer
from kafka.producer.future import FutureRecordMetadata

from chatbi.mmrag.config import KafkaSettings
from chatbi.mmrag.contracts import DeadLetter, ParseResult, StatusEvent


class Reporter:
    """发送成功才算数：每个 ``send`` 都 ``.get(timeout)``，失败直接抛，不吞。"""

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

    def _send(self, topic: str, key: str, model: Any) -> None:
        producer = self._ensure()
        future: FutureRecordMetadata = producer.send(
            topic,
            key=key.encode("utf-8"),
            value=model.model_dump_json().encode("utf-8"),
        )
        future.get(timeout=self._timeout)
        producer.flush(timeout=self._timeout)

    def status(self, event: StatusEvent) -> None:
        """开跑时先发一条；前端靠它把徽标从"待解析"翻成"解析中"。"""
        self._send(self._settings.result_topic, event.doc_id, event)

    def result(self, result: ParseResult) -> None:
        self._send(self._settings.result_topic, result.doc_id, result)

    def dead_letter(self, letter: DeadLetter) -> None:
        key = str(letter.payload.get("doc_id") or "unknown")
        self._send(self._settings.dlq_topic, key, letter)

    def close(self) -> None:
        if self._producer is not None:
            self._producer.flush(timeout=self._timeout)
            self._producer.close(timeout=self._timeout)
            self._producer = None
