"""Streaming lifecycle, privacy, retries, backpressure and disconnect regressions."""

import asyncio
import json
import sqlite3
from collections import deque
from contextlib import aclosing, asynccontextmanager, closing
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from chatbi.api.app import create_app
from chatbi.api.sse import ChatStreamingResponse, encode_event, event_frames
from chatbi.settings import ModelConfig
from chatbi.sql_agent.agent_models import GeneratedSQL, ParsedQuery
from chatbi.sql_agent.agents.runtime import AgentsSDKRunner
from chatbi.sql_agent.agents.stages import (
    ExplicitTableSelectionStrategy,
    QueryUnderstandingStage,
    SQLGenerationStage,
    SQLRepairStage,
)
from chatbi.sql_agent.config import AccessConfig, DatabaseConfig, ExecutionConfig, WorkflowConfig
from chatbi.sql_agent.database import DatabaseService, SQLiteAdapter
from chatbi.sql_agent.database.validator import default_validation_policy
from chatbi.sql_agent.enums import ErrorCode, ResponseStatus, UnderstandingStatus, WorkflowStage
from chatbi.sql_agent.errors import SQLAgentError
from chatbi.sql_agent.events import ChatEvent, ChatRequest, RunCompleted
from chatbi.sql_agent.models import QueryResult, SQLQuery
from chatbi.sql_agent.redaction import REDACTED, ConservativeDisplayRedaction
from chatbi.sql_agent.repair import BudgetedSQLRepairPolicy
from chatbi.sql_agent.service import SQLAgentService
from chatbi.sql_agent.streaming import EVENT_QUEUE_SIZE, EventChannel
from chatbi.sql_agent.summary import ModelAnswerSynthesisStrategy, SummaryAnswer
from chatbi.sql_agent.workflow import SQLAgentWorkflow

COUNT_SQL = "SELECT COUNT(*) AS total FROM people"
PRIVATE_QUERY = "SELECT email, customer_id, amount FROM people WHERE email = :email"
PRIVATE_EMAIL = "alice@example.com"


def parsed(**changes):
    return ParsedQuery(
        **(
            {
                "status": UnderstandingStatus.READY,
                "selected_tables": ["people"],
                "metrics": [],
                "dimensions": [],
                "filters": [],
                "time_range": None,
                "ordering": [],
                "top_n": None,
                "assumptions": [],
                "clarification_questions": [],
            }
            | changes
        )
    )


def candidate(sql=COUNT_SQL, parameters=None):
    return GeneratedSQL(sql=sql, parameters=parameters or [], assumptions=[])


class Runner:
    def __init__(self, outputs):
        self.outputs = deque(outputs)

    async def run(self, **kwargs):
        output = self.outputs.popleft()
        if isinstance(output, Exception):
            raise output
        return output


@pytest.fixture
def make_service(tmp_path):
    path = tmp_path / "stream.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE people(email TEXT, customer_id INTEGER, amount REAL);"
            "INSERT INTO people VALUES ('alice@example.com', 12345, 275);"
        )
    with DatabaseService(
        SQLiteAdapter(DatabaseConfig(project_root=tmp_path, path=path), ExecutionConfig())
    ) as database:

        def build(outputs=None, *, synthesis=None, config=None):
            runner = Runner(outputs if outputs is not None else [parsed(), candidate()])
            config = config or WorkflowConfig()
            access = AccessConfig()
            workflow = SQLAgentWorkflow(
                database=database,
                understanding=QueryUnderstandingStage(runner),
                selection=ExplicitTableSelectionStrategy(),
                generation=SQLGenerationStage(runner, access),
                repair=SQLRepairStage(runner, access),
                validation=default_validation_policy(ExecutionConfig()),
                repair_policy=BudgetedSQLRepairPolicy(config),
                config=config,
            )
            return SQLAgentService(workflow, config, synthesis=synthesis)

        yield build


