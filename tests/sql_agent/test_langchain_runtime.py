"""LangChain runner backed by langchain-openai, verified without network access."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessageChunk
from openai import OpenAIError
from pydantic import ValidationError

from chatbi.settings import ModelConfig
from chatbi.sql_agent.agent_models import ParsedQuery
from chatbi.sql_agent.enums import ErrorCode, UnderstandingStatus
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent_langchain.bootstrap import open_sql_agent_langchain
from chatbi.sql_agent_langchain.runtime import LangChainRunner


def model_config(**changes):
    return ModelConfig(
        **(
            {
                "api_key": "test-only-secret",
                "base_url": "https://example.test/v1",
                "model": "deepseek-chat",
            }
            | changes
        )
    )


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


class FakeClient:
    def __init__(self, **kwargs):
        self.config = kwargs
        self.closed = False

    async def close(self):
        self.closed = True


class FakeModel:
    def __init__(self, **kwargs):
        self.config = kwargs
        self.structured = AsyncMock()
        self.stream_chunks = []
        self.structured_method = None

    def with_structured_output(self, output_type, method="function_calling"):
        self.structured_method = method
        return self.structured

    def astream(self, messages):
        async def generate():
            for chunk in self.stream_chunks:
                yield chunk

        return generate()


def install_fakes(monkeypatch):
    monkeypatch.setattr("chatbi.sql_agent_langchain.runtime.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("chatbi.sql_agent_langchain.runtime.ChatOpenAI", FakeModel)


# --- LangChainRunner -----------------------------------------------------


def test_runner_injects_model_config(monkeypatch):
    install_fakes(monkeypatch)
    runner = LangChainRunner(model_config(reasoning_effort="low"))
    assert runner._model.config["model"] == "deepseek-chat"
    assert runner._model.config["api_key"] == "test-only-secret"
    assert runner._model.config["reasoning_effort"] == "low"
    assert runner._model.config["async_client"] is runner._client
    assert runner._client.config["api_key"] == "test-only-secret"
    assert runner._client.config["base_url"] == "https://example.test/v1"
    assert runner._client.config["timeout"] == 45.0
    assert runner._client.config["max_retries"] == 0


def test_runner_requests_function_calling(monkeypatch):
    install_fakes(monkeypatch)

    async def scenario():
        async with LangChainRunner(model_config()) as runner:
            runner._model.structured.ainvoke.return_value = understood()
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )
            return runner._model.structured_method

    assert asyncio.run(scenario()) == "function_calling"


def test_runner_returns_validated_structured_output(monkeypatch):
    install_fakes(monkeypatch)

    async def scenario():
        async with LangChainRunner(model_config()) as runner:
            runner._model.structured.ainvoke.return_value = understood()
            return await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    output = asyncio.run(scenario())
    assert output.status == UnderstandingStatus.READY
    assert output.selected_tables == ["artists"]


@pytest.mark.parametrize("output", [None, "raw text", 42])
def test_runner_rejects_unstructured_output(monkeypatch, output):
    install_fakes(monkeypatch)

    async def scenario():
        async with LangChainRunner(model_config()) as runner:
            runner._model.structured.ainvoke.return_value = output
            return await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.MODEL_OUTPUT


def test_runner_revalidates_constructed_instances(monkeypatch):
    install_fakes(monkeypatch)
    invalid = understood().model_copy(update={"selected_tables": []})

    async def scenario():
        async with LangChainRunner(model_config()) as runner:
            runner._model.structured.ainvoke.return_value = invalid
            return await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.MODEL_OUTPUT


def test_runner_maps_openai_error_to_model_call(monkeypatch):
    install_fakes(monkeypatch)

    async def scenario():
        async with LangChainRunner(model_config()) as runner:
            runner._model.structured.ainvoke.side_effect = OpenAIError("test-only-secret")
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.MODEL_CALL
    assert "test-only-secret" not in str(error.value)


@pytest.mark.parametrize(
    "exc",
    [ValidationError.from_exception_data("ParsedQuery", []), ValueError("bad")],
)
def test_runner_maps_parse_error_to_model_output(monkeypatch, exc):
    install_fakes(monkeypatch)

    async def scenario():
        async with LangChainRunner(model_config()) as runner:
            runner._model.structured.ainvoke.side_effect = exc
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.MODEL_OUTPUT


def test_runner_has_a_wall_clock_timeout(monkeypatch):
    install_fakes(monkeypatch)

    async def slow_invoke(*args, **kwargs):
        await asyncio.sleep(1)

    async def scenario():
        async with LangChainRunner(model_config(timeout_seconds=0.01)) as runner:
            runner._model.structured.ainvoke.side_effect = slow_invoke
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )

    with pytest.raises(SQLAgentError) as error:
        asyncio.run(scenario())
    assert error.value.code == ErrorCode.TIMEOUT


def test_runner_streams_text_chunks(monkeypatch):
    install_fakes(monkeypatch)

    async def scenario():
        async with LangChainRunner(model_config()) as runner:
            runner._model.stream_chunks = [
                AIMessageChunk(content="共 "),
                AIMessageChunk(content="275 "),
                AIMessageChunk(content="位艺术家。"),
            ]
            return [
                chunk
                async for chunk in runner.stream_text(
                    name="test", instructions="test", user_input="test"
                )
            ]

    assert asyncio.run(scenario()) == ["共 ", "275 ", "位艺术家。"]


def test_runner_refuses_calls_after_close(monkeypatch):
    install_fakes(monkeypatch)

    async def scenario():
        runner = LangChainRunner(model_config())
        await runner.close()
        await runner.close()
        assert runner._client.closed
        with pytest.raises(SQLAgentError) as error:
            await runner.run(
                name="test", instructions="test", user_input="test", output_type=ParsedQuery
            )
        assert error.value.code == ErrorCode.CONFIGURATION

    asyncio.run(scenario())


# --- bootstrap -----------------------------------------------------------


def test_bootstrap_langchain_closes_resources_on_failure(tmp_path, monkeypatch):
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

    monkeypatch.setattr("chatbi.sql_agent_langchain.bootstrap.SQLiteAdapter", DummyAdapter)
    monkeypatch.setattr("chatbi.sql_agent_langchain.bootstrap.LangChainRunner", DummyRunner)

    async def scenario():
        async with open_sql_agent_langchain(tmp_path, model_config=model_config()):
            raise RuntimeError("application failed")

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())
    assert resources["database"].closed and resources["model"].closed


def test_langchain_factory_drives_create_app(tmp_path, monkeypatch):
    from chatbi.api.app import create_app
    from fastapi.testclient import TestClient

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

    monkeypatch.setattr("chatbi.sql_agent_langchain.bootstrap.SQLiteAdapter", DummyAdapter)
    monkeypatch.setattr("chatbi.sql_agent_langchain.bootstrap.LangChainRunner", DummyRunner)
    monkeypatch.setattr(
        "chatbi.sql_agent_langchain.bootstrap.load_model_config", lambda root: model_config()
    )

    with TestClient(create_app(tmp_path, service_factory=open_sql_agent_langchain)) as client:
        assert client.get("/health").json() == {"status": "ready"}
        assert client.get("/docs").status_code == 200
    assert resources["database"].closed and resources["model"].closed
