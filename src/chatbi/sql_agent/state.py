"""Per-request state; never retained on shared service instances or logged by default."""

import json
from dataclasses import dataclass, field
from datetime import date
from time import perf_counter

from chatbi.sql_agent.agent_models import ParsedQuery
from chatbi.sql_agent.enums import WorkflowStage
from chatbi.sql_agent.events import WorkflowObserver
from chatbi.sql_agent.models import (
    DatabaseSchema,
    QueryResult,
    SQLAgentRequest,
    SQLQuery,
    ValidationIssue,
    ValidationResult,
)


@dataclass
class QueryAttempt:
    query: SQLQuery
    validation: ValidationResult | None = None
    error: ValidationIssue | None = None
    result: QueryResult | None = None


@dataclass
class SQLAgentState:
    request_id: str
    request: SQLAgentRequest
    reference_date: date
    started_at: float = field(default_factory=perf_counter)
    stage: WorkflowStage = WorkflowStage.SCHEMA
    parsed_query: ParsedQuery | None = None
    selected_schema: DatabaseSchema | None = None
    query: SQLQuery | None = None
    query_result: QueryResult | None = None
    repair_count: int = 0
    execution_count: int = 0
    attempts: list[QueryAttempt] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    observer: WorkflowObserver | None = None

    @property
    def model_question(self) -> str:
        if not self.request.history:
            return self.request.question
        return json.dumps(
            {
                "conversation_history": [item.model_dump() for item in self.request.history],
                "current_question": self.request.question,
            },
            ensure_ascii=False,
        )

    async def progress(self, stage: WorkflowStage) -> None:
        self.stage = stage
        if self.observer is not None:
            await self.observer.progress(stage)