async def collect(service, request=None):
    return [
        event
        async for event in service.stream(request or ChatRequest(question="统计人数"))
        if event is not None
    ]


def payloads(events, kind):
    return [event.payload for event in events if event.payload.type == kind]


def test_success_order_and_stable_ids(make_service):
    request = ChatRequest(question="统计人数", conversation_id=uuid4())
    events = asyncio.run(collect(make_service(), request))
    kinds = [event.payload.type for event in events]
    assert kinds[0] == "run.started" and kinds[-1] == "run.completed"
    assert kinds.index("sql.started") < kinds.index("sql.executing")
    assert kinds.index("sql.executing") < kinds.index("sql.completed")
    assert kinds.index("sql.completed") < kinds.index("answer.started")
    assert kinds.index("answer.started") < kinds.index("answer.completed")
    assert events[-1].payload.status == ResponseStatus.SUCCESS
    assert events[-1].payload.metadata.execution_count == 1
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.event_id for event in events}) == len(events)
    for name in ["conversation_id", "message_id", "run_id", "user_message_id"]:
        assert len({getattr(event, name) for event in events}) == 1
    assert events[0].conversation_id == request.conversation_id
    assert events[0].user_message_id == request.user_message_id
    assert (
        payloads(events, "sql.started")[0].tool_call_id
        == payloads(events, "sql.completed")[0].tool_call_id
    )
    for event in events:
        assert ChatEvent.model_validate_json(event.model_dump_json()) == event
        assert event.created_at.utcoffset().total_seconds() == 0


def test_repair_has_separate_sql_cards_and_validation_failure_is_not_execution(make_service):
    events = asyncio.run(
        collect(make_service([parsed(), candidate("SELECT missing FROM people"), candidate()]))
    )
    starts = payloads(events, "sql.started")
    assert [item.attempt for item in starts] == [1, 2]
    assert starts[0].tool_call_id != starts[1].tool_call_id
    assert payloads(events, "sql.failed")[0].tool_call_id == starts[0].tool_call_id
    assert payloads(events, "sql.failed")[0].phase == "validation"
    assert len(payloads(events, "sql.executing")) == 1
    assert payloads(events, "sql.completed")[0].tool_call_id == starts[1].tool_call_id
    assert events[-1].payload.metadata.repair_count == 1


def test_execution_error_card_and_retry(make_service):
    events = asyncio.run(
        collect(
            make_service([parsed(), candidate("SELECT SUM(SUM(amount)) FROM people"), candidate()])
        )
    )
    assert payloads(events, "sql.failed")[0].phase == "execution"
    assert len(payloads(events, "sql.executing")) == 2
    assert events[-1].payload.status == ResponseStatus.SUCCESS


@pytest.mark.parametrize("mode", ["clarification", "failure"])
def test_terminal_status_without_answer_or_sql_for_early_exit(make_service, mode):
    if mode == "clarification":
        outputs = [
            parsed(
                status=UnderstandingStatus.NEEDS_CLARIFICATION,
                selected_tables=[],
                clarification_questions=["请指定统计指标"],
            )
        ]
    else:
        outputs = [SQLAgentError(ErrorCode.MODEL_CALL, "private provider detail")]
    events = asyncio.run(collect(make_service(outputs)))
    assert not payloads(events, "sql.started")
    assert not payloads(events, "answer.started")
    assert len(payloads(events, "run.completed")) == 1
    assert "private provider detail" not in "".join(event.model_dump_json() for event in events)
    if mode == "clarification":
        assert events[-1].payload.clarification_questions == ["请指定统计指标"]
    else:
        assert payloads(events, "run.error")


