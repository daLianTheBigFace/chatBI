"""Repair decisions are deterministic and independent of the model."""

from chatbi.sql_agent.config import WorkflowConfig
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.models import ValidationIssue

REPAIRABLE_CODES = frozenset({ErrorCode.INVALID_SQL, ErrorCode.EXECUTION})


class BudgetedSQLRepairPolicy:
    def __init__(self, config: WorkflowConfig) -> None:
        self._config = config

    def should_repair(self, issue: ValidationIssue, repair_count: int) -> bool:
        return (
            issue.repairable
            and issue.code in REPAIRABLE_CODES
            and repair_count < self._config.max_repairs
        )
