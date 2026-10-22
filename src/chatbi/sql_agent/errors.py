"""Typed failures for deterministic components; never expose raw driver errors."""

from chatbi.sql_agent.enums import ErrorCode


class SQLAgentError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, repairable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.repairable = repairable
