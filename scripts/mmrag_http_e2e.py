"""HTTP 端到端：上传 → 轮询状态 → 问答流。

对着真服务跑（不是 TestClient）：
    uv run uvicorn chatbi.mmrag.api.app:create_app --factory --port 8791
    uv run python scripts/mmrag_http_e2e.py --pdf tmp/test.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx

BASE = os.getenv("MMRAG_API_BASE", "http://127.0.0.1:8791")


def wait_done(client: httpx.Client, doc_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        rows = client.get(f"{BASE}/api/mmrag/docs/status", params={"ids": doc_id}).json()
        row = rows[0] if rows else {}
        status = str(row.get("status") or "")
        if status != last:
            print(f"    状态 -> {status} (pages={row.get('pages')})", flush=True)
            last = status
        if status in {"done", "failed"}:
            return row
        time.sleep(2)
    raise SystemExit(f"等待超时，最后状态 {last or '未知'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default="tmp/test.pdf")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--question", default="这份文档讲了什么？表格里有什么内容？")
    args = parser.parse_args()

    pdf = Path(args.pdf)
    with httpx.Client(timeout=60) as client:
        print(f"[01] 上传 {pdf.name}", flush=True)
        with pdf.open("rb") as handle:
            response = client.post(
                f"{BASE}/api/mmrag/docs/upload",
                files=[("files", (pdf.name, handle, "application/pdf"))],
            )
        response.raise_for_status()
        document = response.json()[0]
        doc_id = document["id"]
        print(f"[02] doc_id={doc_id} 初始状态={document['status']}", flush=True)

        detail = wait_done(client, doc_id, args.timeout)
        print(f"[03] 终态={detail['status']} pages={detail.get('pages')}", flush=True)
        if detail["status"] != "done":
            print(f"    失败原因：{detail.get('error')}")
            return 1

        print(f"[04] 提问：{args.question}", flush=True)
        started = time.monotonic()
        events: dict[str, int] = {}
        answer = ""
        with client.stream(
            "POST",
            f"{BASE}/api/mmrag/chat/stream",
            data={"question": args.question, "session_id": f"e2e-{doc_id}"},
            timeout=180,
        ) as stream:
            if stream.status_code != 200:
                print(f"    HTTP {stream.status_code}: {stream.read()[:400]!r}")
                return 1
            name = ""
            for line in stream.iter_lines():
                if line.startswith("event: "):
                    name = line[7:].strip()
                    events[name] = events.get(name, 0) + 1
                elif line.startswith("data: ") and name == "delta":
                    answer += json.loads(line[6:])["text"]
                elif line.startswith("data: ") and name == "error":
                    print(f"    error: {line[6:]}")
                    return 1

        elapsed = time.monotonic() - started
        print(f"[05] 事件 {events}，用时 {elapsed:.1f}s", flush=True)
        print("[06] 回答：", flush=True)
        print(answer.strip() or "(空)", flush=True)

        history = client.get(f"{BASE}/api/mmrag/sessions/e2e-{doc_id}/messages").json()
        blocks = history[-1]["blocks"] if history else []
        print(f"[07] 历史 {len(history)} 条，末条 blocks={[b['type'] for b in blocks]}", flush=True)

    ok = events.get("done", 0) == 1 and bool(answer.strip())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

