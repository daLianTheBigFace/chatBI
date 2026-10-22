"""Validated data exchanged across the service, strategies and database layer."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from chatbi.sql_agent.enums import ErrorCode, ResponseStatus

type SQLValue = str | int | float | bool | None
type NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SQLAgentRequest(Contract):
    question: NonEmptyText
    session_id: str | None = None


class SQLQuery(Contract):
    sql: NonEmptyText
    parameters: dict[str, SQLValue] = Field(default_factory=dict)


class ColumnSchema(Contract):
    name: str
    data_type: str
    nullable: bool
    primary_key: bool = False


class ForeignKeySchema(Contract):
    columns: list[str]
    referred_table: str
    referred_columns: list[str]


class TableSchema(Contract):
    name: str
    columns: list[ColumnSchema]
    foreign_keys: list[ForeignKeySchema] = Field(default_factory=list)


class DatabaseSchema(Contract):
    tables: list[TableSchema]


class ValidationIssue(Contract):
    code: ErrorCode
    message: str
    repairable: bool = False


class ValidationResult(Contract):
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues


class QueryResult(Contract):
    # Position in columns identifies each cell, including duplicate column names.
    columns: list[str]
    rows: list[list[SQLValue]]
    truncated: bool = False
    execution_time_ms: float = Field(ge=0)

    @property
    def returned_row_count(self) -> int:
        return len(self.rows)

    @model_validator(mode="after")
    def check_row_width(self) -> Self:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Row width must match the number of columns")
        return self


class RunMetadata(Contract):
    execution_count: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0)
    elapsed_ms: float = Field(default=0, ge=0)


class SQLAgentResponse(Contract):
    request_id: str
    status: ResponseStatus
    answer: str | None = None
    query: SQLQuery | None = None
    result: QueryResult | None = None
    clarification_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: ValidationIssue | None = None
    metadata: RunMetadata = Field(default_factory=RunMetadata)

    @model_validator(mode="after")
    def check_status_payload(self) -> Self:
        if self.status == ResponseStatus.SUCCESS:
            if self.query is None or self.result is None:
                raise ValueError("Success requires a query and result")
            if self.error is not None or self.clarification_questions:
                raise ValueError("Success cannot include errors or clarification questions")
        elif self.status == ResponseStatus.NEEDS_CLARIFICATION:
            if not self.clarification_questions:
                raise ValueError("Clarification requires at least one question")
            if self.result is not None or self.error is not None:
                raise ValueError("Clarification cannot include a result or error")
        elif self.status == ResponseStatus.FAILED:
            if self.error is None:
                raise ValueError("Failure requires an error")
            if self.result is not None or self.clarification_questions:
                raise ValueError("Failure cannot include a result or clarification questions")
        return self
