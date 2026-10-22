"""SQLite metadata access using native read-only URI connections."""

import sqlite3
from threading import Event

from sqlalchemy import MetaData, Table, create_engine, event, inspect, select
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from chatbi.sql_agent.config import AccessConfig, DatabaseConfig, ExecutionConfig
from chatbi.sql_agent.database.constants import (
    DATABASE_CLOSED,
    DATABASE_NOT_FOUND,
    DATABASE_OPEN_FAILED,
    DATABASE_READ_FAILED,
    EXTENSION_LOADING_ENABLED,
    ORDINARY_TABLE_TYPE,
    QUERY_ONLY_PRAGMA,
    READ_ONLY_URI_SUFFIX,
    SQLITE_DRIVER,
    SQLITE_URI_ENABLED,
    SQLITE_URI_PARAMETER,
    TABLE_LIST_SQL,
    TABLE_NOT_ALLOWED,
)
from chatbi.sql_agent.database.runtime import SQLiteQueryRuntime
from chatbi.sql_agent.database.validator import default_validation_policy, filter_schema
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import (
    ColumnSchema,
    DatabaseSchema,
    ForeignKeySchema,
    QueryResult,
    SQLQuery,
    TableSchema,
    ValidationResult,
)
from chatbi.sql_agent.strategies import SQLValidationPolicy


class SQLiteAdapter:
    def __init__(
        self,
        config: DatabaseConfig,
        execution_config: ExecutionConfig,
        access_config: AccessConfig | None = None,
        validation_policy: SQLValidationPolicy | None = None,
    ) -> None:
        self._config = config
        self._execution_config = execution_config
        self._access = access_config if access_config is not None else AccessConfig()
        self._validation = (
            validation_policy
            if validation_policy is not None
            else default_validation_policy(execution_config)
        )
        self._closed = False
        path = config.resolved_path
        if not path.is_file():
            raise SQLAgentError(ErrorCode.CONFIGURATION, DATABASE_NOT_FOUND)
        # as_uri quotes spaces and URI metacharacters in Windows and POSIX paths.
        url = URL.create(
            drivername=SQLITE_DRIVER,
            database=path.as_uri() + READ_ONLY_URI_SUFFIX,
            query={SQLITE_URI_PARAMETER: SQLITE_URI_ENABLED},
        )
        self._engine = create_engine(
            url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": execution_config.query_timeout_seconds},
        )
        event.listen(self._engine, "connect", self._configure_connection)
        try:
            # Eagerly reject unreadable or invalid databases; never create a file.
            self.get_schema()
        except SQLAgentError as exc:
            self.close()
            raise SQLAgentError(ErrorCode.CONFIGURATION, DATABASE_OPEN_FAILED) from exc

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection, _: object) -> None:
        if hasattr(connection, "enable_load_extension"):
            connection.enable_load_extension(EXTENSION_LOADING_ENABLED)
        cursor = connection.cursor()
        try:
            cursor.execute(QUERY_ONLY_PRAGMA)
        finally:
            cursor.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise SQLAgentError(ErrorCode.CONFIGURATION, DATABASE_CLOSED)

    def get_schema(self) -> DatabaseSchema:
        self._ensure_open()
        try:
            with self._engine.connect() as connection:
                inspector = inspect(connection)
                ordinary_tables = {
                    row.name
                    for row in connection.exec_driver_sql(TABLE_LIST_SQL)
                    if row.type == ORDINARY_TABLE_TYPE
                }
                tables = []
                for name in sorted(inspector.get_table_names()):
                    if name not in ordinary_tables:
                        continue
                    primary_keys = set(inspector.get_pk_constraint(name)["constrained_columns"])
                    columns = [
                        ColumnSchema(
                            name=column["name"],
                            data_type=str(column["type"]),
                            nullable=column["nullable"],
                            primary_key=column["name"] in primary_keys,
                        )
                        for column in inspector.get_columns(name)
                    ]
                    foreign_keys = [
                        ForeignKeySchema(
                            columns=key["constrained_columns"],
                            referred_table=key["referred_table"],
                            referred_columns=key["referred_columns"],
                        )
                        for key in inspector.get_foreign_keys(name)
                    ]
                    tables.append(
                        TableSchema(name=name, columns=columns, foreign_keys=foreign_keys)
                    )
                return filter_schema(DatabaseSchema(tables=tables), self._access)
        except SQLAlchemyError as exc:
            raise SQLAgentError(ErrorCode.EXECUTION, DATABASE_READ_FAILED) from exc

    def get_table_sample(self, table_name: str) -> QueryResult:
        self._ensure_open()
        schema = self.get_schema()
        if table_name not in {table.name for table in schema.tables}:
            raise SQLAgentError(ErrorCode.ACCESS_DENIED, TABLE_NOT_ALLOWED)
        limit = min(self._execution_config.sample_rows, self._execution_config.max_rows)
        try:
            with self._engine.connect() as connection:
                table = Table(table_name, MetaData(), autoload_with=connection)
                visible = next(
                    table_schema
                    for table_schema in schema.tables
                    if table_schema.name == table_name
                )
                statement = select(*(table.c[column.name] for column in visible.columns))
                if table.primary_key.columns:
                    visible_names = {column.name for column in visible.columns}
                    statement = statement.order_by(
                        *(
                            column
                            for column in table.primary_key.columns
                            if column.name in visible_names
                        )
                    )
                query = SQLQuery(
                    sql=str(
                        statement.compile(
                            dialect=self._engine.dialect, compile_kwargs={"literal_binds": True}
                        )
                    )
                )
            return self._execute(query, Event(), limit)
        except SQLAlchemyError as exc:
            raise SQLAgentError(ErrorCode.EXECUTION, DATABASE_READ_FAILED) from exc

    def validate_query(self, query: SQLQuery) -> ValidationResult:
        self._ensure_open()
        return self._validation.validate(query, self.get_schema())

    def execute_query(self, query: SQLQuery, cancelled: Event) -> QueryResult:
        return self._execute(query, cancelled, self._execution_config.max_rows)

    def _execute(self, query: SQLQuery, cancelled: Event, row_limit: int) -> QueryResult:
        self._ensure_open()
        query = query.model_copy(deep=True)
        schema = self.get_schema()
        validation = self._validation.validate(query, schema)
        if not validation.valid:
            issue = validation.issues[0]
            raise SQLAgentError(issue.code, issue.message, repairable=issue.repairable)
        runtime = SQLiteQueryRuntime(self._execution_config, self._access, schema, cancelled)
        try:
            with self._engine.connect() as connection:
                return runtime.run(connection, query, row_limit)
        except SQLAlchemyError as exc:
            raise SQLAgentError(ErrorCode.EXECUTION, DATABASE_READ_FAILED) from exc

    def close(self) -> None:
        if not self._closed:
            self._engine.dispose()
            self._closed = True
