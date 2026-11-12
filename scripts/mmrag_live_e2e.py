"""真链路验收：worker 跑在实例上，bridge 跑在本机，这里只负责"当一个用户"。

    建 documents 行 → scp 原始 PDF 上实例 → 投 doc.parse.request
    → 等 bridge 把状态写成 done（或 failed）→ 打印块数与状态轨迹

跑之前要有两个进程在跑：
    实例：python -m chatbi.mmrag.worker
    本机：python -m chatbi.mmrag.bridge
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kafka import KafkaProducer  # noqa: E402

from chatbi.mmrag.bridge.transfer import push_pdf  # noqa: E402
from chatbi.mmrag.config import load_settings  # noqa: E402
from chatbi.mmrag.contracts import ParseRequest  # noqa: E402
from chatbi.mmrag.db import (  # noqa: E402
    connect,
    create_document,
    get_document,
    list_chunks,
)
from chatbi.mmrag.models import DocStatus  # noqa: E402


def produce(settings, request: ParseRequest) -> None:
    producer = KafkaProducer(bootstrap_servers=settings.kafka.bootstrap_servers, acks="all")
    try:
        meta = producer.send(
            settings.kafka.request_topic,
            key=request.doc_id.encode("utf-8"),
            value=request.model_dump_json().encode("utf-8"),
        ).get(timeout=10)
    finally:
        producer.close()
    print(f"[03] 已投递 request -> partition={meta.partition} offset={meta.offset}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default=str(ROOT / "tmp" / "test.pdf"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    pdf = Path(args.pdf).resolve()
    if not pdf.exists():
        print(f"PDF 不存在: {pdf}")
        return 2

    settings = load_settings(ROOT)
    settings.paths.ensure()
    conn = connect(settings.paths.db_file)

    doc_id = f"live{uuid.uuid4().hex[:10]}"
    print(f"[01] doc_id={doc_id} pdf={pdf.name}", flush=True)

    create_document(conn, doc_id=doc_id, filename=pdf.name, size_bytes=pdf.stat().st_size)
    print(f"[02] documents 行就绪，状态={get_document(conn, doc_id).status}", flush=True)

    remote = push_pdf(settings.mineru, doc_id, pdf)
    print(f"[02b] 原始 PDF 已在实例: {remote}", flush=True)

    produce(
        settings,
        ParseRequest(doc_id=doc_id, filename=pdf.name, raw_path=str(pdf)),
    )

    deadline = time.monotonic() + args.timeout
    last = None
    while time.monotonic() < deadline:
        doc = get_document(conn, doc_id)
        if doc is not None and doc.status != last:
            last = doc.status
            print(f"[04] 状态 -> {doc.status} (pages={doc.pages})", flush=True)
        if doc is not None and doc.status in {DocStatus.DONE, DocStatus.FAILED}:
            break
        time.sleep(2)
    else:
        print("[!] 超时，状态没走到终态")
        return 1

    doc = get_document(conn, doc_id)
    chunks = list_chunks(conn, doc_id)
    kinds: dict[str, int] = {}
    for chunk in chunks:
        kinds[chunk.kind.value] = kinds.get(chunk.kind.value, 0) + 1

    print(f"[05] 最终状态={doc.status} pages={doc.pages} error={doc.error}")
    print(f"[06] 块数={len(chunks)} 分布={kinds}")
    print(f"[07] 向量已写入={sum(1 for c in chunks if c.vec_id is not None)}")
    conn.close()

    ok = doc.status is DocStatus.DONE and bool(chunks)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
