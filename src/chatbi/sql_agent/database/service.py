"""Single metadata entry point, configured through an injected adapter."""

from typing import Self

from chatbi.sql_agent.database.contracts import DatabaseAdapter
from chatbi.sql_agent.database.executor import SQLExecutor
from chatbi.sql_agent.models import DatabaseSchema, QueryResult, SQLQuery, ValidationResult


class DatabaseService:
    def __init__(self, adapter: DatabaseAdapter) -> None:
        self._adapter = adapter
        self._executor = SQLExecutor(adapter)

    def get_schema(self) -> DatabaseSchema:
        return self._adapter.get_schema()

    def get_table_sample(self, table_name: str) -> QueryResult:
        return self._adapter.get_table_sample(table_name)

    def validate_query(self, query: SQLQuery) -> ValidationResult:
        return self._adapter.validate_query(query)

    async def execute_query(self, query: SQLQuery) -> QueryResult:
        return await self._executor.execute(query)

    def close(self) -> None:
        self._adapter.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
