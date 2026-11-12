"""打通 Redis + Kafka 的一遍端到端冒烟（本机跑，需要 SSH 隧道 + 实例在线）。

它验的是"管道"，不是 MinerU 质量：

    ① 建 documents 行（pending）→ ② scp 上传 PDF 到实例 → ③ 实例上真跑一次 MinerU
    → ④ 投 ParseRequest 进 Kafka → ⑤ 本机替身 worker 消费：置锁 + 发 status(parsing)
    → ⑥ 发 result(ok=True) → ⑦ bridge 消费：scp 拉产物 + 切块写库 + done
    → ⑧ Redis 收状态广播

每一步打一行，任何一步不对就停在那儿，不往下猜。

用法：
    uv run python scripts/mmrag_plumbing_smoke.py --pdf tmp/test.pdf
    uv run python scripts/mmrag_plumbing_smoke.py --skip-mineru --resume-doc d1a2b3c4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import redis as redis_sync
from kafka import KafkaConsumer, KafkaProducer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatbi.mmrag.api.sse import CHANNEL_ALL, publish_now  # noqa: E402
from chatbi.mmrag.bridge.result_consumer import handle_record as bridge_handle  # noqa: E402
from chatbi.mmrag.bridge.transfer import (  # noqa: E402
    push_pdf,
    remote_in_path,
    remote_out_path,
)
from chatbi.mmrag.config import MMRagSettings, load_settings  # noqa: E402
from chatbi.mmrag.contracts import ParseRequest, ParseResult, StatusEvent  # noqa: E402
from chatbi.mmrag.db import (  # noqa: E402
    create_document,
    get_document,
    init_db,
    list_chunks,
)
from chatbi.mmrag.models import DocStatus  # noqa: E402

step = 0


def say(message: str) -> None:
    global step
    step += 1
    print(f"[{step:02d}] {message}", flush=True)


def run_mineru_on_instance(settings: MMRagSettings, doc_id: str) -> tuple[int, str]:
    """在实例上真跑一次 MinerU，返回 (页数, 远端产物目录)。"""
    remote_in = remote_in_path(settings.mineru, doc_id)
    remote_out = remote_out_path(settings.mineru, doc_id)
    argv = [
        "ssh",
        settings.mineru.ssh_host,
        f"{settings.mineru.mineru_bin} parse {remote_in} --tier {settings.mineru.tier}"
        f" --pages {settings.mineru.pages} --format markdown -o {remote_out}/parsed.md --json",
    ]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=1800, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip()[-1500:])
    # 4.x 的 --json 打的是多行 pretty JSON，不是末行单条
    payload = json.loads(completed.stdout.strip())
    page_range = str((payload.get("parse") or {}).get("page_range") or "")
    tail = page_range.split("-")[-1].strip()
    pages = int(tail) if tail.isdigit() else 0
    return pages, str((payload.get("output") or {}).get("path") or remote_out)


def start_status_listener(settings: MMRagSettings, sink: list[str]) -> threading.Thread:
    def listen() -> None:
        client = redis_sync.from_url(settings.redis.url, decode_responses=True)
        pubsub = client.pubsub()
        pubsub.subscribe(CHANNEL_ALL)
        deadline = time.monotonic() + 180
        for message in pubsub.listen():
            if message["type"] == "message":
                sink.append(message["data"])
            if time.monotonic() > deadline:
                break
        pubsub.close()
        client.close()

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    return thread


def drain_bridge(settings: MMRagSettings, conn, group: str, doc_id: str, timeout: float) -> int:
    """把 result topic 里属于 doc_id 的消息喂给 bridge，其余（历史积压）跳过。

    独立消费组从 earliest 起会捡到旧消息，所以必须按 doc_id 过滤，
    否则会拿上一轮的 result 去覆盖这一轮的状态。
    """
    consumer = KafkaConsumer(
        settings.kafka.result_topic,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        consumer_timeout_ms=int(timeout * 1000),
        max_poll_records=1,
    )
    handled = 0
    try:
        for record in consumer:
            payload = json.loads(record.value)
            if str(payload.get("doc_id")) != doc_id:
                consumer.commit()
                continue
            bridge_handle(record, consumer=consumer, conn=conn, settings=settings)
            handled += 1
    finally:
        consumer.close()
    return handled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default=str(ROOT / "tmp" / "test.pdf"))
    parser.add_argument("--doc-id", default="")
    parser.add_argument("--skip-mineru", action="store_true", help="复用实例上已有产物")
    parser.add_argument("--remote-out", default="", help="--skip-mineru 时的远端产物目录")
    args = parser.parse_args()

    settings = load_settings(ROOT)
    settings.paths.ensure()
    conn = init_db(settings.paths.db_file)

    doc_id = args.doc_id or f"smoke{int(time.time())}"
    pdf = Path(args.pdf)
    say(f"doc_id={doc_id}  pdf={pdf.name} kafka={settings.kafka.bootstrap_servers}")

    existing = get_document(conn, doc_id)
    if existing is None:
        create_document(conn, doc_id=doc_id, filename=pdf.name, size_bytes=pdf.stat().st_size)
    say(f"documents 行就绪，状态={get_document(conn, doc_id).status.value}")

    raw_path = push_pdf(settings.mineru, doc_id, pdf)
    say(f"scp 上传完成 -> {raw_path}")

    if args.skip_mineru:
        remote_out = args.remote_out or remote_out_path(settings.mineru, doc_id)
        pages = 2
        say(f"跳过 MinerU，复用 {remote_out}")
    else:
        started = time.monotonic()
        pages, remote_out = run_mineru_on_instance(settings, doc_id)
        say(f"MinerU 跑完：pages={pages} 用时 {time.monotonic() - started:.0f}s -> {remote_out}")

    # ---- 投递 request
    producer = KafkaProducer(
        bootstrap_servers=settings.kafka.bootstrap_servers,
        acks="all",
    )
    request = ParseRequest(doc_id=doc_id, filename=pdf.name, raw_path=raw_path)
    meta = producer.send(
        settings.kafka.request_topic,
        key=doc_id.encode("utf-8"),
        value=request.model_dump_json().encode("utf-8"),
    ).get(timeout=10)
    producer.flush()
    say(f"已投递 request -> partition={meta.partition} offset={meta.offset}")

    # ---- 替身 worker：置锁 + 发 status(parsing) + 发 result(ok)
    locks = redis_sync.from_url(settings.redis.url, decode_responses=True)
    lock_key = f"parse:lock:{doc_id}:1"
    locked = locks.set(lock_key, "1", nx=True, ex=settings.redis.lock_ttl_seconds)
    again = locks.set(lock_key, "1", nx=True)
    say(f"幂等锁 {lock_key} 首次={locked} 第二次={again}（第二次必须是 None）")

    listeners = publish_now(settings.redis.url, StatusEvent(doc_id=doc_id, status="parsing"))
    say(f"发 status(parsing) 到 Kafka + Redis（当前订阅者 {listeners}）")

    result = ParseResult(
        doc_id=doc_id,
        ok=True,
        pages=pages,
        parsed_dir=remote_out,
        attempt=1,
        elapsed_ms=1000,
    )
    meta = producer.send(
        settings.kafka.result_topic,
        key=doc_id.encode("utf-8"),
        value=result.model_dump_json().encode("utf-8"),
    ).get(timeout=10)
    producer.flush()
    producer.close()
    say(f"已投递 result(ok) -> partition={meta.partition} offset={meta.offset}")

    # ---- bridge 接货
    sink: list[str] = []
    listener = start_status_listener(settings, sink)
    handled = drain_bridge(settings, conn, f"mmrag-bridge-smoke-{doc_id}", doc_id, timeout=60)
    listener.join(timeout=2)
    say(f"bridge 处理 {handled} 条；Redis 收到 {len(sink)} 条状态广播")

    # ---- 断言
    doc = get_document(conn, doc_id)
    chunks = list_chunks(conn, doc_id)
    say(f"最终状态={doc.status.value} pages={doc.pages} error={doc.error}")
    say(
        f"切块数={len(chunks)}（text={sum(c.kind.value == 'text' for c in chunks)}"
        f" table={sum(c.kind.value == 'table' for c in chunks)}"
        f" image={sum(c.kind.value == 'image' for c in chunks)}）"
    )
    if sink:
        print("   最后一条 Redis 事件:", sink[-1][:160])

    ok = doc.status is DocStatus.DONE and chunks
    print("\nRESULT:", "PASS" if ok else "FAIL")
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
