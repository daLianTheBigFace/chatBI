"""验证 chat/stream 一次带多个文件：上传 → 等解析 → 检索 → 流式回答。

真服务（8791）+ 真 Kafka + 真 MinerU，只断言"接口行为"，不比对答案内容。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = os.getenv("MMRAG_API_BASE", "http://127.0.0.1:8791")
API = f"{BASE}/api/mmrag"


def _frames(response: httpx.Response):
    name = "message"
    data: list[str] = []
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line.strip():
            if data:
                yield name, json.loads("".join(data))
            name, data = "message", []
    if data:
        yield name, json.loads("".join(data))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", action="append", default=[])
    parser.add_argument("--question", default="这两份文档各讲什么？表格里有什么？")
    parser.add_argument("--timeout", type=float, default=420.0)
    args = parser.parse_args()

    pdfs = [Path(item) for item in args.pdf] or [Path("tmp/test.pdf")]
    missing = [str(pdf) for pdf in pdfs if not pdf.is_file()]
    if missing:
        print(f"缺文件: {missing}")
        return 2

    files = [("files", (pdf.name, pdf.read_bytes(), "application/pdf")) for pdf in pdfs]
    data = {"question": args.question}
    counts: dict[str, int] = {}
    answer = ""
    session = ""
    started = time.monotonic()

    print(f"[01] 提交 {len(pdfs)} 份 PDF + 提问：{args.question}")
    with httpx.Client(timeout=httpx.Timeout(args.timeout, read=args.timeout)) as client:
        with client.stream("POST", f"{API}/chat/stream", data=data, files=files) as response:
            if response.status_code != 200:
                print(f"HTTP {response.status_code}: {response.read().decode()[:500]}")
                return 1
            for name, payload in _frames(response):
                counts[name] = counts.get(name, 0) + 1
                if name == "start":
                    session = payload["session_id"]
                    print(f"[02] start session={session}")
                elif name == "attached":
                    print(f"[03] attached {len(payload['docs'])} 份：{[d['filename'] for d in payload['docs']]}")
                elif name == "waiting":
                    done = sum(1 for doc in payload["docs"] if doc["status"] == "done")
                    print(f"[04] waiting {done}/{len(payload['docs'])} 已解析，已等 {payload['waited']}s")
                elif name == "meta":
                    print(f"[05] meta hits={payload.get('hits')} citations={len(payload.get('citations') or [])} 失败={payload.get('failed_docs')}")
                elif name == "delta":
                    answer += payload["text"]
                elif name == "error":
                    print(f"[!!] error {payload}")
                elif name == "done":
                    print(f"[06] done blocks={[b['type'] for b in payload.get('blocks') or []]}")

    elapsed = round(time.monotonic() - started, 1)
    print(f"[07] 事件计数 {counts}，用时 {elapsed}s")
    print(f"[08] 回答（前 200 字）：{answer[:200]}")
    ok = counts.get("done", 0) == 1 and counts.get("meta", 0) == 1 and bool(answer.strip())
    print("RESULT: PASS" if ok else "RESULT: FAIL")

    if session:
        with httpx.Client(timeout=30) as client:
            history = client.get(f"{API}/sessions/{session}/messages").json()
            print(f"[09] 历史 {len(history)} 条，末条 role={history[-1]['role'] if history else None}")
            sessions = client.get(f"{API}/sessions").json()
            print(f"[10] 会话列表 {len(sessions)} 条，首条标题={sessions[0]['title'][:30] if sessions else None}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())


