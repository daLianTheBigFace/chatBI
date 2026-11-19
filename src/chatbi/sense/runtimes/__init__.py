"""按配置挑 runtime。两个实现都实现同一组 Protocol，调用方不需要知道是哪个。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chatbi.sense.config import SenseSettings

if TYPE_CHECKING:
    from chatbi.sense.runtimes.base import JudgeRuntime, ReplyRuntime

RUNTIMES = ("agents", "graph")


def build_runtime(settings: SenseSettings, name: str | None = None) -> object:
    """返回同时实现 judge / reply 的对象；两个适配器都是这样。"""
    chosen = (name or settings.runtime).lower()
    if chosen == "agents":
        from chatbi.sense.runtimes.agents_sdk import AgentsRuntime

        return AgentsRuntime(settings.llm, settings.pipeline)
    if chosen == "graph":
        from chatbi.sense.runtimes.langgraph_rt import GraphRuntime

        return GraphRuntime(
            settings.llm,
            settings.pipeline,
            checkpoint_path=str(settings.checkpoint_path),
        )
    raise ValueError(f"未知 runtime：{chosen}（可选 {', '.join(RUNTIMES)}）")


__all__ = ["RUNTIMES", "build_runtime", "JudgeRuntime", "ReplyRuntime"]
