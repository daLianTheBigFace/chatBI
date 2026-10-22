"""Stable identifiers shared by workflow stages and integrations."""

from enum import StrEnum


class ResponseStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"


class ErrorCode(StrEnum):
    CONFIGURATION = "configuration_error"
    INVALID_SQL = "invalid_sql"
    ACCESS_DENIED = "access_denied"
    EXECUTION = "execution_error"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MODEL_CALL = "model_call_error"
    MODEL_OUTPUT = "model_output_error"
    INTERNAL = "internal_error"


class UnderstandingStatus(StrEnum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"


class WorkflowStage(StrEnum):
    SCHEMA = "schema"
    UNDERSTANDING = "understanding"
    SELECTION = "selection"
    GENERATION = "generation"
    VALIDATION = "validation"
    EXECUTION = "execution"
    REPAIR = "repair"
    SYNTHESIS = "synthesis"
    COMPLETED = "completed"
