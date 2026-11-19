"""LangGraph 版：把 runtime 交给它。

和 Agents 版的区别在于谁驱动：
- 这里我们只写节点（judge / generate），编排、状态、checkpoint、流式下发由图负责；
- 所以能在生成中途崩掉之后用同一个 thread_id 续跑，代价是多一个 sqlite 文件。
"""

from __future__ import annotations

import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from chatbi.sense.config import LLMSettings, PipelineSettings
from chatbi.sense.contracts import (
    ChatTurn,
    ReplyJob,
    ReplyPlan,
    ReplyResult,
    SenseRequest,
    SenseResult,
    SenseVerdict,
)
from chatbi.sense.planning import compose
from chatbi.sense.prompts import SENSE_PROMPT, render_history, reply_instructions
from chatbi.sense.runtimes.base import Emit, RuntimeErrorBase


class JudgeState(TypedDict, total=False):
    prompt: str
    verdict: SenseVerdict


class ReplyState(TypedDict, total=False):
    job: dict[str, Any]
    parts: dict[str, str]
    seq: dict[str, int]


class GraphRuntime:
    name = "graph"

    def __init__(
        self,
        llm: LLMSettings,
        pipeline: PipelineSettings,
        *,
        checkpoint_path: str | None = None,
    ) -> None:
        self._llm = llm
        self._pipeline = pipeline
        self._checkpoint_path = checkpoint_path
        self._stack = AsyncExitStack()
        self._model = ChatOpenAI(
            model=llm.model,
            api_key=llm.api_key,
            base_url=llm.base_url,
            temperature=llm.temperature,
            timeout=llm_timeout(pipeline),
        )
        self._judge_graph = self._build_judge()
        self._reply_graph: Any = None

    # ------------------------------------------------------------ 生命周期

    async def __aenter__(self) -> GraphRuntime:
        checkpointer = None
        if self._checkpoint_path:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            # sqlite 不会自己建父目录，data/sense 不存在就 unable to open database file
            Path(self._checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            checkpointer = await self._stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(self._checkpoint_path)
            )
            await checkpointer.setup()
        self._reply_graph = self._build_reply(checkpointer)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    # ------------------------------------------------------------ judge

    def _build_judge(self) -> Any:
        # json_mode 而不是默认的 function_calling / json_schema：
        # DeepSeek 对 response_format=json_schema 直接 400，json_object 才是它支持的。
        structured = self._model.with_structured_output(SenseVerdict, method="json_mode")

        async def judge(state: JudgeState) -> JudgeState:
            verdict = await structured.ainvoke(
                [SystemMessage(content=SENSE_PROMPT), HumanMessage(content=state["prompt"])]
            )
            if not isinstance(verdict, SenseVerdict):
                raise RuntimeErrorBase("model_output", "情感识别没有返回结构化结果")
            return {"verdict": verdict}

        builder = StateGraph(JudgeState)
        builder.add_node("judge", judge)
        builder.add_edge(START, "judge")
        builder.add_edge("judge", END)
        return builder.compile()

    async def judge(self, request: SenseRequest, history: list[ChatTurn]) -> SenseResult:
        started = time.monotonic()
        prompt = _judge_input(request.text, history)
        try:
            out = await self._judge_graph.ainvoke({"prompt": prompt})
        except RuntimeErrorBase:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeErrorBase("model_call", f"情感识别失败：{exc}") from exc
        verdict: SenseVerdict = out["verdict"]
        return SenseResult(
            request_id=request.request_id,
            label=verdict.label,
            score=verdict.score,
            evidence=verdict.evidence,
            segments=verdict.segments,
            model=self._llm.model,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------ reply

    def _build_reply(self, checkpointer: Any) -> Any:
        async def generate(state: ReplyState) -> ReplyState:
            job = ReplyJob.model_validate(state["job"])
            parts: dict[str, str] = dict(state.get("parts") or {})
            seq: dict[str, int] = dict(state.get("seq") or {})
            writer = get_stream_writer()
            for plan in job.plans:
                messages = [
                    SystemMessage(content=reply_instructions(plan.mode)),
                    HumanMessage(content=_reply_input(job, plan)),
                ]
                collected: list[str] = []
                async for chunk in self._model.astream(messages):
                    piece = chunk.content if isinstance(chunk.content, str) else ""
                    if not piece:
                        continue
                    collected.append(piece)
                    key = str(plan.order)
                    seq[key] = seq.get(key, 0) + 1
                    writer({"plan": plan.order, "seq": seq[key], "text": piece})
                parts[str(plan.order)] = "".join(collected)
            return {"parts": parts, "seq": seq}

        builder = StateGraph(ReplyState)
        builder.add_node("generate", generate)
        builder.add_edge(START, "generate")
        builder.add_edge("generate", END)
        return builder.compile(checkpointer=checkpointer)

    async def reply(self, job: ReplyJob, emit: Emit) -> ReplyResult:
        if self._reply_graph is None:
            self._reply_graph = self._build_reply(None)

        started = time.monotonic()
        config = {
            "configurable": {"thread_id": f"reply:{job.request_id}"},
            "recursion_limit": 6,
        }
        parts: dict[str, str] = {}
        try:
            async for mode, chunk in self._reply_graph.astream(
                {"job": job.model_dump(mode="json")},
                config=config,
                stream_mode=["custom", "values"],
            ):
                if mode == "custom" and isinstance(chunk, dict) and "text" in chunk:
                    await emit(int(chunk.get("plan", 0)), str(chunk["text"]))
                elif mode == "values" and isinstance(chunk, dict):
                    parts = dict(chunk.get("parts") or {})
        except RuntimeErrorBase:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeErrorBase("model_call", f"回应生成失败：{exc}") from exc

        ordered = [parts[key] for key in sorted(parts, key=int)]
        return ReplyResult(
            request_id=job.request_id,
            session_id=job.session_id,
            text=compose(ordered),
            blocks=[{"type": "text", "text": text} for text in ordered if text.strip()],
            model=self._llm.model,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def llm_timeout(pipeline: PipelineSettings) -> float:
    return max(pipeline.sense_timeout_seconds, pipeline.reply_timeout_seconds)


def _judge_input(text: str, history: list[ChatTurn]) -> str:
    lines = [f"{'用户' if turn.role == 'user' else '助手'}：{turn.text}" for turn in history[-4:]]
    context = render_history(lines)
    return f"{context}\n\n现在要判断的这句话：\n{text}" if context else text


def _reply_input(job: ReplyJob, plan: ReplyPlan) -> str:
    lines = [
        f"{'用户' if turn.role == 'user' else '助手'}：{turn.text}" for turn in job.history[-6:]
    ]
    context = render_history(lines)
    targets = "、".join(f"「{item}」" for item in plan.targets) or "整句"
    pieces = [f"这次要回应的是：{targets}", f"用户刚说：{job.text}"]
    if context:
        pieces.insert(0, context)
    return "\n\n".join(pieces)
