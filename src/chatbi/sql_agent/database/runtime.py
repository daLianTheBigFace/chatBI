"""Per-query SQLite authorization, cancellation and bounded result collection."""

import json
import sqlite3
from threading import Event
from time import perf_counter

from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from chatbi.sql_agent.config import AccessConfig, ExecutionConfig
from chatbi.sql_agent.database.constants import (
    BINARY_PREFIX,
    EXPLAIN_PREFIX,
    MAIN_DATABASE,
    MAX_SQLITE_COLUMNS,
    MAX_SQLITE_EXPRESSION_DEPTH,
    MILLISECONDS_PER_SECOND,
    MIN_SQLITE_VALUE_LIMIT,
    QUERY_DENIED,
    QUERY_FAILED,
    QUERY_TIMEOUT,
    RESULT_SERIALIZATION_FAILED,
    RESULT_TOO_LARGE,
    SCHEMA_PRELOAD_SQL,
)
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import DatabaseSchema, QueryResult, SQLQuery, SQLValue

JSON_OPTIONS = {"ensure_ascii": False, "separators": (",", ":"), "allow_nan": False}
TEXT_ENCODING = "utf-8"
JSON_ROW_SEPARATOR_SIZE = 1
QUERY_ALLOWED_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE})


class SQLiteQueryRuntime:
    def __init__(
        self,
        config: ExecutionConfig,
        access: AccessConfig,
        schema: DatabaseSchema,
        cancelled: Event,
    ) -> None:
        self._config = config
        self._columns = {
            table.name.casefold(): {column.name.casefold() for column in table.columns}
            for table in schema.tables
        }
        self._functions = {name.casefold() for name in access.allowed_functions}
        self._cancelled = cancelled
        self._denied = False
        self._started_at = perf_counter()
        self._deadline = self._started_at + config.query_timeout_seconds

    def _authorize(self, action, first, second, database, origin) -> int:
        permitted = False
        if action in QUERY_ALLOWED_ACTIONS:
            permitted = True
        elif action == sqlite3.SQLITE_READ:
            table = (first or "").casefold()
            column = (second or "").casefold()
            permitted = (
                (database == MAIN_DATABASE or (database is None and not column))
                and table in self._columns
                and (not column or column in self._columns[table])
            )
        elif action == sqlite3.SQLITE_FUNCTION:
            permitted = (second or "").casefold() in self._functions
        if not permitted:
            self._denied = True
        return sqlite3.SQLITE_OK if permitted else sqlite3.SQLITE_DENY

    def _interrupted(self) -> bool:
        return self._cancelled.is_set() or perf_counter() >= self._deadline

    def _check_budget(self) -> None:
        if self._interrupted():
            raise SQLAgentError(ErrorCode.TIMEOUT, QUERY_TIMEOUT)

    @staticmethod
    def _serialize_cell(value: object) -> SQLValue:
        if isinstance(value, bytes):
            return BINARY_PREFIX + value.hex()
        if value is None or isinstance(value, str | int | float | bool):
            return value
        return str(value)

    @staticmethod
    def _json_size(value: object) -> int:
        try:
            return len(json.dumps(value, **JSON_OPTIONS).encode(TEXT_ENCODING))
        except (ValueError, UnicodeError) as exc:
            raise SQLAgentError(ErrorCode.EXECUTION, RESULT_SERIALIZATION_FAILED) from exc

    def run(self, connection: Connection, query: SQLQuery, row_limit: int) -> QueryResult:
        raw = connection.connection.driver_connection
        # Load schema before setting a small per-value memory bound.
        # The main schema is service metadata, never supplied by the model.
        with connection.exec_driver_sql(SCHEMA_PRELOAD_SQL):
            pass
        raw.setlimit(
            sqlite3.SQLITE_LIMIT_LENGTH,
            max(self._config.max_result_bytes, MIN_SQLITE_VALUE_LIMIT),
        )
        raw.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_SQLITE_COLUMNS)
        raw.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, MAX_SQLITE_EXPRESSION_DEPTH)
        raw.setlimit(
            sqlite3.SQLITE_LIMIT_SQL_LENGTH,
            self._config.max_sql_characters + len(EXPLAIN_PREFIX),
        )
        raw.set_authorizer(self._authorize)
        raw.set_progress_handler(self._interrupted, self._config.progress_interval)
        try:
            self._check_budget()
            # Preparation catches SQLite dialect and authorization errors before execution.
            with connection.exec_driver_sql(EXPLAIN_PREFIX + query.sql, query.parameters):
                pass
            self._check_budget()
            with connection.exec_driver_sql(query.sql, query.parameters) as result:
                columns = list(result.keys())
                size = self._json_size({"columns": columns, "rows": []})
                if size > self._config.max_result_bytes:
                    raise SQLAgentError(ErrorCode.BUDGET_EXHAUSTED, RESULT_TOO_LARGE)
                rows = []
                truncated = False
                for row in result:
                    self._check_budget()
                    if len(rows) >= row_limit:
                        truncated = True
                        break
                    values = [self._serialize_cell(value) for value in row]
                    size += self._json_size(values) + (JSON_ROW_SEPARATOR_SIZE if rows else 0)
                    if size > self._config.max_result_bytes:
                        truncated = True
                        break
                    rows.append(values)
            self._check_budget()
            return QueryResult(
                columns=columns,
                rows=rows,
                truncated=truncated,
                execution_time_ms=(perf_counter() - self._started_at) * MILLISECONDS_PER_SECOND,
            )
        except DBAPIError as exc:
            if self._denied:
                raise SQLAgentError(ErrorCode.ACCESS_DENIED, QUERY_DENIED) from exc
            code = getattr(exc.orig, "sqlite_errorcode", None)
            if self._interrupted() or code in {sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY}:
                raise SQLAgentError(ErrorCode.TIMEOUT, QUERY_TIMEOUT) from exc
            if code == sqlite3.SQLITE_TOOBIG:
                raise SQLAgentError(ErrorCode.BUDGET_EXHAUSTED, RESULT_TOO_LARGE) from exc
            raise SQLAgentError(ErrorCode.EXECUTION, QUERY_FAILED, repairable=True) from exc
        finally:
            raw.set_authorizer(None)
            raw.set_progress_handler(None, 0)
