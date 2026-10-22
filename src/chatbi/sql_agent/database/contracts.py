"""Database implementation boundary, independent of SQLAlchemy."""

from threading import Event
from typing import Protocol

from chatbi.sql_agent.models import DatabaseSchema, QueryResult, SQLQuery, ValidationResult


class DatabaseAdapter(Protocol):
    def get_schema(self) -> DatabaseSchema: ...

    def get_table_sample(self, table_name: str) -> QueryResult: ...

    def validate_query(self, query: SQLQuery) -> ValidationResult: ...

    def execute_query(self, query: SQLQuery, cancelled: Event) -> QueryResult: ...

    def close(self) -> None: ...
