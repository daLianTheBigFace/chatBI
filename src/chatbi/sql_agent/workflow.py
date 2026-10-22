"""Fixed orchestration with request-local state and bounded repair loops."""

from functools import partial

from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery
from chatbi.sql_agent.config import WorkflowConfig
from chatbi.sql_agent.database.executor import run_in_worker
from chatbi.sql_agent.database.service import DatabaseService
from chatbi.sql_agent.enums import ErrorCode, ResponseStatus, UnderstandingStatus, WorkflowStage
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import DatabaseSchema, SQLAgentResponse, ValidationIssue
from chatbi.sql_agent.repair import REPAIRABLE_CODES
from chatbi.sql_agent.state import QueryAttempt, SQLAgentState
from chatbi.sql_agent.strategies import (
    QueryUnderstandingStrategy,
    SchemaSelectionStrategy,
    SQLGenerationStrategy,
    SQLRepairPolicy,
    SQLRepairStrategy,
    SQLValidationPolicy,
)

REPAIR_BUDGET_EXHAUSTED = "SQL repair budget exhausted"
EXECUTION_BUDGET_EXHAUSTED = "SQL execution budget exhausted"
TRUNCATED_WARNING = "查询结果已截断，仅返回预算范围内的数据。"
ASSUMPTION_PREFIX = "假设："


class SQLAgentWorkflow:
    def __init__(
        self,
        *,
        database: DatabaseService,
        understanding: QueryUnderstandingStrategy,
        selection: SchemaSelectionStrategy,
        generation: SQLGenerationStrategy,
        repair: SQLRepairStrategy,
        validation: SQLValidationPolicy,
        repair_policy: SQLRepairPolicy,
        config: WorkflowConfig,
    ) -> None:
        self._database = database
        self._understanding = understanding
        self._selection = selection
        self._generation = generation
        self._repair = repair
        self._validation = validation
        self._repair_policy = repair_policy
        self._config = config

    async def run(self, state: SQLAgentState) -> SQLAgentResponse:
        await state.progress(WorkflowStage.SCHEMA)
        schema = await run_in_worker(self._database.get_schema)
        await state.progress(WorkflowStage.UNDERSTANDING)
        parsed = await self._understanding.parse(state.model_question, schema, state.reference_date)
        state.parsed_query = parsed
        if parsed.status == UnderstandingStatus.NEEDS_CLARIFICATION:
            return SQLAgentResponse(
                request_id=state.request_id,
                status=ResponseStatus.NEEDS_CLARIFICATION,
                clarification_questions=parsed.clarification_questions,
            )
        await state.progress(WorkflowStage.SELECTION)
        selected = await self._selection.select(parsed, schema)
        state.selected_schema = selected
        await state.progress(WorkflowStage.GENERATION)
        candidate = await self._generation.generate(
            state.model_question, parsed, selected, state.reference_date
        )
        return await self._run_candidates(state, parsed, selected, candidate)

    async def _run_candidates(
        self,
        state: SQLAgentState,
        parsed: ParsedQuery,
        schema: DatabaseSchema,
        candidate: GeneratedSQL,
    ) -> SQLAgentResponse:
        while True:
            query = candidate.as_query()
            state.query = query
            state.assumptions = list(dict.fromkeys(parsed.assumptions + candidate.assumptions))
            attempt = QueryAttempt(query=query.model_copy(deep=True))
            state.attempts.append(attempt)
            tool_call_id = None
            if state.observer is not None:
                tool_call_id = await state.observer.sql_started(len(state.attempts), query)
            await state.progress(WorkflowStage.VALIDATION)
            validation = await run_in_worker(partial(self._validation.validate, query, schema))
            attempt.validation = validation
            if not validation.valid:
                issue = validation.issues[0]
            else:
                if state.execution_count >= self._config.max_executions:
                    raise SQLAgentError(ErrorCode.BUDGET_EXHAUSTED, EXECUTION_BUDGET_EXHAUSTED)
                await state.progress(WorkflowStage.EXECUTION)
                if state.observer is not None:
                    await state.observer.sql_executing(tool_call_id)
                state.execution_count += 1
                try:
                    result = await self._database.execute_query(query)
                except SQLAgentError as exc:
                    issue = ValidationIssue(
                        code=exc.code, message=str(exc), repairable=exc.repairable
                    )
                else:
                    attempt.result = result
                    state.query_result = result
                    if state.observer is not None:
                        await state.observer.sql_completed(tool_call_id, result)
                    state.stage = WorkflowStage.COMPLETED
                    warnings = [ASSUMPTION_PREFIX + item for item in state.assumptions]
                    if result.truncated:
                        warnings.append(TRUNCATED_WARNING)
                    return SQLAgentResponse(
                        request_id=state.request_id,
                        status=ResponseStatus.SUCCESS,
                        query=query,
                        result=result,
                        warnings=warnings,
                    )
            attempt.error = issue
            if state.observer is not None:
                await state.observer.sql_failed(
                    tool_call_id, "validation" if not validation.valid else "execution", issue.code
                )
            self._check_repair_budget(state, issue)
            await state.progress(WorkflowStage.REPAIR)
            state.repair_count += 1
            candidate = await self._repair.repair(
                state.model_question, parsed, schema, query, issue, state.reference_date
            )

    def _check_repair_budget(self, state: SQLAgentState, issue: ValidationIssue) -> None:
        # Policy injection must never enable retries of access violations or timeouts.
        if not issue.repairable or issue.code not in REPAIRABLE_CODES:
            raise SQLAgentError(issue.code, issue.message, repairable=issue.repairable)
        if state.execution_count >= self._config.max_executions:
            raise SQLAgentError(ErrorCode.BUDGET_EXHAUSTED, EXECUTION_BUDGET_EXHAUSTED)
        if state.repair_count >= self._config.max_repairs:
            raise SQLAgentError(ErrorCode.BUDGET_EXHAUSTED, REPAIR_BUDGET_EXHAUSTED)
        if not self._repair_policy.should_repair(issue, state.repair_count):
            raise SQLAgentError(issue.code, issue.message, repairable=issue.repairable)