def test_redacted_sql_results_and_model_input_never_modify_query_execution(make_service):
    class Summary:
        async def synthesize(self, question, result):
            assert PRIVATE_EMAIL not in question
            assert result.rows == [[REDACTED, REDACTED, 275.0]]
            return "已查询到匹配数据。"

    service = make_service(
        [parsed(), candidate(PRIVATE_QUERY, [{"name": "email", "value": PRIVATE_EMAIL}])],
        synthesis=Summary(),
    )
    events = asyncio.run(collect(service, ChatRequest(question=f"查询{PRIVATE_EMAIL}的记录")))
    combined = "".join(event.model_dump_json() for event in events)
    assert PRIVATE_EMAIL not in combined and "12345" not in combined
    assert events[-1].payload.status == ResponseStatus.SUCCESS
    assert payloads(events, "sql.completed")[0].result.rows == [[REDACTED, REDACTED, 275.0]]


def test_private_literals_hex_comments_and_adjacent_chinese_are_redacted():
    display = ConservativeDisplayRedaction()
    query = SQLQuery(
        sql="SELECT 'Alice', X'616263', 12345 /* password=private */ WHERE 1 = :value",
        parameters={"value": "alice@example.com"},
    )
    public = display.query(query)
    assert "Alice" not in public.sql and "616263" not in public.sql
    assert "12345" not in public.sql and "password" not in public.sql
    assert display.text("Alice的订单") == REDACTED + "的订单"
    assert display.text("共275行") == "共275行"
    assert "Alice" in query.sql and query.parameters["value"] == PRIVATE_EMAIL


def test_invalid_sql_display_never_falls_back_to_raw_query():
    display = ConservativeDisplayRedaction()
    assert "secret" not in display.query(SQLQuery(sql="SELECT 'secret")).sql


@pytest.mark.parametrize("fail", [False, True])
def test_model_deltas_scrub_across_chunks_and_final_answer_replaces_partial(make_service, fail):
    class Summary:
        async def synthesize(self, question, result):
            pytest.fail("Streaming strategy should be selected")

        async def stream_synthesize(self, question, result):
            yield "联系 alice@"
            yield "example.com\n"
            if fail:
                raise SQLAgentError(ErrorCode.MODEL_CALL, "private error")
            yield "总数为1。"

    events = asyncio.run(collect(make_service(synthesis=Summary())))
    assert PRIVATE_EMAIL not in "".join(event.model_dump_json() for event in events)
    assert REDACTED in payloads(events, "answer.delta")[0].delta
    answer = payloads(events, "answer.completed")[0]
    assert answer.replace
    if fail:
        assert "查询结果：1" in answer.text
        assert payloads(events, "warning")
    else:
        assert "总数为1" in answer.text
    assert events[-1].payload.status == ResponseStatus.SUCCESS


def test_summary_timeout_preserves_data_and_finishes_answer_block(make_service):
    class Summary:
        async def synthesize(self, question, result):
            await asyncio.sleep(1)

    events = asyncio.run(
        collect(
            make_service(synthesis=Summary(), config=WorkflowConfig(request_timeout_seconds=0.08))
        )
    )
    assert payloads(events, "sql.completed")
    assert payloads(events, "answer.completed")
    assert payloads(events, "warning")
    assert events[-1].payload.status == ResponseStatus.SUCCESS


def test_close_stream_cancels_inflight_summary(make_service):
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        class Summary:
            async def synthesize(self, question, result):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        service = make_service(synthesis=Summary())
        async with aclosing(service.stream(ChatRequest(question="统计"))) as stream:
            async for event in stream:
                if event is not None and event.payload.type == "answer.started":
                    await started.wait()
                    break
        assert stopped.is_set()

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))


def test_delta_arrives_before_model_finishes(make_service):
    async def scenario():
        release, waiting = asyncio.Event(), asyncio.Event()

        class Summary:
            async def synthesize(self, question, result):
                pytest.fail("Should stream")

            async def stream_synthesize(self, question, result):
                yield "第一段\n"
                waiting.set()
                await release.wait()
                yield "结束"

        async with aclosing(
            make_service(synthesis=Summary()).stream(ChatRequest(question="统计"))
        ) as stream:
            async for event in stream:
                if event is not None and event.payload.type == "answer.delta":
                    assert event.payload.delta == "第一段\n"
                    await waiting.wait()
                    assert not release.is_set()
                    release.set()
                    break
            remaining = [event async for event in stream if event is not None]
            assert remaining[-1].payload.status == ResponseStatus.SUCCESS

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))


