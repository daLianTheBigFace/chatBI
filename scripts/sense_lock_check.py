"""验证会话锁：一轮说完后锁立刻释放，不用等 30 秒 TTL。

跑法：.venv\\Scripts\\python.exe scripts\\sense_lock_check.py
"""

from __future__ import annotations

import json
import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8800"


def submit(client: httpx.Client, text: str, session_id: str) -> tuple[int, dict]:
    response = client.post(
        f"{BASE}/api/sense",
        json={"text": text, "session_id": session_id},
        timeout=30.0,
    )
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {"raw": response.text}
    return response.status_code, body


def wait_done(client: httpx.Client, request_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    with client.stream("GET", f"{BASE}/api/sense/{request_id}/events", timeout=timeout) as stream:
        name = ""
        for line in stream.iter_lines():
            if time.time() > deadline:
                return {"error": "timeout"}
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if name in {"done", "error"}:
                    return {"event": name, **payload}
    return {"error": "stream closed"}


def main() -> int:
    session = f"lockcheck-{uuid.uuid4().hex[:8]}"
    with httpx.Client() as client:
        code1, body1 = submit(client, "今天有点累", session)
        print(f"[01] 第一条 -> {code1} rid={body1.get('request_id')}")
        if code1 != 202:
            print(f"FAIL 第一条就没被接收：{body1}")
            return 1
        rid1 = body1["request_id"]

        code2, body2 = submit(client, "我还没说完", session)
        print(f"[02] 生成中再问 -> {code2} {body2.get('detail', '')}")
        ok_busy = code2 == 409

        done = wait_done(client, rid1)
        print(f"[03] 第一条结束 -> {done.get('event')} 用时 {done.get('elapsed_ms')}ms")

        code3, body3 = submit(client, "现在再问一句", session)
        print(f"[04] 说完再问 -> {code3} rid={body3.get('request_id')}")
        ok_free = code3 == 202

        if ok_free:
            done3 = wait_done(client, body3["request_id"])
            print(f"[05] 第二条结束 -> {done3.get('event')} 用时 {done3.get('elapsed_ms')}ms")

    print("RESULT:", "PASS" if (ok_busy and ok_free) else "FAIL")
    return 0 if (ok_busy and ok_free) else 1


if __name__ == "__main__":
    sys.exit(main())
