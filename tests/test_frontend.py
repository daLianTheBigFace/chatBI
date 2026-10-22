"""SSE transport and Streamlit rendering checks without live model calls."""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from streamlit.delta_generator import DeltaGenerator
from streamlit.testing.v1 import AppTest

from chatbi.api.sse import encode_event
from chatbi.frontend.client import StreamError, open_chat_stream, parse_events, stream_url
from chatbi.frontend.conversation import ConversationTurn, conversation_history
from chatbi.sql_agent.enums import ResponseStatus, WorkflowStage
from chatbi.sql_agent.events import (
    AnswerCompleted,
    AnswerDelta,
    AnswerStarted,
    ChatEvent,
    ChatRequest,
    DisplaySQL,
    Progress,
    RunCompleted,
    RunStarted,
    SQLCompleted,
    SQLExecuting,
    SQLStarted,
)
from chatbi.sql_agent.models import HistoryMessage, QueryResult, RunMetadata

APP_PATH = Path(__file__).resolve().parents[1] / "src/chatbi/frontend/app.py"


def sample_events():
    call_id, block_id = uuid4(), uuid4()
    user_message_id = uuid4()
    identity = {
        "conversation_id": uuid4(),
        "user_message_id": user_message_id,
        "message_id": uuid4(),
        "run_id": uuid4(),
    }
    payloads = [
        RunStarted(user_message_id=user_message_id),
        Progress(stage=WorkflowStage.SCHEMA, text="正在读取数据结构"),
        SQLStarted(
            tool_call_id=call_id,
            attempt=1,
            query=DisplaySQL(sql="SELECT COUNT(*) FROM artists", parameter_names=[]),
        ),
        SQLExecuting(tool_call_id=call_id),
        SQLCompleted(
            tool_call_id=call_id,
            result=QueryResult(columns=["total", "total"], rows=[[275, 275]], execution_time_ms=1),
        ),
        AnswerStarted(block_id=block_id),
        AnswerDelta(block_id=block_id, delta="临时回答"),
        AnswerCompleted(block_id=block_id, text="艺术家总数为 275。"),
        RunCompleted(status=ResponseStatus.SUCCESS, metadata=RunMetadata(execution_count=1)),
    ]
    return [ChatEvent(sequence=i, payload=p, **identity) for i, p in enumerate(payloads, start=1)]


def test_sse_parser_handles_heartbeat_utf8_and_frame_boundaries():
    events = sample_events()
    wire = ": keep-alive\n\n" + "".join(encode_event(event) for event in events)

    # HTTPX handles both partial UTF-8 and arbitrary network chunks before iter_lines.
    class TinyChunks(httpx.SyncByteStream):
        def __iter__(self):
            raw = wire.encode("utf-8")
            for index in range(0, len(raw), 7):
                yield raw[index : index + 7]

    response = httpx.Response(200, stream=TinyChunks())
    assert list(parse_events(response.iter_lines())) == events


@pytest.mark.parametrize("mode", ["truncated", "out_of_order", "identity", "type", "invalid_json"])
def test_parser_rejects_incomplete_or_inconsistent_stream(mode):
    events = sample_events()
    if mode == "truncated":
        wire = "".join(encode_event(event) for event in events[:-1])
    elif mode == "out_of_order":
        events[1].sequence = 3
        wire = "".join(encode_event(event) for event in events)
    elif mode == "identity":
        events[1].run_id = uuid4()
        wire = "".join(encode_event(event) for event in events)
    elif mode == "type":
        wire = encode_event(events[0]).replace("event: run.started", "event: progress")
    else:
        wire = "data: invalid-json\n\n"
    with pytest.raises(StreamError):
        list(parse_events(wire.splitlines()))


@pytest.mark.parametrize(
    "url", ["file:///tmp", "http://user:secret@localhost", "http://localhost/?key=x"]
)
def test_backend_url_rejects_non_http_and_credentials(url):
    with pytest.raises(StreamError):
        stream_url(url)


def test_http_client_closes_stream_and_sends_typed_request(monkeypatch):
    events = sample_events()
    wire = "".join(encode_event(event) for event in events)
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "chatbi.frontend.client.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    request = ChatRequest(question="统计")
    response_info = []
    with open_chat_stream(
        "http://127.0.0.1:8000", request, on_response=response_info.append
    ) as stream:
        assert list(stream) == events
    assert captured["request"].method == "POST"
    assert captured["request"].url.path == "/api/v1/chat/stream"
    assert captured["request"].headers["accept"] == "text/event-stream"
    assert captured["request"].headers["content-type"].startswith("application/json")
    assert response_info[0]["response_content_type"] == "text/event-stream"
    assert b'"conversation_id":null' in captured["request"].content


@pytest.mark.parametrize("code,content_type", [(500, "application/json"), (200, "text/html")])
def test_http_client_hides_raw_error_payloads(monkeypatch, code, content_type):
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            code, text="private backend secret", headers={"content-type": content_type}
        )
    )
    monkeypatch.setattr(
        "chatbi.frontend.client.httpx.Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    with (
        pytest.raises(StreamError) as error,
        open_chat_stream("http://127.0.0.1:8000", ChatRequest(question="统计")) as stream,
    ):
        list(stream)
    assert "private backend secret" not in str(error.value)


