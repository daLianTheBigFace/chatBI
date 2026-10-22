"""Question understanding and candidate SQL generation, with no database execution."""

import json
from datetime import date

from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery
from chatbi.sql_agent.agents.contracts import StructuredAgentRunner
from chatbi.sql_agent.config import AccessConfig
from chatbi.sql_agent.enums import ErrorCode, UnderstandingStatus
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import DatabaseSchema, SQLAgentRequest, SQLQuery, ValidationIssue
from chatbi.sql_agent.prompts import (
    GENERATION_AGENT_NAME,
    GENERATION_PROMPT,
    PROMPT_VERSION,
    REPAIR_AGENT_NAME,
    REPAIR_PROMPT,
    UNDERSTANDING_AGENT_NAME,
    UNDERSTANDING_PROMPT,
)

UNAVAILABLE_SELECTION = "Model selected tables outside the supplied schema"
CLARIFICATION_REQUIRED = "Question requires clarification before SQL generation"
SCHEMA_SELECTION_MISMATCH = "Selected schema must match the understood table selection"


class QueryUnderstandingStage:
    def __init__(self, runner: StructuredAgentRunner) -> None:
        self._runner = runner

    async def parse(
        self, question: str, schema: DatabaseSchema, reference_date: date
    ) -> ParsedQuery:
        request = SQLAgentRequest(question=question)
        user_input = json.dumps(
            {
                "question": request.question,
                "schema": schema.model_dump(mode="json"),
                "reference_date": reference_date.isoformat(),
                "prompt_version": PROMPT_VERSION,
            },
            ensure_ascii=False,
        )
        output = await self._runner.run(
            name=UNDERSTANDING_AGENT_NAME,
            instructions=UNDERSTANDING_PROMPT,
            user_input=user_input,
            output_type=ParsedQuery,
        )
        allowed = {table.name.casefold() for table in schema.tables}
        if any(name.casefold() not in allowed for name in output.selected_tables):
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, UNAVAILABLE_SELECTION)
        return output


class ExplicitTableSelectionStrategy:
    """Select existing table definitions; never trust model-generated schema content."""

    async def select(self, query: ParsedQuery, schema: DatabaseSchema) -> DatabaseSchema:
        if query.status != UnderstandingStatus.READY:
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, CLARIFICATION_REQUIRED)
        tables = {table.name.casefold(): table for table in schema.tables}
        names = {name.casefold() for name in query.selected_tables}
        if not names or not names <= tables.keys():
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, UNAVAILABLE_SELECTION)
        selected = [table.model_copy(deep=True) for name, table in tables.items() if name in names]
        for table in selected:
            table.foreign_keys = [
                key for key in table.foreign_keys if key.referred_table.casefold() in names
            ]
        return DatabaseSchema(tables=selected)


class SQLGenerationStage:
    def __init__(self, runner: StructuredAgentRunner, access_config: AccessConfig) -> None:
        self._runner = runner
        self._access = access_config

    async def generate(
        self,
        question: str,
        parsed_query: ParsedQuery,
        schema: DatabaseSchema,
        reference_date: date,
    ) -> GeneratedSQL:
        request = SQLAgentRequest(question=question)
        if parsed_query.status != UnderstandingStatus.READY:
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, CLARIFICATION_REQUIRED)
        if {name.casefold() for name in parsed_query.selected_tables} != {
            table.name.casefold() for table in schema.tables
        }:
            raise SQLAgentError(ErrorCode.MODEL_OUTPUT, SCHEMA_SELECTION_MISMATCH)
        user_input = json.dumps(
            {
                "question": request.question,
                "parsed_query": parsed_query.model_dump(mode="json"),
                "schema": schema.model_dump(mode="json"),
                "reference_date": reference_date.isoformat(),
                "allowed_functions": sorted(self._access.allowed_functions),
                "prompt_version": PROMPT_VERSION,
            },
            ensure_ascii=False,
        )
        return await self._runner.run(
            name=GENERATION_AGENT_NAME,
            instructions=GENERATION_PROMPT,
            user_input=user_input,
            output_type=GeneratedSQL,
        )


class SQLRepairStage:
    def __init__(self, runner: StructuredAgentRunner, access_config: AccessConfig) -> None:
        self._runner = runner
        self._access = access_config

    async def repair(
        self,
        question: str,
        parsed_query: ParsedQuery,
        schema: DatabaseSchema,
        previous_query: SQLQuery,
        error: ValidationIssue,
        reference_date: date,
    ) -> GeneratedSQL:
        user_input = json.dumps(
            {
                "question": question,
                "parsed_query": parsed_query.model_dump(mode="json"),
                "schema": schema.model_dump(mode="json"),
                "previous_query": previous_query.model_dump(mode="json"),
                "error": error.model_dump(mode="json"),
                "reference_date": reference_date.isoformat(),
                "allowed_functions": sorted(self._access.allowed_functions),
                "prompt_version": PROMPT_VERSION,
            },
            ensure_ascii=False,
        )
        return await self._runner.run(
            name=REPAIR_AGENT_NAME,
            instructions=REPAIR_PROMPT,
            user_input=user_input,
            output_type=GeneratedSQL,
        )
