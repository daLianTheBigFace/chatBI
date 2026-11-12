"""消费循环：手动提交 offset，处理成功才 commit；同 doc 串行靠分区 + key=doc_id。

三个必须踩对的点：

1. ``enable_auto_commit=False``。自动提交是"取到就算处理完"，MinerU 一挂，
   消息已被标记消费，文档永久卡 parsing。手动 commit 放在 result 发出之后。
2. ``max_poll_interval_ms`` 要大于 MinerU 的超时（1800 秒），否则解析到一半
   会被踢出消费组、触发 rebalance，另一台机器又把同一份文档捡起来重跑。
3. 幂等锁 ``parse:lock:{doc_id}:{attempt}`` 只在 TTL 内有效；作用不是"跑完删掉"，
   而是防止同一 attempt 被重投时被两个消费者同时解析。不主动 DEL，
   让它自然过期 —— 提前删等于放开闸门。
"""

from __future__ import annotations

import logging
import time

import redis as redis_sync
from kafka import KafkaConsumer
from kafka.consumer.fetcher import ConsumerRecord
from pydantic import ValidationError

from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.contracts import DeadLetter, ParseRequest, StatusEvent
from chatbi.mmrag.worker.pipeline import handle
from chatbi.mmrag.worker.reporter import Reporter

log = logging.getLogger(__name__)

# MinerU 单篇最长 30 分钟，留 15 分钟余量给提交与下一轮 poll
MAX_POLL_INTERVAL_MS = 45 * 60 * 1000


def build_consumer(settings: MMRagSettings) -> KafkaConsumer:
    return KafkaConsumer(
        settings.kafka.request_topic,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=settings.kafka.worker_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=1,
        max_poll_interval_ms=MAX_POLL_INTERVAL_MS,
        key_deserializer=lambda raw: raw.decode("utf-8") if raw else None,
    )


def handle_record(
    record: ConsumerRecord,
    *,
    consumer: KafkaConsumer,
    reporter: Reporter,
    locks: redis_sync.Redis,
    settings: MMRagSettings,
) -> None:
    """单条消息：解析 → 上报 → 提交。任何一步抛异常都不提交，交给上层重投。"""
    try:
        request = ParseRequest.model_validate_json(record.value)
    except ValidationError as exc:
        # 契约坏了，重投多少次都没用，直接进 DLQ 并提交，别堵住分区
        reporter.dead_letter(
            DeadLetter(
                original_topic=settings.kafka.request_topic,
                payload={"raw": record.value.decode("utf-8", "replace")},
                attempts=0,
                error=f"invalid ParseRequest: {exc}",
            )
        )
        consumer.commit()
        return

    lock_key = f"parse:lock:{request.doc_id}:{request.attempt}"
    if not locks.set(lock_key, "1", nx=True, ex=settings.redis.lock_ttl_seconds):
        log.info("skip duplicate doc=%s attempt=%s", request.doc_id, request.attempt)
        consumer.commit()
        return

    reporter.status(StatusEvent(doc_id=request.doc_id, status="parsing"))
    started = time.monotonic()
    try:
        result = handle(request, settings)
    except Exception as exc:  # noqa: BLE001 - 兜底：任何崩溃都要有人知道
        log.exception("pipeline crashed doc=%s", request.doc_id)
        reporter.dead_letter(
            DeadLetter(
                original_topic=settings.kafka.request_topic,
                payload=request.model_dump(mode="json"),
                attempts=request.attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        consumer.commit()
        return

    reporter.result(result)
    consumer.commit()
    log.info(
        "doc=%s ok=%s pages=%s elapsed=%dms (round %.1fs)",
        result.doc_id,
        result.ok,
        result.pages,
        result.elapsed_ms,
        time.monotonic() - started,
    )


def run(settings: MMRagSettings) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # kafka-python 每几分钟刷一行 metadata，INFO 会把真正的解析记录淹掉
    logging.getLogger("kafka").setLevel(logging.WARNING)
    reporter = Reporter(settings.kafka)
    locks = redis_sync.from_url(settings.redis.url, decode_responses=True, socket_timeout=10)
    consumer = build_consumer(settings)
    log.info(
        "worker up: topic=%s group=%s servers=%s",
        settings.kafka.request_topic,
        settings.kafka.worker_group,
        settings.kafka.bootstrap_servers,
    )
    try:
        for record in consumer:
            handle_record(
                record,
                consumer=consumer,
                reporter=reporter,
                locks=locks,
                settings=settings,
            )
    except KeyboardInterrupt:
        log.info("worker stopped by user")
    finally:
        consumer.close()
        reporter.close()
        locks.close()
