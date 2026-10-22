"""End-to-end deterministic workflow tests, using real SQLite and a scripted model."""

import asyncio
import json
import sqlite3
from collections import deque
from contextlib import closing
from datetime import date

import pytest

from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery, SQLParameter
from chatbi.sql_agent.agents.stages import (
    ExplicitTableSelectionStrategy,
    QueryUnderstandingStage,
    SQLGenerationStage,
    SQLRepairStage,
)
from chatbi.sql_agent.config import AccessConfig, DatabaseConfig, ExecutionConfig, WorkflowConfig
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent.database.validator import default_validation_policy
from chatbi.sql_agent.enums import ErrorCode, ResponseStatus, UnderstandingStatus
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import SQLAgentRequest
from chatbi.sql_agent.prompts import (
    GENERATION_AGENT_NAME,
    REPAIR_AGENT_NAME,
    UNDERSTANDING_AGENT_NAME,
)
from chatbi.sql_agent.repair import BudgetedSQLRepairPolicy
from chatbi.sql_agent.service import SQLAgentService
from chatbi.sql_agent.workflow import SQLAgentWorkflow

REFERENCE_DATE = date(2026, 10, 22)
COUNT_SQL = "SELECT COUNT(*) AS total FROM numbers"
INVALID_COLUMN_SQL = "SELECT missing FROM numbers"
INVALID_AGGREGATE_SQL = "SELECT SUM(SUM(id)) FROM numbers"
EXPENSIVE_SQL = """WITH RECURSIVE counter(n) AS (
    SELECT 1 UNION ALL SELECT n+1 FROM counter WHERE n < 100000000
) SELECT SUM(n) FROM counter"""


def parsed_query(**changes):
    values = {
        "status": UnderstandingStatus.READY,
        "selected_tables": ["numbers"],
        "metrics": ["记录数"],
        "dimensions": [],
        "filters": [],
        "time_range": None,
        "ordering": [],
        "top_n": None,
        "assumptions": [],
        "clarification_questions": [],
    }
    return ParsedQuery(**(values | changes))


def candidate(sql, **changes):
    return GeneratedSQL(**({"sql": sql, "parameters": [], "assumptions": []} | changes))


class ScriptedRunner:
    def __init__(self, outputs):
        self.outputs = deque(outputs)
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.popleft()
        if isinstance(output, Exception):
            raise output
        assert isinstance(output, kwargs["output_type"])
        return output


class RecordingWorkflow(SQLAgentWorkflow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.states = []

    async def run(self, state):
        self.states.append(state)
        return await super().run(state)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "workflow.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE numbers(id INTEGER PRIMARY KEY);"
            "INSERT INTO numbers VALUES(1),(2),(3);"
            "CREATE TABLE other(id INTEGER);"
        )
    adapter = SQLiteAdapter(DatabaseConfig(project_root=tmp_path, path=path), ExecutionConfig())
    with DatabaseService(adapter) as database:
        yield database


def build_service(database, runner, *, config=None, repair_policy=None):
    config = config if config is not None else WorkflowConfig()
    access = AccessConfig()
    workflow = RecordingWorkflow(
        database=database,
        understanding=QueryUnderstandingStage(runner),
        selection=ExplicitTableSelectionStrategy(),
        generation=SQLGenerationStage(runner, access),
        repair=SQLRepairStage(runner, access),
        validation=default_validation_policy(ExecutionConfig()),
        repair_policy=repair_policy
        if repair_policy is not None
        else BudgetedSQLRepairPolicy(config),
        config=config,
    )
    return SQLAgentService(workflow, config, lambda: REFERENCE_DATE), workflow


def ask(service, question="统计 numbers 表的记录数"):
    return asyncio.run(service.ask(SQLAgentRequest(question=question)))


