"""OpenAI Agents SDK 版：我们负责 runtime。

它给的是 Agent / Runner / 流式事件；
循环、并行、超时、JSON 解析、错误归一，全在下面这几行里写。
所以这条路最可控，也最需要自己写代码。
"""

from __future__ import annotations

import asyncio
import time

from agents import Agent, OpenAIChatCompletionsModel, RunConfig, Runner
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent

from chatbi.sense.config import LLMSettings, PipelineSettings
from chatbi.sense.contracts import (
    ChatTurn,
    ReplyJob,
    ReplyPlan,
    ReplyResult,
    SenseRequest,
    SenseResult,
    parse_verdict,
)
from chatbi.sense.planning import compose
from chatbi.sense.prompts import SENSE_PROMPT, render_history, reply_instructions
from chatbi.sense.runtimes.base import Emit, RuntimeErrorBase


class AgentsRuntime:
    """一个类同时实现 judge / reply：两条流程共用同一个模型客户端。

    拆两个类也行，但那样客户端建两次，对比时又多一个变量。
    """

    name = "agents"

    def __init__(self, llm: LLMSettings, pipeline: PipelineSettings) -> None:
        self._llm = llm
        self._pipeline = pipeline
        self._client = AsyncOpenAI(api_key=llm.api_key, base_url=llm.base_url)
        self._model = OpenAIChatCompletionsModel(model=llm.model, openai_client=self._client)
        self._config = RunConfig(tracing_disabled=True)

    async def judge(self, request: SenseRequest, history: list[ChatTurn]) -> SenseResult:
        started = time.monotonic()
        # 不用 output_type=SenseVerdict：那会让 SDK 发 response_format=json_schema，
        # DeepSeek 会回 400 "This response_format type is unavailable now"。
        # 改成让模型吐 JSON、我们自己解析校验——这本来就是我们负责的那部分 runtime。
        agent = Agent(name="sense", instructions=SENSE_PROMPT, model=self._model)
        prompt = _judge_input(request.text, history)
        try:
            async with asyncio.timeout(self._pipeline.sense_timeout_seconds):
                result = await Runner.run(agent, input=prompt, max_turns=1, run_config=self._config)
        except TimeoutError:
            raise RuntimeErrorBase("timeout", "情感识别超时") from None
        except Exception as exc:  # noqa: BLE001 - 统一归一，上层只认 code
            raise RuntimeErrorBase("model_call", f"情感识别失败：{exc}") from exc

        try:
            verdict = parse_verdict(str(result.final_output))
        except (ValueError, TypeError) as exc:
            raise RuntimeErrorBase("model_output", f"情感识别输出无法解析：{exc}") from exc

        return SenseResult(
            request_id=request.request_id,
            label=verdict.label,
            score=verdict.score,
            evidence=verdict.evidence,
            segments=verdict.segments,
            model=self._llm.model,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def reply(self, job: ReplyJob, emit: Emit) -> ReplyResult:
        started = time.monotonic()
        parts: dict[int, str] = {}

        async def run_plan(plan: ReplyPlan) -> None:
            parts[plan.order] = await self._stream_plan(job, plan, emit)

        try:
            async with asyncio.timeout(self._pipeline.reply_timeout_seconds):
                await asyncio.gather(*(run_plan(plan) for plan in job.plans))
        except TimeoutError:
            raise RuntimeErrorBase("timeout", "回应生成超时") from None
        except RuntimeErrorBase:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeErrorBase("model_call", f"回应生成失败：{exc}") from exc

        ordered = [parts[key] for key in sorted(parts)]
        return ReplyResult(
            request_id=job.request_id,
            session_id=job.session_id,
            text=compose(ordered),
            blocks=[{"type": "text", "text": text} for text in ordered if text.strip()],
            model=self._llm.model,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _stream_plan(self, job: ReplyJob, plan: ReplyPlan, emit: Emit) -> str:
        agent = Agent(
            name=f"reply-{plan.mode.value}",
            instructions=reply_instructions(plan.mode),
            model=self._model,
        )
        streamed = Runner.run_streamed(
            agent,
            input=_reply_input(job, plan),
            max_turns=1,
            run_config=self._config,
        )
        chunks: list[str] = []
        async for event in streamed.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                piece = event.data.delta
                if piece:
                    chunks.append(piece)
                    await emit(plan.order, piece)
        return "".join(chunks)

    async def aclose(self) -> None:
        await self._client.close()


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
