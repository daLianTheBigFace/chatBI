"""同一条句子分别过两个 runtime，打印判定、plan 和回应。

用途：改造提示词或换模型时，先看两条路的差异，再决定哪条上台。
不碰 Redis，也就没有进程间那一层——只比 runtime 本身。

    uv run python -m chatbi.sense.compare "项目上线了很开心，但爷爷刚走"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from chatbi.sense.config import load_settings
from chatbi.sense.contracts import ReplyJob, SenseRequest
from chatbi.sense.planning import derive_plans
from chatbi.sense.runtimes import build_runtime


async def run_one(name: str, text: str, project_root: Path) -> dict[str, object]:
    settings = load_settings(project_root, environment={**__import__("os").environ})
    runtime = build_runtime(settings, name)

    entry = getattr(runtime, "__aenter__", None)
    if entry is not None:
        runtime = await entry()

    try:
        started = time.monotonic()
        sense = await runtime.judge(
            SenseRequest(request_id=f"cmp-{name}", session_id="cmp", text=text), []
        )
        plans = derive_plans(text, sense)
        job = ReplyJob(
            request_id=f"cmp-{name}",
            session_id="cmp",
            text=text,
            plans=plans,
            sense=sense,
        )
        deltas: list[str] = []

        async def emit(plan: int, piece: str) -> None:
            deltas.append(piece)

        reply = await runtime.reply(job, emit)
        return {
            "runtime": name,
            "label": sense.label.value,
            "score": round(sense.score, 2),
            "evidence": sense.evidence,
            "segments": [(s.polarity, s.text) for s in sense.segments],
            "plans": [p.mode.value for p in plans],
            "text": reply.text,
            "deltas": len(deltas),
            "elapsed": round(time.monotonic() - started, 1),
        }
    finally:
        exit_ = getattr(runtime, "__aexit__", None)
        if exit_ is not None:
            await exit_(None, None, None)
        close = getattr(runtime, "aclose", None)
        if close is not None:
            await close()


def show(row: dict[str, object]) -> None:
    print(f"--- {row['runtime']} -------------------------------------------")
    print(f"判定   {row['label']}  score={row['score']}  plan={row['plans']}")
    if row["segments"]:
        print(f"分段   {row['segments']}")
    if row["evidence"]:
        print(f"证据   {row['evidence']}")
    print(f"回应   {row['text']!r}")
    print(f"耗时   {row['elapsed']}s  增量 {row['deltas']} 段")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对比两个 runtime 的同一条输入")
    parser.add_argument("text", help="要判断的一句话")
    parser.add_argument("--runtime", choices=["both", "agents", "graph"], default="both")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[3]
    names = ["agents", "graph"] if args.runtime == "both" else [args.runtime]
    rows = []
    for name in names:
        try:
            rows.append(await run_one(name, args.text, root))
        except Exception as exc:  # noqa: BLE001 - 对比脚本，失败也要继续跑另一条
            print(f"[{name}] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    for row in rows:
        show(row)
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
