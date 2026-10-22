"""Per-request bounded event channel and privacy-aware UI projection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from chatbi.sql_agent.enums import ResponseStatus, WorkflowStage
from chatbi.sql_agent.events import (
    AnswerCompleted,
    AnswerDelta,
    AnswerStarted,
    ChatEvent,
    ChatRequest,
    EventPayload,
    Progress,
    RunCompleted,
    RunError,
    RunStarted,
    SQLCompleted,
    SQLExecuting,
    SQLFailed,
    SQLStarted,
    WarningEvent,
)
from chatbi.sql_agent.models import QueryResult, RunMetadata, SQLAgentRequest, SQLQuery
from chatbi.sql_agent.redaction import ConservativeDisplayRedaction, DisplayRedactionStrategy

if TYPE_CHECKING:
    from chatbi.sql_agent.service import SQLAgentService

EVENT_QUEUE_SIZE = 32
HEARTBEAT_SECONDS = 15.0
PUBLIC_SQL_FAILURE = "该 SQL 未通过校验或执行失败。"
PUBLIC_RUN_FAILURE = "本次查询未能完成，请调整问题或稍后重试。"
PUBLIC_STREAM_FAILURE = "查询流意外结束，请重新提交问题。"
STAGE_TEXT = {
    WorkflowStage.SCHEMA: "正在读取可用的数据结构。",
    WorkflowStage.UNDERSTANDING: "正在理解问题和统计条件。",
    WorkflowStage.SELECTION: "正在选择相关数据表。",
    WorkflowStage.GENERATION: "正在生成查询 SQL。",
    WorkflowStage.VALIDATION: "正在校验 SQL 的只读权限和语法。",
    WorkflowStage.EXECUTION: "正在执行只读查询。",
    WorkflowStage.REPAIR: "正在根据校验或执行反馈调整 SQL。",
    WorkflowStage.SYNTHESIS: "正在整理结果并生成回答。",
    WorkflowStage.COMPLETED: "处理完成。",
}


class EventChannel:
    def __init__(self, request: ChatRequest, display: DisplayRedactionStrategy) -> None:
        self.queue: asyncio.Queue[ChatEvent] = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.request = request
        self.display = display
        self.conversation_id = request.conversation_id or uuid4()
        self.message_id = uuid4()
        self.run_id = uuid4()
        self.block_id = uuid4()
        self.sequence = 0
        self.answer_started = False
        self._answer_buffer = ""
        self._pending_sql: dict[UUID, str] = {}

    async def emit(self, payload: EventPayload) -> None:
        self.sequence += 1
        await self.queue.put(
            ChatEvent(
                sequence=self.sequence,
                conversation_id=self.conversation_id,
                user_message_id=self.request.user_message_id,
                message_id=self.message_id,
                run_id=self.run_id,
                payload=payload,
            )
        )

    async def start_answer(self) -> None:
        if not self.answer_started:
            self.answer_started = True
            await self.emit(AnswerStarted(block_id=self.block_id))

    async def progress(self, stage: WorkflowStage) -> None:
        await self.emit(Progress(stage=stage, text=STAGE_TEXT[stage]))
        if stage == WorkflowStage.SYNTHESIS:
            await self.start_answer()

    async def sql_started(self, attempt: int, query: SQLQuery) -> UUID:
        tool_call_id = uuid4()
        self._pending_sql[tool_call_id] = "validation"
        await self.emit(
            SQLStarted(tool_call_id=tool_call_id, attempt=attempt, query=self.display.query(query))
        )
        return tool_call_id

    async def sql_executing(self, tool_call_id: UUID) -> None:
        self._pending_sql[tool_call_id] = "execution"
        await self.emit(SQLExecuting(tool_call_id=tool_call_id))

    async def sql_completed(self, tool_call_id: UUID, result: QueryResult) -> None:
        self._pending_sql.pop(tool_call_id, None)
        await self.emit(SQLCompleted(tool_call_id=tool_call_id, result=self.display.result(result)))

    async def sql_failed(self, tool_call_id: UUID, phase: str, code: str) -> None:
        self._pending_sql.pop(tool_call_id, None)
        await self.emit(
            SQLFailed(tool_call_id=tool_call_id, phase=phase, code=code, text=PUBLIC_SQL_FAILURE)
        )

    async def answer_delta(self, delta: str) -> None:
        self._answer_buffer += delta
        safe, self._answer_buffer = self.display.stream_prefix(self._answer_buffer)
        if safe:
            await self.emit(AnswerDelta(block_id=self.block_id, delta=safe))

    async def finish_answer(self, text: str) -> None:
        await self.start_answer()
        # Flush only after synthesis succeeded; the final event is authoritative on fallback.
        if self._answer_buffer and text.endswith(self._answer_buffer):
            await self.emit(
                AnswerDelta(block_id=self.block_id, delta=self.display.text(self._answer_buffer))
            )
        self._answer_buffer = ""
        await self.emit(AnswerCompleted(block_id=self.block_id, text=self.display.text(text)))

    async def fail_pending_sql(self, code: str) -> None:
        for call_id, phase in list(self._pending_sql.items()):
            await self.sql_failed(call_id, phase, code)


async def stream_response(
    service: SQLAgentService,
    request: ChatRequest,
    *,
    display_factory: Callable[[], DisplayRedactionStrategy] = ConservativeDisplayRedaction,
) -> AsyncIterator[ChatEvent | None]:
    request = ChatRequest.model_validate(request.model_dump())
    channel = EventChannel(request, display_factory())

    async def produce() -> None:
        await channel.emit(RunStarted(user_message_id=request.user_message_id))
        try:
            response = await service._ask(
                SQLAgentRequest(question=request.question, history=request.history),
                request_id=str(channel.run_id),
                observer=channel,
                display=channel.display,
                answer_sink=channel.answer_delta,
            )
            if response.answer is not None:
                await channel.finish_answer(response.answer)
            for warning in response.warnings:
                await channel.emit(WarningEvent(text=channel.display.text(warning)))
            if response.error is not None:
                await channel.fail_pending_sql(response.error.code)
                await channel.emit(RunError(code=response.error.code, text=PUBLIC_RUN_FAILURE))
            await channel.emit(
                RunCompleted(
                    status=response.status,
                    clarification_questions=[
                        channel.display.text(item) for item in response.clarification_questions
                    ],
                    metadata=response.metadata,
                )
            )
        except Exception:
            await channel.fail_pending_sql("internal_error")
            if channel.answer_started:
                await channel.finish_answer(PUBLIC_STREAM_FAILURE)
            await channel.emit(RunError(code="internal_error", text=PUBLIC_STREAM_FAILURE))
            await channel.emit(RunCompleted(status=ResponseStatus.FAILED, metadata=RunMetadata()))

    producer = asyncio.create_task(produce())
    try:
        while True:
            try:
                event = await asyncio.wait_for(channel.queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                yield None
                continue
            yield event
            if isinstance(event.payload, RunCompleted):
                break
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
