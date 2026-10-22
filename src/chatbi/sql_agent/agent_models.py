"""Strict structured outputs for LLM stages, separate from database contracts."""

import re
from typing import Self

from pydantic import Field, model_validator

from chatbi.sql_agent.enums import UnderstandingStatus
from chatbi.sql_agent.models import Contract, NonEmptyText, SQLQuery, SQLValue

PARAMETER_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
INVALID_UNDERSTANDING = "Ready output needs tables; clarification output needs questions"
INVALID_PARAMETER_NAMES = "SQL parameter names must be valid and unique"


class ParsedQuery(Contract):
    status: UnderstandingStatus
    selected_tables: list[NonEmptyText]
    metrics: list[str]
    dimensions: list[str]
    filters: list[str]
    time_range: str | None
    ordering: list[str]
    top_n: int | None = Field(ge=1)
    assumptions: list[str]
    clarification_questions: list[NonEmptyText]

    @model_validator(mode="after")
    def check_status(self) -> Self:
        if self.status == UnderstandingStatus.READY:
            if not self.selected_tables or self.clarification_questions:
                raise ValueError(INVALID_UNDERSTANDING)
        elif not self.clarification_questions:
            raise ValueError(INVALID_UNDERSTANDING)
        return self


class SQLParameter(Contract):
    name: NonEmptyText
    value: SQLValue


class GeneratedSQL(Contract):
    sql: NonEmptyText
    # Lists avoid arbitrary JSON object keys in strict structured-output schemas.
    parameters: list[SQLParameter]
    assumptions: list[str]

    @model_validator(mode="after")
    def check_parameters(self) -> Self:
        names = [parameter.name for parameter in self.parameters]
        if len(set(names)) != len(names) or any(
            not PARAMETER_NAME_PATTERN.fullmatch(name) for name in names
        ):
            raise ValueError(INVALID_PARAMETER_NAMES)
        return self

    def as_query(self) -> SQLQuery:
        return SQLQuery(
            sql=self.sql,
            parameters={parameter.name: parameter.value for parameter in self.parameters},
        )
