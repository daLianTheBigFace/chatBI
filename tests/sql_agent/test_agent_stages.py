"""Stage contracts, schema selection and SDK wiring using network-free replacements."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import AgentOutputSchema
from openai import OpenAIError
from pydantic import ValidationError

from chatbi.settings import ModelConfig
from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery, SQLParameter
from chatbi.sql_agent.agents.runtime import AgentsSDKRunner
from chatbi.sql_agent.agents.stages import (
    ExplicitTableSelectionStrategy,
    QueryUnderstandingStage,
    SQLGenerationStage,
)
from chatbi.sql_agent.config import AccessConfig
from chatbi.sql_agent.enums import ErrorCode, UnderstandingStatus
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.models import ColumnSchema, DatabaseSchema, ForeignKeySchema, TableSchema

REFERENCE_DATE = date(2026, 10, 22)
QUESTION = "查询编号为 1 的艺术家名称"


def understood(**changes):
    values = {
        "status": UnderstandingStatus.READY,
        "selected_tables": ["artists"],
        "metrics": [],
        "dimensions": ["Name"],
        "filters": ["ArtistId = 1"],
        "time_range": None,
        "ordering": [],
        "top_n": None,
        "assumptions": [],
        "clarification_questions": [],
    }
    return ParsedQuery(**(values | changes))


@pytest.fixture
def schema():
    return DatabaseSchema(
        tables=[
            TableSchema(
                name="artists",
                columns=[
                    ColumnSchema(
                        name="ArtistId", data_type="INTEGER", nullable=False, primary_key=True
                    ),
                    ColumnSchema(name="Name", data_type="TEXT", nullable=True),
                ],
            ),
            TableSchema(
                name="albums",
                columns=[
                    ColumnSchema(name="ArtistId", data_type="INTEGER", nullable=False),
                ],
                foreign_keys=[
                    ForeignKeySchema(
                        columns=["ArtistId"],
                        referred_table="artists",
                        referred_columns=["ArtistId"],
                    )
                ],
            ),
        ]
    )


class StubRunner:
    def __init__(self, output):
        self.output = output
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.output


def test_understanding_includes_schema_and_reference_date(schema):
    runner = StubRunner(understood())
    output = asyncio.run(QueryUnderstandingStage(runner).parse(QUESTION, schema, REFERENCE_DATE))
    assert output.status == UnderstandingStatus.READY
    payload = json.loads(runner.calls[0]["user_input"])
    assert payload["reference_date"] == "2026-10-22"
    assert payload["schema"] == schema.model_dump()
    assert "api_key" not in payload


def test_hallucinated_table_selection_is_rejected(schema):
    runner = StubRunner(understood(selected_tables=["secret"]))
    with pytest.raises(SQLAgentError) as error:
        asyncio.run(QueryUnderstandingStage(runner).parse(QUESTION, schema, REFERENCE_DATE))
    assert error.value.code == ErrorCode.MODEL_OUTPUT


def test_selection_copies_real_schema_and_filters_relationships(schema):
    selection = ExplicitTableSelectionStrategy()
    chosen = asyncio.run(selection.select(understood(selected_tables=["ALBUMS"]), schema))
    assert [table.name for table in chosen.tables] == ["albums"]
    assert chosen.tables[0].foreign_keys == []
    chosen.tables[0].columns.clear()
    assert schema.tables[1].columns
    assert schema.tables[1].foreign_keys


def test_clarification_stops_generation_without_calling_model(schema):
    runner = StubRunner(None)
    parsed = understood(
        status=UnderstandingStatus.NEEDS_CLARIFICATION,
        selected_tables=[],
        clarification_questions=["统计哪个指标？"],
    )
    stage = SQLGenerationStage(runner, AccessConfig())
    with pytest.raises(SQLAgentError):
        asyncio.run(stage.generate(QUESTION, parsed, schema, REFERENCE_DATE))
    assert runner.calls == []


def test_generation_only_receives_selected_schema_and_preserves_parameters(schema):
    candidate = GeneratedSQL(
        sql="SELECT Name FROM artists WHERE ArtistId=:artist_id",
        parameters=[SQLParameter(name="artist_id", value=1)],
        assumptions=[],
    )
    runner = StubRunner(candidate)
    parsed = understood()
    selected = asyncio.run(ExplicitTableSelectionStrategy().select(parsed, schema))
    output = asyncio.run(
        SQLGenerationStage(runner, AccessConfig()).generate(
            QUESTION, parsed, selected, REFERENCE_DATE
        )
    )
    payload = json.loads(runner.calls[0]["user_input"])
    assert [table["name"] for table in payload["schema"]["tables"]] == ["artists"]
    assert output.as_query().parameters == {"artist_id": 1}
    assert payload["allowed_functions"]


def test_generation_rejects_schema_selection_mismatch(schema):
    runner = StubRunner(None)
    with pytest.raises(SQLAgentError):
        asyncio.run(
            SQLGenerationStage(runner, AccessConfig()).generate(
                QUESTION, understood(), schema, REFERENCE_DATE
            )
        )
    assert not runner.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"selected_tables": []},
        {"clarification_questions": ["unexpected question"]},
        {"top_n": 0},
        {"status": UnderstandingStatus.NEEDS_CLARIFICATION},
    ],
)
def test_understanding_contract_rejects_inconsistent_output(changes):
    with pytest.raises(ValidationError):
        understood(**changes)


@pytest.mark.parametrize("names", [["duplicate", "duplicate"], [":invalid"], ["has space"]])
def test_parameter_names_must_be_unique_and_valid(names):
    with pytest.raises(ValidationError):
        GeneratedSQL(
            sql="SELECT 1",
            parameters=[SQLParameter(name=name, value=1) for name in names],
            assumptions=[],
        )


@pytest.mark.parametrize("output_type", [ParsedQuery, GeneratedSQL])
def test_outputs_support_sdk_strict_schema(output_type):
    schema = AgentOutputSchema(output_type).json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def model_config(**changes):
    return ModelConfig(
        **(
            {
                "api_key": "test-only-secret",
                "base_url": "https://example.test/v1",
                "model": "test-model",
            }
            | changes
        )
    )


def test_sdk_runner_injects_client_model_and_disables_tracing(monkeypatch):
    captured = {}

    async def fake_run(agent, **kwargs):
        captured.update(kwargs)
        captured["agent"] = agent
        return SimpleNamespace(final_output=understood())

    monkeypatch.setattr("chatbi.sql_agent.agents.runtime.Runner.run", fake_run)

    async def scenario():
        async with AgentsSDKRunner(model_config()) as runner:
            assert runner._client.api_key == "test-only-secret"
            assert str(runner._client.base_url) == "https://example.test/v1/"
            return await runner.run(
                name="test",
                instructions="test prompt",
                user_input="test input",
                output_type=ParsedQuery,
            )

    assert asyncio.run(scenario()).status == UnderstandingStatus.READY
    assert captured["run_config"].tracing_disabled
    assert not captured["run_config"].trace_include_sensitive_data
    assert captured["max_turns"] == 1
    assert captured["agent"].model.model == "test-model"
    assert captured["agent"].tools == []


@pytest.mark.parametrize("output", ["raw text", None])
def test_sdk_runner_rejects_unstructured_output(monkeypatch, output):
    monkeypatch.setattr(
        "chatbi.sql_agent.agents.runtime.Runner.run",
        AsyncMock(return_value=SimpleNamespace(final_output=output)),
    )

    async def scenario():
        async with AgentsSDKRunner(model_config()) as runner:
            return await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.MODEL_OUTPUT


def test_model_error_does_not_expose_provider_details(monkeypatch):
    monkeypatch.setattr(
        "chatbi.sql_agent.agents.runtime.Runner.run",
        AsyncMock(side_effect=OpenAIError("test-only-secret")),
    )

    async def scenario():
        async with AgentsSDKRunner(model_config()) as runner:
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.MODEL_CALL
    assert "test-only-secret" not in str(error.value)


def test_model_call_has_a_wall_clock_timeout(monkeypatch):
    async def slow_run(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr("chatbi.sql_agent.agents.runtime.Runner.run", slow_run)

    async def scenario():
        async with AgentsSDKRunner(model_config(timeout_seconds=0.01)) as runner:
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.TIMEOUT


def test_sdk_runner_revalidates_constructed_instances(monkeypatch):
    invalid = understood().model_copy(update={"selected_tables": []})
    monkeypatch.setattr(
        "chatbi.sql_agent.agents.runtime.Runner.run",
        AsyncMock(return_value=SimpleNamespace(final_output=invalid)),
    )

    async def scenario():
        async with AgentsSDKRunner(model_config()) as runner:
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.MODEL_OUTPUT


def test_runtime_releases_client_and_refuses_calls_after_close():
    async def scenario():
        runner = AgentsSDKRunner(model_config())
        close = AsyncMock()
        runner._client.close = close
        await runner.close()
        await runner.close()
        close.assert_awaited_once()
        with pytest.raises(SQLAgentError) as error:
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )
        assert error.value.code == ErrorCode.CONFIGURATION

    asyncio.run(scenario())
