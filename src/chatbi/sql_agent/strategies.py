"""Structural interfaces; callers inject implementations without inheritance."""

from datetime import date
from typing import Protocol

from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery
from chatbi.sql_agent.models import DatabaseSchema, SQLQuery, ValidationIssue, ValidationResult


class SchemaSelectionStrategy(Protocol):
    async def select(self, query: ParsedQuery, schema: DatabaseSchema) -> DatabaseSchema: ...


class SQLValidationPolicy(Protocol):
    def validate(self, query: SQLQuery, schema: DatabaseSchema) -> ValidationResult: ...


class SQLRepairPolicy(Protocol):
    def should_repair(self, issue: ValidationIssue, repair_count: int) -> bool: ...


class QueryUnderstandingStrategy(Protocol):
    async def parse(
        self, question: str, schema: DatabaseSchema, reference_date: date
    ) -> ParsedQuery: ...


class SQLGenerationStrategy(Protocol):
    async def generate(
        self, question: str, parsed_query: ParsedQuery, schema: DatabaseSchema, reference_date: date
    ) -> GeneratedSQL: ...


class SQLRepairStrategy(Protocol):
    async def repair(
        self,
        question: str,
        parsed_query: ParsedQuery,
        schema: DatabaseSchema,
        previous_query: SQLQuery,
        error: ValidationIssue,
        reference_date: date,
    ) -> GeneratedSQL: ...
