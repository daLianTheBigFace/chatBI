"""Composable SQL AST rules. SQLite authorization remains a separate hard boundary."""

import re
from dataclasses import dataclass
from typing import Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from chatbi.sql_agent.config import AccessConfig, ExecutionConfig
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.models import DatabaseSchema, SQLQuery, ValidationIssue, ValidationResult

SQLITE_DIALECT = "sqlite"
NAMED_PARAMETER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
PARSE_FAILURE = "SQL must be one parseable SQLite query"
QUERY_ONLY = "Only a single SELECT or WITH SELECT query is permitted"
SOURCE_DENIED = "Query references an unavailable table or database"
COLUMN_INVALID = "Query contains unknown, ambiguous or unavailable columns"
PARAMETER_INVALID = "Provide exactly the named :parameters used in the query"
SQL_TOO_LONG = "SQL exceeds the configured character budget"
FORBIDDEN_NODES = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock)


@dataclass(frozen=True)
class ValidationContext:
    query: SQLQuery
    tree: exp.Expression
    schema: DatabaseSchema


class ValidationRule(Protocol):
    def check(self, context: ValidationContext) -> ValidationIssue | None: ...


class ReadOnlyRule:
    def check(self, context: ValidationContext) -> ValidationIssue | None:
        tree = context.tree
        if not isinstance(tree, exp.Query) or any(
            isinstance(node, FORBIDDEN_NODES) for node in tree.walk()
        ):
            return ValidationIssue(code=ErrorCode.ACCESS_DENIED, message=QUERY_ONLY)
        return None


class SchemaRule:
    def check(self, context: ValidationContext) -> ValidationIssue | None:
        tables = {table.name.casefold(): table for table in context.schema.tables}
        for scope in traverse_scope(context.tree):
            for source in scope.sources.values():
                if isinstance(source, exp.Table) and (
                    not isinstance(source.this, exp.Identifier)
                    or source.name.casefold() not in tables
                    or source.db
                    or source.catalog
                ):
                    return ValidationIssue(code=ErrorCode.ACCESS_DENIED, message=SOURCE_DENIED)
        try:
            qualify(
                context.tree.copy(),
                dialect=SQLITE_DIALECT,
                schema={
                    name: {column.name.casefold(): column.data_type for column in table.columns}
                    for name, table in tables.items()
                },
                validate_qualify_columns=True,
            )
        except OptimizeError:
            return ValidationIssue(
                code=ErrorCode.INVALID_SQL, message=COLUMN_INVALID, repairable=True
            )
        return None


class ParameterRule:
    def check(self, context: ValidationContext) -> ValidationIssue | None:
        placeholders = list(context.tree.find_all(exp.Placeholder))
        names = {placeholder.name for placeholder in placeholders}
        if (
            any(not NAMED_PARAMETER_PATTERN.fullmatch(name) for name in names)
            or any(context.tree.find_all(exp.Parameter))
            or names != set(context.query.parameters)
        ):
            return ValidationIssue(
                code=ErrorCode.INVALID_SQL, message=PARAMETER_INVALID, repairable=True
            )
        return None


class CompositeSQLValidationPolicy:
    def __init__(self, config: ExecutionConfig, rules: tuple[ValidationRule, ...]) -> None:
        self._config = config
        self._rules = rules

    def validate(self, query: SQLQuery, schema: DatabaseSchema) -> ValidationResult:
        if len(query.sql) > self._config.max_sql_characters:
            return ValidationResult(
                issues=[ValidationIssue(code=ErrorCode.INVALID_SQL, message=SQL_TOO_LONG)]
            )
        try:
            statements = sqlglot.parse(query.sql, read=SQLITE_DIALECT)
            if len(statements) != 1 or statements[0] is None:
                return ValidationResult(
                    issues=[ValidationIssue(code=ErrorCode.ACCESS_DENIED, message=QUERY_ONLY)]
                )
            context = ValidationContext(query=query, tree=statements[0], schema=schema)
            for rule in self._rules:
                if issue := rule.check(context):
                    return ValidationResult(issues=[issue])
        except (ParseError, ValueError, RecursionError):
            return ValidationResult(
                issues=[
                    ValidationIssue(
                        code=ErrorCode.INVALID_SQL, message=PARSE_FAILURE, repairable=True
                    )
                ]
            )
        return ValidationResult()


def default_validation_policy(config: ExecutionConfig) -> CompositeSQLValidationPolicy:
    return CompositeSQLValidationPolicy(config, (ReadOnlyRule(), SchemaRule(), ParameterRule()))


def filter_schema(schema: DatabaseSchema, access: AccessConfig) -> DatabaseSchema:
    allowed_tables = (
        {name.casefold() for name in access.allowed_tables}
        if access.allowed_tables is not None
        else None
    )
    allowed_columns = {
        name.casefold(): {column.casefold() for column in columns}
        for name, columns in access.allowed_columns.items()
    }
    tables = []
    for table in schema.tables:
        name = table.name.casefold()
        if allowed_tables is not None and name not in allowed_tables:
            continue
        columns = allowed_columns.get(name)
        if columns is not None and not any(
            column.name.casefold() in columns for column in table.columns
        ):
            continue
        tables.append(
            table.model_copy(
                update={
                    "columns": [
                        column
                        for column in table.columns
                        if columns is None or column.name.casefold() in columns
                    ],
                    "foreign_keys": [],
                }
            )
        )
    # Only publish relationships whose endpoints and keys are visible.
    visible = {
        table.name.casefold(): {c.name.casefold() for c in table.columns} for table in tables
    }
    original = {table.name.casefold(): table for table in schema.tables}
    for table in tables:
        table.foreign_keys = [
            key
            for key in original[table.name.casefold()].foreign_keys
            if key.referred_table.casefold() in visible
            and all(c.casefold() in visible[table.name.casefold()] for c in key.columns)
            and all(
                c.casefold() in visible[key.referred_table.casefold()] for c in key.referred_columns
            )
        ]
    return DatabaseSchema(tables=tables)
