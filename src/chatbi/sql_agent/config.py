"""Central configuration; relative database paths resolve from the project root."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt

DEFAULT_DATABASE_PATH = Path("data/chinook.db")
DEFAULT_QUERY_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_ROWS = 1000
DEFAULT_MAX_RESULT_BYTES = 2 * 1024 * 1024
DEFAULT_SAMPLE_ROWS = 5
DEFAULT_MAX_REPAIRS = 2
DEFAULT_MAX_EXECUTIONS = 3
DEFAULT_MAX_SQL_CHARACTERS = 50_000
DEFAULT_PROGRESS_INTERVAL = 1000
DEFAULT_ALLOWED_FUNCTIONS = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "total",
        "abs",
        "round",
        "coalesce",
        "ifnull",
        "nullif",
        "lower",
        "upper",
        "length",
        "substr",
        "substring",
        "trim",
        "ltrim",
        "rtrim",
        "replace",
        "date",
        "datetime",
        "strftime",
        "julianday",
        "unixepoch",
        "time",
        "timediff",
        "typeof",
        "instr",
        "like",
        "glob",
        "group_concat",
        "row_number",
        "rank",
        "dense_rank",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        "percent_rank",
        "cume_dist",
    }
)


class ExecutionConfig(BaseModel):
    """Immutable runtime budgets, enforced by later execution components."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_timeout_seconds: PositiveFloat = DEFAULT_QUERY_TIMEOUT_SECONDS
    max_rows: PositiveInt = DEFAULT_MAX_ROWS
    max_result_bytes: PositiveInt = DEFAULT_MAX_RESULT_BYTES
    sample_rows: PositiveInt = DEFAULT_SAMPLE_ROWS
    max_sql_characters: PositiveInt = DEFAULT_MAX_SQL_CHARACTERS
    progress_interval: PositiveInt = DEFAULT_PROGRESS_INTERVAL


class AccessConfig(BaseModel):
    """Names are matched case-insensitively; unspecified columns permit all columns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_tables: frozenset[str] | None = None
    allowed_columns: dict[str, frozenset[str]] = Field(default_factory=dict)
    allowed_functions: frozenset[str] = DEFAULT_ALLOWED_FUNCTIONS


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_timeout_seconds: PositiveFloat = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_executions: PositiveInt = DEFAULT_MAX_EXECUTIONS

    # A repair budget of zero disables repairs.
    max_repairs: int = Field(default=DEFAULT_MAX_REPAIRS, ge=0)


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_root: Path
    path: Path = DEFAULT_DATABASE_PATH

    @property
    def resolved_path(self) -> Path:
        path = self.path if self.path.is_absolute() else self.project_root / self.path
        return path.resolve()
