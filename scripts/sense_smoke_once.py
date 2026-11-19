"""一句话验完整链路：POST → SSE → judged/delta×N/done，确认 done 之后不再来 error。"""

from __future__ import annotations

import json
import sys
import threading
import time

import httpx

BASE = "http://127.0.0.1:8800/api/sense"


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "项目上线了很开心，但爷爷刚走"
    events: list[tuple[float, str, dict]] = []

    def watch(request_id: str) -> None:
        with httpx.stream("GET", f"{BASE}/{request_id}/events", timeout=120) as response:
            name = ""
            for line in response.iter_lines():
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    events.append((time.monotonic(), name, json.loads(line[5:])))
                    if name in ("done", "error"):
                        return

    started = time.monotonic()
    response = httpx.post(BASE, json={"text": text}, timeout=30)
    print("post", response.status_code, response.text[:120])
    if response.status_code != 202:
        return 1

    request_id = response.json()["request_id"]
    watcher = threading.Thread(target=watch, args=(request_id,), daemon=True)
    watcher.start()
    watcher.join(timeout=110)

    counts: dict[str, int] = {}
    for _, name, _ in events:
        counts[name] = counts.get(name, 0) + 1
    print("events", counts, "elapsed", round(time.monotonic() - started, 1))
    for _, name, payload in events:
        if name == "judged":
            print("  judged:", payload.get("label"), payload.get("score"), payload.get("evidence"))
        elif name == "error":
            print("  ERROR FRAME:", payload)
        elif name == "done":
            print("  done:", str(payload.get("text", ""))[:160].replace("\n", " / "))
    return 0 if "error" not in counts and "done" in counts else 2


if __name__ == "__main__":
    raise SystemExit(main())