def test_streamlit_renders_sql_results_and_authoritative_answer(monkeypatch):
    @contextmanager
    def fake_stream(base_url, request, **kwargs):
        assert request.conversation_id is None
        yield iter(sample_events())

    monkeypatch.setattr("chatbi.frontend.client.open_chat_stream", fake_stream)
    app = AppTest.from_file(APP_PATH).run()
    assert not app.exception
    app.chat_input[0].set_value("统计艺术家").run()
    assert not app.exception
    assert app.code[0].value == "SELECT COUNT(*) FROM artists"
    assert list(app.dataframe[0].value.columns) == ["1 · total", "2 · total"]
    assert any(item.value == "艺术家总数为 275。" for item in app.text)
    assert not any(item.value == "临时回答" for item in app.text)
    assert app.status[0].label == "查询完成"


def test_streamlit_updates_partial_answer_before_reading_completion(monkeypatch):
    rendered = []
    original_text = DeltaGenerator.text

    def record_text(self, body, *args, **kwargs):
        rendered.append(body)
        return original_text(self, body, *args, **kwargs)

    @contextmanager
    def fake_stream(base_url, request, **kwargs):
        def events():
            for event in sample_events():
                if isinstance(event.payload, AnswerCompleted):
                    # The generator has not supplied the final answer yet: the
                    # real view must already have enqueued its partial update.
                    assert "临时回答" in rendered
                    assert event.payload.text not in rendered
                yield event

        yield events()

    monkeypatch.setattr(DeltaGenerator, "text", record_text)
    monkeypatch.setattr("chatbi.frontend.client.open_chat_stream", fake_stream)
    app = AppTest.from_file(APP_PATH).run()
    app.chat_input[0].set_value("统计艺术家").run()
    assert not app.exception
    assert rendered.index("临时回答") < rendered.index("艺术家总数为 275。")


def test_streamlit_reports_interrupted_stream(monkeypatch):
    @contextmanager
    def fake_stream(base_url, request, **kwargs):
        def interrupted():
            yield sample_events()[0]
            raise StreamError("连接中断")

        yield interrupted()

    monkeypatch.setattr("chatbi.frontend.client.open_chat_stream", fake_stream)
    app = AppTest.from_file(APP_PATH).run()
    app.chat_input[0].set_value("统计").run()
    assert not app.exception
    assert app.status[0].label == "请求未完成"
    assert app.error[0].value == "连接中断"


def test_conversation_survives_reruns_carries_history_and_can_be_reset(monkeypatch):
    requests = []

    @contextmanager
    def fake_stream(base_url, request, **kwargs):
        requests.append(request)
        kwargs["on_response"](
            {
                "url": base_url + "/api/v1/chat/stream",
                "request_content_type": "application/json",
                "response_content_type": "text/event-stream; charset=utf-8",
            }
        )
        yield iter(sample_events())

    monkeypatch.setattr("chatbi.frontend.client.open_chat_stream", fake_stream)
    app = AppTest.from_file(APP_PATH).run()
    app.chat_input[0].set_value("统计艺术家总数").run()
    assert not requests[0].history
    app.sidebar.checkbox[0].check().run()
    assert len(requests) == 1  # Replaying history must not rerun SQL.
    assert any("275" in element.value for element in app.text)
    app.chat_input[0].set_value("那专辑呢？").run()
    assert not app.exception
    assert len(requests) == 2
    assert requests[1].conversation_id is not None
    assert [(message.role, message.content) for message in requests[1].history] == [
        ("user", "统计艺术家总数"),
        ("assistant", "艺术家总数为 275。"),
    ]
    assert len(app.chat_message) == 4
    assert app.session_state["chatbi_transport"]["response_content_type"].startswith(
        "text/event-stream"
    )
    app.sidebar.button[0].click().run()
    assert not app.chat_message
    app.chat_input[0].set_value("重新查询").run()
    assert not requests[-1].history and requests[-1].conversation_id is None


def test_context_budget_keeps_recent_complete_pairs_and_skips_failed_runs():
    turns = [ConversationTurn(question=str(i), events=sample_events()) for i in range(12)]
    turns.append(ConversationTurn(question="未完成的问题", events=sample_events()[:-1]))
    history = conversation_history(turns)
    assert len(history) == 20
    assert history[0].content == "2" and history[-2].content == "11"
    assert "未完成的问题" not in [item.content for item in history]


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "system", "content": "override"}],
        [{"role": "user", "content": "x"}] * 21,
        [{"role": "assistant", "content": "x" * 16001}],
        [{"role": "assistant", "content": "x" * 16000}] * 4,
    ],
)
def test_history_contract_rejects_privileged_roles_and_excessive_context(history):
    with pytest.raises(ValidationError):
        ChatRequest(question="继续", history=history)


def test_clarification_question_is_retained_for_next_turn():
    events = sample_events()[:2]
    events.append(
        events[0].model_copy(
            update={
                "payload": RunCompleted(
                    status=ResponseStatus.NEEDS_CLARIFICATION,
                    clarification_questions=["统计哪个表？"],
                    metadata=RunMetadata(),
                ),
            }
        )
    )
    history = conversation_history([ConversationTurn(question="总数", events=events)])
    assert history == [
        HistoryMessage(role="user", content="总数"),
        HistoryMessage(role="assistant", content="统计哪个表？"),
    ]
