"""本机常驻：消费 doc.parse.result。

收到 result 后：
    ok    → scp 拉产物 → 建块写库 → 状态 done → SSE
    fail  → 状态 failed（原因照抄，不静默吞）

这是「实例解析完、本机接货」的那一跳；跑在本机，不占 GPU。

注意 done 的时机：**产物拉回 + 切块入库之后**才写，不是 MinerU 一返回就写。
否则前端看到"解析完成"立刻提问，检索结果是空的。
"""

from __future__ import annotations

import json
import logging
import sqlite3

from kafka import KafkaConsumer
from kafka.consumer.fetcher import ConsumerRecord

from chatbi.mmrag.api.sse import publish_now
from chatbi.mmrag.bridge import watchdog
from chatbi.mmrag.bridge.figures import materialize
from chatbi.mmrag.bridge.transfer import TransferError, pull_parsed
from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.contracts import ParseResult, StatusEvent
from chatbi.mmrag.db import init_db, replace_chunks, set_status
from chatbi.mmrag.ingest.chunker import build_chunks, load_items
from chatbi.mmrag.ingest.indexer import index_document
from chatbi.mmrag.models import DocStatus

log = logging.getLogger(__name__)

GROUP = "mmrag-bridge"
POLL_INTERVAL_MS = 5000


def build_consumer(settings: MMRagSettings) -> KafkaConsumer:
    return KafkaConsumer(
        settings.kafka.result_topic,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=1,
    )


def _emit(
    settings: MMRagSettings,
    doc_id: str,
    status: DocStatus,
    *,
    pages: int = 0,
    error: str | None = None,
) -> None:
    event = StatusEvent(doc_id=doc_id, status=status.value, pages=pages, error=error)
    listeners = publish_now(settings.redis.url, event)
    log.info("status doc=%s -> %s (listeners=%d)", doc_id, status.value, listeners)


def _ingest(settings: MMRagSettings, conn: sqlite3.Connection, result: ParseResult) -> int:
    """拉产物 → 切块 → 写库 → 向量化。返回块数。

    向量化放这里而不是 worker：块在本机产生（页面 bbox 也要靠本机产物），
    而且实例重启后 /workspace 会丢，向量必须落在本机磁盘。
    """
    local_parsed = settings.paths.parsed_dir(result.doc_id)
    files = pull_parsed(settings.mineru, result.doc_id, local_parsed)
    log.info("pulled %d files -> %s", files, local_parsed)

    items = load_items(local_parsed)
    chunks = list(build_chunks(result.doc_id, items, parsed_dir=local_parsed))
    count = replace_chunks(conn, result.doc_id, chunks)
    log.info("chunked doc=%s blocks=%d (text/table/image)", result.doc_id, count)

    # markdown 里的图只有块定位符，得回实例导成 PNG 才给前端显示
    figures = materialize(settings, conn, result.doc_id, local_parsed)
    log.info("figures doc=%s ready=%d", result.doc_id, figures)

    indexed = index_document(conn, settings, result.doc_id)
    log.info("indexed doc=%s vectors=%d", result.doc_id, indexed)
    return count


def handle_record(
    record: ConsumerRecord,
    *,
    consumer: KafkaConsumer,
    conn: sqlite3.Connection,
    settings: MMRagSettings,
) -> None:
    payload = json.loads(record.value)
    kind = str(payload.get("kind", "result"))

    if kind == "status":
        event = StatusEvent.model_validate(payload)
        status = DocStatus(event.status)
        set_status(conn, event.doc_id, status, error=event.error)
        _emit(settings, event.doc_id, status)
        consumer.commit()
        return

    result = ParseResult.model_validate(payload)
    if not result.ok:
        set_status(conn, result.doc_id, DocStatus.FAILED, error=result.error or "parse failed")
        _emit(settings, result.doc_id, DocStatus.FAILED, error=result.error)
        consumer.commit()
        return

    try:
        _ingest(settings, conn, result)
    except (TransferError, FileNotFoundError, ValueError) as exc:
        # 本机这一侧失败也算失败：产物没拉回来 / 格式不对，前端要能看到原因
        log.exception("ingest failed doc=%s", result.doc_id)
        set_status(conn, result.doc_id, DocStatus.FAILED, error=f"ingest failed: {exc}")
        _emit(settings, result.doc_id, DocStatus.FAILED, error=str(exc))
        consumer.commit()
        return

    set_status(conn, result.doc_id, DocStatus.DONE, pages=result.pages)
    _emit(settings, result.doc_id, DocStatus.DONE, pages=result.pages)
    consumer.commit()


def run(settings: MMRagSettings) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings.paths.ensure()
    conn = init_db(settings.paths.db_file)
    consumer = build_consumer(settings)
    log.info(
        "bridge up: topic=%s group=%s db=%s",
        settings.kafka.result_topic,
        GROUP,
        settings.paths.db_file,
    )
    try:
        # 用 poll 而不是 for：每轮先扫一次看门狗，再处理消息。
        # for 循环会一直阻塞在 fetch 上，卡死的文档没人收尸。
        while True:
            watchdog.sweep(
                conn,
                notify=lambda event: publish_now(settings.redis.url, event),
            )
            batches = consumer.poll(timeout_ms=POLL_INTERVAL_MS)
            for records in batches.values():
                for record in records:
                    handle_record(record, consumer=consumer, conn=conn, settings=settings)
    except KeyboardInterrupt:
        log.info("bridge stopped by user")
    finally:
        consumer.close()
        conn.close()