def test_disconnect_closes_asgi_23_task_group(make_service):
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        class Summary:
            async def synthesize(self, question, result):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        async def receive():
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        response = ChatStreamingResponse(
            event_frames(make_service(synthesis=Summary()).stream(ChatRequest(question="统计"))),
            media_type="text/event-stream",
        )
        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
        assert stopped.is_set()

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))


def test_execution_timeout_finishes_pending_sql_card(make_service):
    service = make_service()

    async def slow_execute(query):
        await asyncio.sleep(1)

    service._config = WorkflowConfig(request_timeout_seconds=0.08)
    service._workflow._database.execute_query = slow_execute
    events = asyncio.run(collect(service))
    assert payloads(events, "sql.executing")
    assert payloads(events, "sql.failed")[0].code == ErrorCode.TIMEOUT
    assert not payloads(events, "sql.completed")
    assert events[-1].payload.status == ResponseStatus.FAILED


def test_heartbeat_during_model_wait_and_no_events_after_terminal(make_service, monkeypatch):
    monkeypatch.setattr("chatbi.sql_agent.streaming.HEARTBEAT_SECONDS", 0.01)

    class Summary:
        async def synthesize(self, question, result):
            await asyncio.sleep(0.05)
            return "共1条记录。"

    async def scenario():
        return [
            event
            async for event in make_service(synthesis=Summary()).stream(
                ChatRequest(question="统计")
            )
        ]

    events = asyncio.run(scenario())
    assert None in events
    assert isinstance(events[-1].payload, RunCompleted)


def test_bounded_channel_backpressure():
    async def scenario():
        channel = EventChannel(ChatRequest(question="统计"), ConservativeDisplayRedaction())
        for _ in range(EVENT_QUEUE_SIZE):
            await channel.progress(WorkflowStage.SCHEMA)
        blocked = asyncio.create_task(channel.progress(WorkflowStage.SCHEMA))
        await asyncio.sleep(0)
        assert not blocked.done()
        await channel.queue.get()
        await blocked
        assert channel.queue.qsize() == EVENT_QUEUE_SIZE

    asyncio.run(scenario())


def test_concurrent_runs_have_isolated_ids_and_redaction(make_service):
    async def scenario():
        return await asyncio.gather(collect(make_service()), collect(make_service()))

    first, second = asyncio.run(scenario())
    assert first[0].conversation_id != second[0].conversation_id
    assert first[0].message_id != second[0].message_id
    assert first[0].run_id != second[0].run_id
    assert first[0].sequence == second[0].sequence == 1


def test_http_sse_schema_and_bad_body_validation(make_service, tmp_path):
    @asynccontextmanager
    async def factory(root):
        yield make_service()

    with TestClient(create_app(tmp_path, service_factory=factory)) as client:
        response = client.post("/api/v1/chat/stream", json={"question": "统计"})
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        records = response.text.strip().split("\n\n")
        events = []
        for record in records:
            lines = record.splitlines()
            event = ChatEvent.model_validate_json(lines[2].removeprefix("data: "))
            assert lines[0] == f"id: {event.event_id}"
            assert lines[1] == f"event: {event.payload.type}"
            events.append(event)
        assert events[-1].payload.status == ResponseStatus.SUCCESS
        assert client.get("/api/v1/chat/events/schema").json()["$defs"]["SQLStarted"]
        for body in [
            {"question": " "},
            {"question": "统计", "conversation_id": "invalid"},
            {"question": "统计", "sql": "DELETE FROM people"},
        ]:
            assert client.post("/api/v1/chat/stream", json=body).status_code == 422
        assert (
            "text/event-stream"
            in client.get("/openapi.json").json()["paths"]["/api/v1/chat/stream"]["post"][
                "responses"
            ]["200"]["content"]
        )