def test_success_returns_exact_executed_query_result_and_metadata(database):
    runner = ScriptedRunner([parsed_query(), candidate(COUNT_SQL)])
    service, workflow = build_service(database, runner)
    response = ask(service)
    assert response.status == ResponseStatus.SUCCESS
    assert response.query.sql == COUNT_SQL and response.result.rows == [[3]]
    assert response.metadata.execution_count == 1
    assert response.metadata.repair_count == 0
    assert response.metadata.elapsed_ms > 0
    assert workflow.states[0].attempts[0].result == response.result
    assert [call["name"] for call in runner.calls] == [
        UNDERSTANDING_AGENT_NAME,
        GENERATION_AGENT_NAME,
    ]


def test_clarification_returns_without_generating_or_executing_sql(database):
    runner = ScriptedRunner(
        [
            parsed_query(
                status=UnderstandingStatus.NEEDS_CLARIFICATION,
                selected_tables=[],
                clarification_questions=["希望统计哪个指标？"],
            )
        ]
    )
    service, workflow = build_service(database, runner)
    response = ask(service)
    assert response.status == ResponseStatus.NEEDS_CLARIFICATION
    assert response.clarification_questions == ["希望统计哪个指标？"]
    assert response.query is None and response.metadata.execution_count == 0
    assert len(runner.calls) == 1 and not workflow.states[0].attempts


@pytest.mark.parametrize(
    "bad_sql,expected_executions",
    [
        (INVALID_COLUMN_SQL, 1),
        (INVALID_AGGREGATE_SQL, 2),
    ],
)
def test_validation_and_execution_errors_are_repaired(database, bad_sql, expected_executions):
    runner = ScriptedRunner([parsed_query(), candidate(bad_sql), candidate(COUNT_SQL)])
    service, workflow = build_service(database, runner)
    response = ask(service)
    assert response.status == ResponseStatus.SUCCESS
    assert response.query.sql == COUNT_SQL and response.result.rows == [[3]]
    assert response.metadata.repair_count == 1
    assert response.metadata.execution_count == expected_executions
    attempts = workflow.states[0].attempts
    assert len(attempts) == 2 and attempts[0].error is not None
    assert attempts[1].validation.valid
    payload = json.loads(runner.calls[-1]["user_input"])
    assert runner.calls[-1]["name"] == REPAIR_AGENT_NAME
    assert payload["previous_query"]["sql"] == bad_sql
    assert payload["question"] == "统计 numbers 表的记录数"
    assert payload["reference_date"] == "2026-10-22"
    assert [table["name"] for table in payload["schema"]["tables"]] == ["numbers"]


@pytest.mark.parametrize("sql", ["DELETE FROM numbers", "SELECT * FROM other"])
def test_security_or_selected_schema_violations_are_not_repaired(database, sql):
    runner = ScriptedRunner([parsed_query(), candidate(sql)])
    service, _ = build_service(database, runner)
    response = ask(service)
    assert response.status == ResponseStatus.FAILED
    assert response.error.code == ErrorCode.ACCESS_DENIED
    assert response.metadata.repair_count == response.metadata.execution_count == 0
    assert len(runner.calls) == 2


def test_repair_cannot_bypass_revalidation(database):
    runner = ScriptedRunner(
        [
            parsed_query(),
            candidate(INVALID_COLUMN_SQL),
            candidate("DROP TABLE numbers"),
        ]
    )
    service, workflow = build_service(database, runner)
    response = ask(service)
    assert response.status == ResponseStatus.FAILED
    assert response.error.code == ErrorCode.ACCESS_DENIED
    assert response.metadata.repair_count == 1 and response.metadata.execution_count == 0
    assert not workflow.states[0].attempts[-1].validation.valid


def test_repair_budget_limits_candidate_count(database):
    runner = ScriptedRunner([parsed_query()] + [candidate(INVALID_COLUMN_SQL) for _ in range(3)])
    service, workflow = build_service(database, runner)
    response = ask(service)
    assert response.error.code == ErrorCode.BUDGET_EXHAUSTED
    assert response.metadata.repair_count == 2 and response.metadata.execution_count == 0
    assert len(workflow.states[0].attempts) == 3
    assert not runner.outputs


