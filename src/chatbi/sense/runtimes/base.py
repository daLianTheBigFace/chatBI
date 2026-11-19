"""两个 runtime 的共同接口。

谁在跑由配置决定，调用方只认这两个 Protocol：

    judge = runtime.judge(request, history)
    reply = runtime.reply(job, emit)

Agent SDK 由我们自己驱动循环（拿它的 Agent/Runner 当积木）；
LangGraph 由它的图驱动（我们只提供节点）。
差异全部收在 adapters 里，流水线代码一行都不用改。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from chatbi.sense.contracts import ChatTurn, ReplyJob, ReplyResult, SenseRequest, SenseResult

# (plan 序号, 文本增量) —— plan 用来让前端把两段分开排
Emit = Callable[[int, str], Awaitable[None]]


class JudgeRuntime(Protocol):
    name: str

    async def judge(self, request: SenseRequest, history: list[ChatTurn]) -> SenseResult: ...


class ReplyRuntime(Protocol):
    name: str

    async def reply(self, job: ReplyJob, emit: Emit) -> ReplyResult: ...


class RuntimeErrorBase(RuntimeError):
    """两个 runtime 统一抛这个，上层不用分辨是谁炸的。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
