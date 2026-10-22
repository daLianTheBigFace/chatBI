"""Result bounds and graceful synthesis failures without network calls."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from chatbi.api.app import create_app
from chatbi.sql_agent.config import WorkflowConfig
from chatbi.sql_agent.enums import ErrorCode, ResponseStatus
from chatbi.sql_agent.models import (
    QueryResult,
    SQLAgentRequest,
    SQLAgentResponse,
    SQLQuery,
    ValidationIssue,
)
from chatbi.sql_agent.service import SQLAgentService
from chatbi.sql_agent.summary import (
    PARTIAL_ANSWER,
    SAMPLE_ANSWER,
    SUMMARY_FALLBACK_WARNING,
    ModelAnswerSynthesisStrategy,
    SummaryAnswer,
    SummaryConfig,
    basic_answer,
)


class Runner:
    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return SummaryAnswer(answer="共 275 位艺术家。")


def result(rows=None, **kwargs):
    return QueryResult(
        columns=["total"], rows=[[275]] if rows is None else rows, execution_time_ms=1, **kwargs
    )


class Workflow:
    async def run(self, state):
        state.execution_count = 1
        return SQLAgentResponse(
            request_id=state.request_id,
            status=ResponseStatus.SUCCESS,
            query=SQLQuery(sql="SELECT COUNT(*) FROM artists"),
            result=result(),
        )


@pytest.mark.parametrize("rows,expected", [([], "没有查询"), ([[None]], "NULL"), ([[275]], "275")])
def test_basic_answer(rows, expected):
    assert expected in basic_answer(result(rows))


def test_summary_preserves_precision_and_bounds_model_input():
    runner = Runner()
    strategy = ModelAnswerSynthesisStrategy(runner, SummaryConfig(max_rows=2, max_input_bytes=400))
    original = result([[1.123456789], [2], [3]], truncated=True)
    answer = asyncio.run(strategy.synthesize("统计", original))
    payload = json.loads(runner.calls[0]["user_input"])
    assert payload["rows"] == [[1.123456789], [2]]
    assert payload["returned_row_count"] == 3
    assert payload["result_truncated"] and payload["summary_rows_omitted"]
    assert len(runner.calls[0]["user_input"].encode("utf-8")) <= 400
    assert SAMPLE_ANSWER in answer and PARTIAL_ANSWER in answer
    assert len(original.rows) == 3


@pytest.mark.parametrize("rows,question", [([["超长" * 1000]], "统计"), ([[1]], "问" * 1000)])
def test_oversized_values_or_question_skip_model(rows, question):
    runner = Runner()
    strategy = ModelAnswerSynthesisStrategy(runner, SummaryConfig(max_input_bytes=300))
    original = result(rows)
    assert asyncio.run(strategy.synthesize(question, original)) == basic_answer(original)
    assert not runner.calls


def test_byte_budget_can_reduce_rows_without_modifying_results():
    runner = Runner()
    strategy = ModelAnswerSynthesisStrategy(runner, SummaryConfig(max_input_bytes=320))
    original = result([["中" * 20], ["文" * 20], ["字" * 20]])
    answer = asyncio.run(strategy.synthesize("统计", original))
    payload = json.loads(runner.calls[0]["user_input"])
    assert 0 < len(payload["rows"]) < len(original.rows)
    assert SAMPLE_ANSWER in answer


@pytest.mark.parametrize("mode", ["error", "timeout", "invalid_output"])
def test_summary_failure_preserves_successful_sql_and_data(mode):
    class Synthesis:
        async def synthesize(self, question, data):
            data.rows.clear()
            if mode == "invalid_output":
                return " "
            if mode == "timeout":
                await asyncio.sleep(1)
            raise RuntimeError("private provider detail")

    service = SQLAgentService(
        Workflow(), WorkflowConfig(request_timeout_seconds=0.02), synthesis=Synthesis()
    )
    response = asyncio.run(service.ask(SQLAgentRequest(question="统计艺术家")))
    assert response.status == ResponseStatus.SUCCESS
    assert response.result.rows == [[275]] and "275" in response.answer
    assert response.query.sql == "SELECT COUNT(*) FROM artists"
    assert response.metadata.execution_count == 1
    assert response.warnings == [SUMMARY_FALLBACK_WARNING]
    assert "private" not in response.model_dump_json()


def test_cancellation_during_synthesis_propagates():
    async def scenario():
        started = asyncio.Event()

        class Synthesis:
            async def synthesize(self, question, data):
                started.set()
                await asyncio.Event().wait()

        service = SQLAgentService(Workflow(), WorkflowConfig(), synthesis=Synthesis())
        task = asyncio.create_task(service.ask(SQLAgentRequest(question="统计")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("status", list(ResponseStatus))
def test_api_contract_and_lifespan(tmp_path, status):
    events = []

    class Service:
        async def ask(self, request):
            events.append(request.question)
            fields = {}
            if status == ResponseStatus.SUCCESS:
                fields = {
                    "query": SQLQuery(sql="SELECT 275"),
                    "result": result(),
                    "answer": "共 275 位艺术家。",
                }
            elif status == ResponseStatus.NEEDS_CLARIFICATION:
                fields = {"clarification_questions": ["统计哪个表？"]}
            else:
                fields = {"error": ValidationIssue(code=ErrorCode.TIMEOUT, message="请求超时")}
            return SQLAgentResponse(request_id="test", status=status, **fields)

    @asynccontextmanager
    async def factory(root):
        assert root == tmp_path
        events.append("open")
        try:
            yield Service()
        finally:
            events.append("close")

    app = create_app(tmp_path, service_factory=factory)
    assert events == []
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ready"}
        assert client.get("/docs").status_code == 200
        response = client.post("/api/v1/query", json={"question": "统计"})
        assert response.status_code == 200 and response.json()["status"] == status
        assert client.post("/api/v1/query", json={"question": " "}).status_code == 422
        assert (
            client.post(
                "/api/v1/query", json={"question": "统计", "database_path": "other.db"}
            ).status_code
            == 422
        )
        assert events == ["open", "统计"]
    assert events == ["open", "统计", "close"]
    assert not hasattr(app.state, "sql_agent")


def test_startup_failure_prevents_serving(tmp_path):
    @asynccontextmanager
    async def factory(root):
        raise RuntimeError("startup failed")
        yield  # pragma: no cover

    with (
        pytest.raises(RuntimeError, match="startup failed"),
        TestClient(create_app(tmp_path, service_factory=factory)),
    ):
        pytest.fail("Startup must fail before accepting requests")