def test_execution_budget_stops_before_another_repair_call(database):
    runner = ScriptedRunner([parsed_query(), candidate(INVALID_AGGREGATE_SQL)])
    service, _ = build_service(database, runner, config=WorkflowConfig(max_executions=1))
    response = ask(service)
    assert response.error.code == ErrorCode.BUDGET_EXHAUSTED
    assert response.metadata.execution_count == 1 and response.metadata.repair_count == 0
    assert len(runner.calls) == 2


def test_zero_repair_budget_disables_repairs(database):
    runner = ScriptedRunner([parsed_query(), candidate(INVALID_COLUMN_SQL)])
    service, _ = build_service(database, runner, config=WorkflowConfig(max_repairs=0))
    response = ask(service)
    assert response.error.code == ErrorCode.BUDGET_EXHAUSTED
    assert response.metadata.repair_count == 0


def test_custom_policy_can_decline_repair(database):
    class NeverRepair:
        def should_repair(self, issue, repair_count):
            return False

    runner = ScriptedRunner([parsed_query(), candidate(INVALID_COLUMN_SQL)])
    service, _ = build_service(database, runner, repair_policy=NeverRepair())
    response = ask(service)
    assert response.error.code == ErrorCode.INVALID_SQL
    assert response.metadata.repair_count == 0


def test_empty_result_is_success_and_does_not_trigger_repair(database):
    runner = ScriptedRunner([parsed_query(), candidate("SELECT id FROM numbers WHERE id > 10")])
    service, _ = build_service(database, runner)
    response = ask(service)
    assert response.status == ResponseStatus.SUCCESS and response.result.rows == []
    assert response.metadata.repair_count == 0


def test_model_failures_and_unexpected_failures_have_controlled_responses(database):
    for exception, expected_code in [
        (SQLAgentError(ErrorCode.MODEL_CALL, "Model request failed"), ErrorCode.MODEL_CALL),
        (RuntimeError("private internal details"), ErrorCode.INTERNAL),
    ]:
        service, _ = build_service(database, ScriptedRunner([exception]))
        response = ask(service)
        assert response.status == ResponseStatus.FAILED and response.error.code == expected_code
        assert "private internal details" not in response.model_dump_json()