def test_invalid_event_payload_rejected():
    with pytest.raises(ValidationError):
        ChatEvent.model_validate(
            {
                "sequence": 1,
                "conversation_id": str(uuid4()),
                "message_id": str(uuid4()),
                "user_message_id": str(uuid4()),
                "run_id": str(uuid4()),
                "payload": {"type": "unknown"},
            }
        )


def test_sse_json_escapes_multiline_content():
    channel = EventChannel(ChatRequest(question="统计"), ConservativeDisplayRedaction())
    asyncio.run(channel.progress(WorkflowStage.SCHEMA))
    event = channel.queue.get_nowait()
    event.payload.text = "第一行\n\nevent: fake"
    frame = encode_event(event)
    assert len(frame.strip().splitlines()) == 3
    assert json.loads(frame.splitlines()[2][6:])["payload"]["text"] == event.payload.text


def test_asgi_send_disconnect_closes_event_generator():
    async def scenario():
        closed = asyncio.Event()

        async def events():
            try:
                yield None
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        response = ChatStreamingResponse(event_frames(events()), media_type="text/event-stream")
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert closed.is_set()

    asyncio.run(scenario())


def test_sdk_stream_filters_reasoning_and_closes_on_cancel(monkeypatch):
    captured = {}

    class Result:
        is_complete = False

        def cancel(self):
            captured["cancelled"] = True

        async def stream_events(self):
            try:
                yield SimpleNamespace(
                    type="raw_response_event",
                    data=SimpleNamespace(
                        type="response.reasoning_text.delta", delta="private reasoning"
                    ),
                )
                yield SimpleNamespace(
                    type="raw_response_event",
                    data=SimpleNamespace(type="response.output_text.delta", delta="public text"),
                )
                await asyncio.Event().wait()
            finally:
                captured["closed"] = True

    def run_streamed(agent, **kwargs):
        captured.update(kwargs)
        captured["agent"] = agent
        return Result()

    monkeypatch.setattr("chatbi.sql_agent.agents.runtime.Runner.run_streamed", run_streamed)

    async def scenario():
        config = ModelConfig(api_key="test", base_url="https://example.test/v1", model="test")
        async with (
            AgentsSDKRunner(config) as runner,
            aclosing(
                runner.stream_text(name="test", instructions="test", user_input="test")
            ) as chunks,
        ):
            assert await anext(chunks) == "public text"

    asyncio.run(scenario())
    assert captured["cancelled"] and captured["closed"]
    assert captured["run_config"].tracing_disabled
    assert captured["agent"].tools == [] and captured["agent"].output_type is None


def test_summary_stream_is_native_text_and_handles_scope_suffix():
    class StreamingRunner:
        async def run(self, **kwargs):
            pytest.fail("Should use native text streaming")

        async def stream_text(self, **kwargs):
            assert "JSON" in kwargs["instructions"]
            yield "第一段\n"
            yield "第二段"

    async def scenario():
        strategy = ModelAnswerSynthesisStrategy(StreamingRunner())
        data = QueryResult(columns=["total"], rows=[[1]], truncated=True, execution_time_ms=0)
        return [chunk async for chunk in strategy.stream_synthesize("统计", data)]

    chunks = asyncio.run(scenario())
    assert chunks[0] == "第一段\n" and chunks[1] == "第二段"
    assert "截断" in chunks[2]


def test_summary_stream_supports_nonstreaming_test_runner():
    async def scenario():
        strategy = ModelAnswerSynthesisStrategy(Runner([SummaryAnswer(answer="共1条。")]))
        data = QueryResult(columns=["total"], rows=[[1]], execution_time_ms=0)
        return [chunk async for chunk in strategy.stream_synthesize("统计", data)]

    assert asyncio.run(scenario()) == ["共1条。"]
