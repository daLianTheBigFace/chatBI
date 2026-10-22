"""Safety, execution budgets and strategy composition across the public entry point."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest

from chatbi.sql_agent.config import AccessConfig, DatabaseConfig, ExecutionConfig
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent.database.validator import (
    CompositeSQLValidationPolicy,
    ParameterRule,
    ReadOnlyRule,
    SchemaRule,
)
from chatbi.sql_agent.enums import ErrorCode
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import SQLQuery, ValidationIssue, ValidationResult

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPENSIVE_SQL = """
WITH RECURSIVE counter(n) AS (
    SELECT 1 UNION ALL SELECT n + 1 FROM counter WHERE n < 100000000
)
SELECT SUM(n) FROM counter
"""


@pytest.fixture
def query_database(tmp_path):
    path = tmp_path / "queries.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE numbers(id INTEGER PRIMARY KEY, label TEXT);"
            "INSERT INTO numbers VALUES(1, 'one'), (2, 'two'), (3, 'three');"
            "CREATE TABLE secret(token TEXT);"
            "INSERT INTO secret VALUES ('hidden');"
        )
    return path


def make_service(path, *, access=None, policy=None, **settings):
    return DatabaseService(
        SQLiteAdapter(
            DatabaseConfig(project_root=path.parent, path=path),
            ExecutionConfig(**settings),
            access_config=access,
            validation_policy=policy,
        )
    )


def run_query(service, sql, parameters=None):
    return asyncio.run(service.execute_query(SQLQuery(sql=sql, parameters=parameters or {})))


@pytest.mark.parametrize(
    "sql",
    [
        "-- comment\nSELECT COUNT(*) FROM numbers",
        "WITH chosen AS (SELECT id FROM numbers) SELECT SUM(id) FROM chosen",
        "SELECT n.id, s.id FROM numbers n JOIN numbers s ON n.id=s.id ORDER BY n.id",
        "SELECT id FROM numbers UNION SELECT id FROM numbers",
        "SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM numbers",
        "SELECT 'DROP TABLE numbers; --' AS harmless_text",
        "SELECT id AS ident FROM numbers ORDER BY ident",
        "SELECT COUNT(*) FROM numbers HAVING COUNT(*) > 0",
        "SELECT n.id FROM numbers n WHERE EXISTS(SELECT 1 FROM numbers m WHERE m.id=n.id)",
    ],
)
def test_valid_sql_variants(query_database, sql):
    with make_service(query_database) as service:
        assert service.validate_query(SQLQuery(sql=sql)).valid
        assert run_query(service, sql).rows


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM numbers",
        "UPDATE numbers SET label='changed'",
        "INSERT INTO numbers VALUES(4, 'four')",
        "DROP TABLE numbers",
        "ALTER TABLE numbers ADD COLUMN extra TEXT",
        "ATTACH DATABASE ':memory:' AS other",
        "DETACH DATABASE main",
        "PRAGMA query_only=OFF",
        "VACUUM",
        "SELECT 1; DELETE FROM numbers",
        "WITH chosen AS (SELECT 1) DELETE FROM numbers",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM pragma_table_info('numbers')",
        "SELECT * FROM main.numbers",
        "SELECT * FROM missing",
        "SELECT missing_column FROM numbers",
        "SELECT id FROM numbers n JOIN numbers m ON n.id=m.id",
    ],
)
def test_invalid_sql_is_blocked_before_execution(query_database, sql):
    before = hashlib.sha256(query_database.read_bytes()).digest()
    with make_service(query_database) as service:
        assert not service.validate_query(SQLQuery(sql=sql)).valid
        with pytest.raises(SQLAgentError):
            run_query(service, sql)
    assert hashlib.sha256(query_database.read_bytes()).digest() == before


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT load_extension('anything')",
        "SELECT readfile('anything')",
        "SELECT randomblob(100000000)",
        "SELECT sqlite_version()",
    ],
)
def test_function_allowlist_rejects_unapproved_functions(query_database, sql):
    with make_service(query_database) as service:
        with pytest.raises(SQLAgentError) as error:
            run_query(service, sql)
        assert error.value.code in {ErrorCode.ACCESS_DENIED, ErrorCode.EXECUTION}


def test_random_sampling_is_allowed_without_enabling_randomblob(query_database):
    with make_service(query_database) as service:
        result = run_query(
            service, "SELECT id, label FROM numbers ORDER BY RANDOM() LIMIT :n", {"n": 2}
        )
        assert len(result.rows) == 2
        assert len({row[0] for row in result.rows}) == 2
        assert all(row[0] in {1, 2, 3} for row in result.rows)
        with pytest.raises(SQLAgentError):
            run_query(service, "SELECT randomblob(100000000)")


def test_random_can_still_be_disabled_by_custom_access_policy(query_database):
    access = AccessConfig(allowed_functions=frozenset({"count"}))
    with make_service(query_database, access=access) as service:
        with pytest.raises(SQLAgentError) as error:
            run_query(service, "SELECT id FROM numbers ORDER BY RANDOM() LIMIT 2")
        assert error.value.code == ErrorCode.ACCESS_DENIED


def test_bound_value_cannot_change_sql(query_database):
    with make_service(query_database) as service:
        result = run_query(
            service,
            "SELECT id FROM numbers WHERE label=:label",
            {"label": "one' OR 1=1; DROP TABLE numbers; --"},
        )
        assert not result.rows
        assert run_query(service, "SELECT COUNT(*) FROM numbers").rows == [[3]]


@pytest.mark.parametrize(
    "sql,parameters",
    [
        ("SELECT :missing", {}),
        ("SELECT 1", {"extra": 1}),
        ("SELECT ?", {}),
        ("SELECT @value", {"value": 1}),
    ],
)
def test_parameter_mismatches_are_rejected(query_database, sql, parameters):
    with make_service(query_database) as service:
        assert not service.validate_query(SQLQuery(sql=sql, parameters=parameters)).valid


def test_restricted_tables_and_columns_apply_to_schema_samples_and_queries(query_database):
    access = AccessConfig(
        allowed_tables=frozenset({"NUMBERS"}), allowed_columns={"numbers": frozenset({"id"})}
    )
    with make_service(query_database, access=access) as service:
        schema = service.get_schema()
        assert [t.name for t in schema.tables] == ["numbers"]
        assert [c.name for c in schema.tables[0].columns] == ["id"]
        assert service.get_table_sample("numbers").columns == ["id"]
        assert run_query(service, "SELECT id FROM numbers").rows == [[1], [2], [3]]
        for sql in ["SELECT * FROM secret", "SELECT label FROM numbers", "SELECT * FROM numbers"]:
            with pytest.raises(SQLAgentError):
                run_query(service, sql)


def test_row_truncation_and_empty_result(query_database):
    with make_service(query_database, max_rows=2) as service:
        result = run_query(service, "SELECT id FROM numbers ORDER BY id")
        assert result.rows == [[1], [2]] and result.truncated
        result = run_query(service, "SELECT id FROM numbers WHERE id > 10")
        assert result.rows == [] and not result.truncated
        result = run_query(service, "SELECT id FROM numbers WHERE id < 3")
        assert len(result.rows) == 2 and not result.truncated


def test_byte_budget_and_samples_use_same_runtime(query_database):
    with make_service(query_database, max_result_bytes=45) as service:
        for result in [
            run_query(service, "SELECT label FROM numbers"),
            service.get_table_sample("numbers"),
        ]:
            payload = {"columns": result.columns, "rows": result.rows}
            assert (
                len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 45
            )
            assert result.truncated


def test_sql_length_budget(query_database):
    with make_service(query_database, max_sql_characters=20) as service:
        with pytest.raises(SQLAgentError):
            run_query(service, "SELECT id, label FROM numbers")


def test_nonfinite_result_is_a_controlled_error(query_database):
    with make_service(query_database) as service:
        with pytest.raises(SQLAgentError) as error:
            run_query(service, "SELECT 1e999")
        assert error.value.code == ErrorCode.EXECUTION
        assert not error.value.repairable


def test_empty_column_allowlist_does_not_allow_counting_table(query_database):
    access = AccessConfig(allowed_columns={"secret": frozenset()})
    with make_service(query_database, access=access) as service:
        assert "secret" not in {table.name for table in service.get_schema().tables}
        with pytest.raises(SQLAgentError):
            run_query(service, "SELECT COUNT(*) FROM secret")


def test_oversized_value_is_stopped_inside_sqlite(query_database):
    with make_service(query_database, max_result_bytes=64) as service:
        with pytest.raises(SQLAgentError) as error:
            run_query(service, "SELECT :large_value", {"large_value": "x" * 100000})
        assert error.value.code == ErrorCode.BUDGET_EXHAUSTED


def test_views_and_virtual_tables_are_not_exposed(query_database):
    with closing(sqlite3.connect(query_database)) as connection, connection:
        connection.execute("CREATE VIEW hidden_view AS SELECT * FROM secret")
        try:
            connection.execute("CREATE VIRTUAL TABLE search USING fts5(content)")
        except sqlite3.OperationalError:
            pass  # Some SQLite distributions are built without FTS5.
    with make_service(query_database) as service:
        assert {table.name for table in service.get_schema().tables} == {"numbers", "secret"}
        for sql in ["SELECT * FROM hidden_view", "SELECT * FROM search_data"]:
            with pytest.raises(SQLAgentError):
                run_query(service, sql)


def test_progress_handler_stops_expensive_query_and_connection_remains_usable(query_database):
    with make_service(query_database, query_timeout_seconds=0.03, progress_interval=100) as service:
        with pytest.raises(SQLAgentError) as error:
            run_query(service, EXPENSIVE_SQL)
        assert error.value.code == ErrorCode.TIMEOUT
        assert run_query(service, "SELECT 1").rows == [[1]]


def test_async_cancellation_waits_for_worker_cleanup(query_database):
    with make_service(query_database) as service:

        async def scenario():
            task = asyncio.create_task(service.execute_query(SQLQuery(sql=EXPENSIVE_SQL)))
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            return await service.execute_query(SQLQuery(sql="SELECT COUNT(*) FROM numbers"))

        assert asyncio.run(scenario()).rows == [[3]]


def test_native_authorizer_still_blocks_tables_if_injected_validator_allows_everything(
    query_database,
):
    class AllowEverything:
        def validate(self, query, schema):
            return ValidationResult()

    with make_service(
        query_database,
        access=AccessConfig(allowed_tables=frozenset({"numbers"})),
        policy=AllowEverything(),
    ) as service:
        with pytest.raises(SQLAgentError) as error:
            run_query(service, "SELECT token FROM secret")
        assert error.value.code == ErrorCode.ACCESS_DENIED


def test_custom_rule_can_extend_default_strategy(query_database):
    class DenyAllRule:
        def check(self, context):
            return ValidationIssue(code=ErrorCode.ACCESS_DENIED, message="Disabled by policy")

    policy = CompositeSQLValidationPolicy(
        ExecutionConfig(), (ReadOnlyRule(), SchemaRule(), ParameterRule(), DenyAllRule())
    )
    with make_service(query_database, policy=policy) as service:
        with pytest.raises(SQLAgentError, match="Disabled by policy"):
            run_query(service, "SELECT 1")


def test_precancelled_execution_is_rejected(query_database):
    adapter = SQLiteAdapter(
        DatabaseConfig(project_root=query_database.parent, path=query_database), ExecutionConfig()
    )
    cancelled = Event()
    cancelled.set()
    try:
        with pytest.raises(SQLAgentError) as error:
            adapter.execute_query(SQLQuery(sql="SELECT 1"), cancelled)
        assert error.value.code == ErrorCode.TIMEOUT
    finally:
        adapter.close()


def test_chinook_aggregate_matches_independent_sqlite_result():
    path = PROJECT_ROOT / "data" / "chinook.db"
    sql = """SELECT a.Name, COUNT(*) AS total FROM artists a
             JOIN albums b ON a.ArtistId=b.ArtistId
             GROUP BY a.ArtistId, a.Name ORDER BY total DESC, a.ArtistId LIMIT 5"""
    before = hashlib.sha256(path.read_bytes()).digest()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        expected = [list(row) for row in connection.execute(sql)]
    with make_service(path) as service:
        assert run_query(service, sql).rows == expected
    assert hashlib.sha256(path.read_bytes()).digest() == before