def test_request_timeout_cancels_model_wait(database):
    class BlockingRunner:
        cancelled = False

        async def run(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    runner = BlockingRunner()
    service, _ = build_service(database, runner, config=WorkflowConfig(request_timeout_seconds=0.1))
    response = ask(service)
    assert response.error.code == ErrorCode.TIMEOUT and runner.cancelled
    assert response.metadata.execution_count == response.metadata.repair_count == 0


def test_request_timeout_stops_sql_and_does_not_repair(database):
    runner = ScriptedRunner([parsed_query(), candidate(EXPENSIVE_SQL)])
    service, _ = build_service(
        database, runner, config=WorkflowConfig(request_timeout_seconds=0.15)
    )
    response = ask(service)
    assert response.error.code == ErrorCode.TIMEOUT
    assert response.metadata.repair_count == 0
    assert response.metadata.elapsed_ms < 2000
    # The same database service can immediately execute another request.
    next_service, _ = build_service(
        database, ScriptedRunner([parsed_query(), candidate(COUNT_SQL)])
    )
    assert ask(next_service).result.rows == [[3]]


def test_caller_cancellation_propagates(database):
    class BlockingRunner:
        def __init__(self):
            self.entered = asyncio.Event()

        async def run(self, **kwargs):
            self.entered.set()
            await asyncio.Event().wait()

    async def scenario():
        runner = BlockingRunner()
        service, _ = build_service(database, runner)
        task = asyncio.create_task(service.ask(SQLAgentRequest(question="统计记录数")))
        await runner.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_concurrent_requests_do_not_share_state_or_parameters(database):
    class RoutingRunner:
        async def run(self, **kwargs):
            if kwargs["output_type"] is ParsedQuery:
                return parsed_query()
            value = int(json.loads(kwargs["user_input"])["question"])
            return candidate(
                "SELECT id FROM numbers WHERE id=:value",
                parameters=[SQLParameter(name="value", value=value)],
            )

    service, workflow = build_service(database, RoutingRunner())

    async def scenario():
        return await asyncio.gather(
            *(service.ask(SQLAgentRequest(question=str(i))) for i in (1, 2))
        )

    first, second = asyncio.run(scenario())
    assert first.result.rows == [[1]] and second.result.rows == [[2]]
    assert first.request_id != second.request_id
    assert first.query.parameters != second.query.parameters
    assert workflow.states[0].attempts is not workflow.states[1].attempts


def test_final_warnings_do_not_keep_assumptions_from_failed_candidate(database):
    runner = ScriptedRunner(
        [
            parsed_query(assumptions=["基础口径"]),
            candidate(INVALID_COLUMN_SQL, assumptions=["失败候选的假设"]),
            candidate(COUNT_SQL, assumptions=["修复后的假设"]),
        ]
    )
    service, _ = build_service(database, runner)
    response = ask(service)
    assert response.warnings == ["假设：基础口径", "假设：修复后的假设"]


def test_untrusted_repair_policy_cannot_retry_access_violations(database):
    class AlwaysRepair:
        def should_repair(self, issue, repair_count):
            return True

    runner = ScriptedRunner([parsed_query(), candidate("DELETE FROM numbers")])
    service, _ = build_service(database, runner, repair_policy=AlwaysRepair())
    response = ask(service)
    assert response.error.code == ErrorCode.ACCESS_DENIED
    assert response.metadata.repair_count == 0


def test_execution_limit_and_repair_limit_are_independent(database):
    runner = ScriptedRunner([parsed_query()] + [candidate(INVALID_AGGREGATE_SQL) for _ in range(2)])
    service, _ = build_service(
        database, runner, config=WorkflowConfig(max_executions=2, max_repairs=5)
    )
    response = ask(service)
    assert response.error.code == ErrorCode.BUDGET_EXHAUSTED
    assert response.metadata.execution_count == 2 and response.metadata.repair_count == 1
    assert not runner.outputs


def test_one_deadline_is_shared_by_all_model_stages(database):
    class StagedRunner:
        def __init__(self):
            self.remaining_at_generation = None

        async def run(self, **kwargs):
            if kwargs["output_type"] is ParsedQuery:
                await asyncio.sleep(0.06)
                return parsed_query()
            # A fresh per-stage timeout would incorrectly allow this to finish.
            await asyncio.sleep(0.08)
            return candidate(COUNT_SQL)

    service, _ = build_service(
        database, StagedRunner(), config=WorkflowConfig(request_timeout_seconds=0.1)
    )
    response = ask(service)
    assert response.error.code == ErrorCode.TIMEOUT
    assert response.metadata.execution_count == 0


def test_bootstrap_closes_database_and_model_on_failure(tmp_path, monkeypatch):
    from chatbi.settings import ModelConfig
    from chatbi.sql_agent.bootstrap import open_sql_agent

    resources = {}

    class DummyAdapter:
        def __init__(self, *args, **kwargs):
            self.closed = False
            resources["database"] = self

        def close(self):
            self.closed = True

    class DummyRunner:
        def __init__(self, *args, **kwargs):
            self.closed = False
            resources["model"] = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    monkeypatch.setattr("chatbi.sql_agent.bootstrap.SQLiteAdapter", DummyAdapter)
    monkeypatch.setattr("chatbi.sql_agent.bootstrap.AgentsSDKRunner", DummyRunner)

    async def scenario():
        config = ModelConfig(api_key="test-key", base_url="https://example.test/v1", model="test")
        async with open_sql_agent(tmp_path, model_config=config):
            raise RuntimeError("application failed")

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())
    assert resources["database"].closed and resources["model"].closed
