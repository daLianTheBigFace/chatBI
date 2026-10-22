"""Versioned UI events: public progress and display data, never private reasoning."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import Field

from chatbi.sql_agent.enums import ResponseStatus, WorkflowStage
from chatbi.sql_agent.models import (
    Contract,
    ConversationContext,
    NonEmptyText,
    QueryResult,
    RunMetadata,
    SQLQuery,
)

PROTOCOL_VERSION = "1.0"


class ChatRequest(ConversationContext):
    question: NonEmptyText = Field(max_length=8000)
    conversation_id: UUID | None = None
    user_message_id: UUID = Field(default_factory=uuid4)


class RunStarted(Contract):
    type: Literal["run.started"] = "run.started"
    tag: Literal["lifecycle"] = "lifecycle"
    user_message_id: UUID


class Progress(Contract):
    type: Literal["progress"] = "progress"
    tag: Literal["progress"] = "progress"
    stage: WorkflowStage
    text: str


class DisplaySQL(Contract):
    sql: str
    parameter_names: list[str]
    redacted: Literal[True] = True


class SQLStarted(Contract):
    type: Literal["sql.started"] = "sql.started"
    tag: Literal["sql"] = "sql"
    tool_call_id: UUID
    attempt: int = Field(ge=1)
    query: DisplaySQL
    phase: Literal["validation"] = "validation"


class SQLExecuting(Contract):
    type: Literal["sql.executing"] = "sql.executing"
    tag: Literal["sql"] = "sql"
    tool_call_id: UUID


class SQLCompleted(Contract):
    type: Literal["sql.completed"] = "sql.completed"
    tag: Literal["table"] = "table"
    tool_call_id: UUID
    result: QueryResult
    redacted: Literal[True] = True


class SQLFailed(Contract):
    type: Literal["sql.failed"] = "sql.failed"
    tag: Literal["sql"] = "sql"
    tool_call_id: UUID
    phase: Literal["validation", "execution"]
    code: str
    text: str


class AnswerStarted(Contract):
    type: Literal["answer.started"] = "answer.started"
    tag: Literal["answer"] = "answer"
    block_id: UUID
    format: Literal["plain_text"] = "plain_text"


class AnswerDelta(Contract):
    type: Literal["answer.delta"] = "answer.delta"
    tag: Literal["answer"] = "answer"
    block_id: UUID
    delta: str


class AnswerCompleted(Contract):
    type: Literal["answer.completed"] = "answer.completed"
    tag: Literal["answer"] = "answer"
    block_id: UUID
    text: str
    replace: Literal[True] = True


class WarningEvent(Contract):
    type: Literal["warning"] = "warning"
    tag: Literal["warning"] = "warning"
    text: str


class RunError(Contract):
    type: Literal["run.error"] = "run.error"
    tag: Literal["error"] = "error"
    code: str
    text: str


class RunCompleted(Contract):
    type: Literal["run.completed"] = "run.completed"
    tag: Literal["lifecycle"] = "lifecycle"
    status: ResponseStatus
    clarification_questions: list[str] = Field(default_factory=list)
    metadata: RunMetadata


type EventPayload = Annotated[
    RunStarted
    | Progress
    | SQLStarted
    | SQLExecuting
    | SQLCompleted
    | SQLFailed
    | AnswerStarted
    | AnswerDelta
    | AnswerCompleted
    | WarningEvent
    | RunError
    | RunCompleted,
    Field(discriminator="type"),
]


class ChatEvent(Contract):
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    conversation_id: UUID
    user_message_id: UUID
    message_id: UUID
    role: Literal["assistant"] = "assistant"
    run_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: EventPayload


class WorkflowObserver(Protocol):
    async def progress(self, stage: WorkflowStage) -> None: ...
    async def sql_started(self, attempt: int, query: SQLQuery) -> UUID: ...
    async def sql_executing(self, tool_call_id: UUID) -> None: ...
    async def sql_completed(self, tool_call_id: UUID, result: QueryResult) -> None: ...
    async def sql_failed(self, tool_call_id: UUID, phase: str, code: str) -> None: ...


@runtime_checkable
class StreamingAnswerStrategy(Protocol):
    def stream_synthesize(self, question: str, result: QueryResult) -> AsyncIterator[str]: ...
