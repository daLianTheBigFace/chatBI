"""Stable Python service boundary with whole-request budgets and sanitized failures."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import date
from time import perf_counter
from uuid import uuid4

from chatbi.sql_agent.config import WorkflowConfig
from chatbi.sql_agent.enums import ErrorCode, ResponseStatus, WorkflowStage
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.events import (
    ChatEvent,
    ChatRequest,
    StreamingAnswerStrategy,
    WorkflowObserver,
)
from chatbi.sql_agent.models import (
    RunMetadata,
    SQLAgentRequest,
    SQLAgentResponse,
    ValidationIssue,
)
from chatbi.sql_agent.redaction import ConservativeDisplayRedaction, DisplayRedactionStrategy
from chatbi.sql_agent.state import SQLAgentState
from chatbi.sql_agent.summary import (
    ANSWER_TOO_LONG,
    MAX_ANSWER_CHARACTERS,
    SUMMARY_FALLBACK_WARNING,
    AnswerSynthesisStrategy,
    SummaryAnswer,
    basic_answer,
)
from chatbi.sql_agent.workflow import SQLAgentWorkflow

MILLISECONDS_PER_SECOND = 1000
REQUEST_TIMEOUT = "SQL Agent request exceeded its overall time budget"
UNEXPECTED_FAILURE = "SQL Agent request failed unexpectedly"


class SQLAgentService:
    def __init__(
        self,
        workflow: SQLAgentWorkflow,
        config: WorkflowConfig,
        reference_date_provider: Callable[[], date] = date.today,
        *,
        synthesis: AnswerSynthesisStrategy | None = None,
        display_factory: Callable[[], DisplayRedactionStrategy] = ConservativeDisplayRedaction,
    ) -> None:
        self._workflow = workflow
        self._config = config
        self._reference_date_provider = reference_date_provider
        self._synthesis = synthesis
        self._display_factory = display_factory

    async def ask(self, request: SQLAgentRequest) -> SQLAgentResponse:
        return await self._ask(request)

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatEvent | None]:
        from chatbi.sql_agent.streaming import stream_response

        return stream_response(self, request, display_factory=self._display_factory)

    async def _ask(
        self,
        request: SQLAgentRequest,
        *,
        request_id: str | None = None,
        observer: WorkflowObserver | None = None,
        display: DisplayRedactionStrategy | None = None,
        answer_sink: Callable[[str], Awaitable[None]] | None = None,
    ) -> SQLAgentResponse:
        request = SQLAgentRequest.model_validate(request.model_dump())
        state = SQLAgentState(
            request_id=request_id if request_id is not None else str(uuid4()),
            request=request,
            reference_date=self._reference_date_provider(),
            observer=observer,
        )
        response = None
        try:
            async with asyncio.timeout(self._config.request_timeout_seconds):
                response = await self._workflow.run(state)
                if response.status == ResponseStatus.SUCCESS:
                    summary_result = (
                        display.result(response.result)
                        if display is not None
                        else response.result.model_copy(deep=True)
                    )
                    summary_question = (
                        display.text(state.model_question) if display else state.model_question
                    )
                    response.answer = basic_answer(summary_result)
                    await state.progress(WorkflowStage.SYNTHESIS)
                    if self._synthesis is not None:
                        if answer_sink is not None and isinstance(
                            self._synthesis, StreamingAnswerStrategy
                        ):
                            parts = []
                            answer_length = 0
                            async with aclosing(
                                self._synthesis.stream_synthesize(summary_question, summary_result)
                            ) as chunks:
                                async for chunk in chunks:
                                    answer_length += len(chunk)
                                    if answer_length > MAX_ANSWER_CHARACTERS:
                                        raise SQLAgentError(ErrorCode.MODEL_OUTPUT, ANSWER_TOO_LONG)
                                    parts.append(chunk)
                                    await answer_sink(chunk)
                            answer = "".join(parts)
                        else:
                            answer = await self._synthesis.synthesize(
                                summary_question, summary_result
                            )
                        if len(answer) > MAX_ANSWER_CHARACTERS:
                            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, ANSWER_TOO_LONG)
                        response.answer = SummaryAnswer(answer=answer).answer
        except TimeoutError:
            response = self._fallback_or_failure(
                response, state, ErrorCode.TIMEOUT, REQUEST_TIMEOUT
            )
        except SQLAgentError as exc:
            response = self._fallback_or_failure(
                response, state, exc.code, str(exc), repairable=exc.repairable
            )
        except Exception:
            response = self._fallback_or_failure(
                response, state, ErrorCode.INTERNAL, UNEXPECTED_FAILURE
            )
        # CancelledError deliberately propagates; callers cancelling do not get a fake response.
        response.metadata = RunMetadata(
            execution_count=state.execution_count,
            repair_count=state.repair_count,
            elapsed_ms=(perf_counter() - state.started_at) * MILLISECONDS_PER_SECOND,
        )
        return response

    @classmethod
    def _fallback_or_failure(
        cls,
        response: SQLAgentResponse | None,
        state: SQLAgentState,
        code: ErrorCode,
        message: str,
        *,
        repairable: bool = False,
    ) -> SQLAgentResponse:
        if response is not None and response.status == ResponseStatus.SUCCESS:
            response.warnings.append(SUMMARY_FALLBACK_WARNING)
            return response
        return cls._failure(state, code, message, repairable=repairable)

    @staticmethod
    def _failure(
        state: SQLAgentState, code: ErrorCode, message: str, *, repairable: bool = False
    ) -> SQLAgentResponse:
        issue = ValidationIssue(code=code, message=message, repairable=repairable)
        if state.attempts and state.attempts[-1].error is None:
            state.attempts[-1].error = issue
        return SQLAgentResponse(
            request_id=state.request_id,
            status=ResponseStatus.FAILED,
            query=state.query,
            error=issue,
        )
