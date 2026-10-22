"""Run via: uv run streamlit run src/chatbi/frontend/app.py."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import pandas as pd
import streamlit as st
from pydantic import ValidationError
from streamlit.delta_generator import DeltaGenerator

from chatbi.frontend.client import DEFAULT_API_URL, StreamError, open_chat_stream
from chatbi.frontend.conversation import (
    MAX_VISIBLE_TURNS,
    ConversationTurn,
    conversation_history,
)
from chatbi.sql_agent.enums import ResponseStatus
from chatbi.sql_agent.events import (
    AnswerCompleted,
    AnswerDelta,
    AnswerStarted,
    ChatEvent,
    ChatRequest,
    Progress,
    RunCompleted,
    RunError,
    RunStarted,
    SQLCompleted,
    SQLExecuting,
    SQLFailed,
    SQLStarted,
    WarningEvent,
)

PAGE_TITLE = "ChatBI · SSE 测试"
PAGE_ICON = "💬"
WELCOME = "提一个关于 Chinook 的问题"
INTRO = "观察 SQL 查询的实时过程，支持在当前对话中继续追问。"
INPUT_HINT = "例如：统计 artists 表中的艺术家总数。"
BACKEND_LABEL = "FastAPI 后端地址"
DEBUG_LABEL = "显示事件调试信息"
SESSION_NOTE = "当前页面会话内保留最近 10 轮问答；不写入数据库，点击新对话可清空。"
TURNS_KEY = "chatbi_turns"
CONVERSATION_KEY = "chatbi_conversation_id"
TRANSPORT_KEY = "chatbi_transport"
NEW_CONVERSATION_LABEL = "新对话"
WAITING = "正在连接后端…"
ANSWER_WAITING = "正在整理回答…"
STATUS_LABELS = {
    ResponseStatus.SUCCESS: "查询完成",
    ResponseStatus.NEEDS_CLARIFICATION: "需要补充条件",
    ResponseStatus.FAILED: "查询失败",
}
REQUEST_FAILURE = "请求未完成"
QUERY_STATUS = "第 {attempt} 次 SQL · {status}"
VALIDATING = "校验中"
EXECUTING = "执行中"
SUCCEEDED = "执行成功"
FAILED = "失败"
MAX_DEBUG_EVENTS = 200


@dataclass
class SQLCard:
    status: DeltaGenerator
    result: DeltaGenerator
    attempt: int


@dataclass
class ChatView:
    """Render one live or replayed run without invoking the backend on replay."""

    status: DeltaGenerator
    process: DeltaGenerator
    answer: DeltaGenerator
    notices: DeltaGenerator
    diagnostics: DeltaGenerator
    show_debug: bool
    cards: dict[UUID, SQLCard] = field(default_factory=dict)
    answers: dict[UUID, str] = field(default_factory=dict)
    raw_events: list[dict] = field(default_factory=list)
    completed: bool = False
    run_started_at: datetime | None = None
    first_answer_seconds: float | None = None
    answer_delta_count: int = 0

    def render(self, event: ChatEvent) -> None:
        payload = event.payload
        match payload:
            case RunStarted():
                self.run_started_at = event.created_at
                self.status.update(label="已连接，开始处理")
            case Progress():
                self.status.update(label=payload.text)
                self.process.caption(payload.text)
            case SQLStarted():
                with self.process:
                    card = st.expander(f"SQL {payload.attempt}", expanded=True)
                    with card:
                        status = st.empty()
                        status.info(QUERY_STATUS.format(attempt=payload.attempt, status=VALIDATING))
                        st.code(payload.query.sql, language="sql")
                        st.caption("展示内容已脱敏；SQL 字面量和参数值已隐藏。")
                        if payload.query.parameter_names:
                            st.write({"参数名称": payload.query.parameter_names})
                        result = st.empty()
                    self.cards[payload.tool_call_id] = SQLCard(status, result, payload.attempt)
            case SQLExecuting():
                card = self.cards[payload.tool_call_id]
                card.status.info(QUERY_STATUS.format(attempt=card.attempt, status=EXECUTING))
            case SQLCompleted():
                card = self.cards[payload.tool_call_id]
                card.status.success(QUERY_STATUS.format(attempt=card.attempt, status=SUCCEEDED))
                data = payload.result
                with card.result.container():
                    # Unique headers preserve positional data even with duplicate SQL aliases.
                    headers = [f"{i + 1} · {name}" for i, name in enumerate(data.columns)]
                    st.dataframe(pd.DataFrame(data.rows, columns=headers), hide_index=True)
                    st.caption(f"返回 {len(data.rows)} 行 · 执行 {data.execution_time_ms:.1f} ms")
                    if data.truncated:
                        st.warning("结果已达到返回预算，当前只展示部分数据。")
            case SQLFailed():
                card = self.cards[payload.tool_call_id]
                card.status.error(QUERY_STATUS.format(attempt=card.attempt, status=FAILED))
                card.result.warning(f"{payload.text}（{payload.phase} / {payload.code}）")
            case AnswerStarted():
                self.answers[payload.block_id] = ""
                self.answer.caption(ANSWER_WAITING)
            case AnswerDelta():
                self.answer_delta_count += 1
                if self.first_answer_seconds is None and self.run_started_at is not None:
                    self.first_answer_seconds = (
                        event.created_at - self.run_started_at
                    ).total_seconds()
                self.answers[payload.block_id] += payload.delta
                self.answer.text(self.answers[payload.block_id])
            case AnswerCompleted():
                self.answers[payload.block_id] = payload.text
                self.answer.text(payload.text)
            case WarningEvent():
                self.notices.warning(payload.text)
            case RunError():
                self.notices.error(f"{payload.text}（{payload.code}）")
            case RunCompleted():
                self.completed = True
                self.status.update(
                    label=STATUS_LABELS[payload.status],
                    state="error" if payload.status == ResponseStatus.FAILED else "complete",
                    expanded=False,
                )
                for question in payload.clarification_questions:
                    self.notices.info(question)
                meta = payload.metadata
                self.notices.caption(
                    f"执行 {meta.execution_count} 次 · 修复 {meta.repair_count} 次 · "
                    f"总耗时 {meta.elapsed_ms / 1000:.2f} s"
                )
        if self.show_debug:
            self.raw_events.append(event.model_dump(mode="json"))
            self.raw_events = self.raw_events[-MAX_DEBUG_EVENTS:]
            with self.diagnostics.container():
                st.caption(f"run_id: {event.run_id} · message_id: {event.message_id}")
                if self.first_answer_seconds is not None:
                    st.caption(
                        f"正文分片 {self.answer_delta_count} 个 · "
                        f"后端首段正文 {self.first_answer_seconds:.2f} s"
                    )
                st.json(self.raw_events, expanded=False)


def render_turn(turn: ConversationTurn, show_debug: bool) -> ChatView:
    with st.chat_message("user"):
        st.text(turn.question)
    with st.chat_message("assistant"):
        status = st.status(WAITING, expanded=True)
        view = ChatView(
            status,
            status.container(),
            st.empty(),
            st.container(),
            st.expander("事件调试", expanded=False),
            show_debug,
        )
        for event in turn.events:
            view.render(event)
        if turn.error:
            view.status.update(label=REQUEST_FAILURE, state="error")
            view.notices.error(turn.error)
        return view


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, page_icon=PAGE_ICON, layout="wide")
    st.title(PAGE_TITLE)
    st.caption(INTRO)
    if TURNS_KEY not in st.session_state:
        st.session_state[TURNS_KEY] = []
        st.session_state[CONVERSATION_KEY] = None
    turns = st.session_state[TURNS_KEY]
    with st.sidebar:
        st.header("连接设置")
        base_url = st.text_input(BACKEND_LABEL, value=DEFAULT_API_URL)
        show_debug = st.checkbox(DEBUG_LABEL, value=False)
        if st.button(NEW_CONVERSATION_LABEL):
            turns.clear()
            st.session_state[CONVERSATION_KEY] = None
            st.session_state.pop(TRANSPORT_KEY, None)
        st.caption(SESSION_NOTE)
        st.caption("正文分片实时更新；脱敏可能暂存未完整的词或句子，不保证逐字显示。")
        if show_debug:
            st.caption("Python 服务端消费 SSE，浏览器与 Streamlit 之间使用 WebSocket。")
            if TRANSPORT_KEY in st.session_state:
                st.json(st.session_state[TRANSPORT_KEY])
    for turn in turns:
        render_turn(turn, show_debug)
    prompt = st.chat_input(INPUT_HINT, max_chars=8000)
    if not prompt:
        if not turns:
            st.info(WELCOME)
        return
    history = conversation_history(turns)
    turn = ConversationTurn(question=prompt)
    turns.append(turn)
    del turns[:-MAX_VISIBLE_TURNS]
    view = render_turn(turn, show_debug)

    def transport_received(info: dict[str, str]) -> None:
        st.session_state[TRANSPORT_KEY] = info
        if show_debug:
            view.notices.caption(f"后端响应 Content-Type: {info['response_content_type']}")

    try:
        request = ChatRequest(
            question=prompt,
            history=history,
            conversation_id=st.session_state[CONVERSATION_KEY],
        )
        with open_chat_stream(base_url, request, on_response=transport_received) as events:
            for event in events:
                st.session_state[CONVERSATION_KEY] = event.conversation_id
                if not isinstance(event.payload, AnswerDelta):
                    turn.events.append(event)
                view.render(event)
    except (StreamError, ValidationError) as exc:
        turn.error = str(exc) if isinstance(exc, StreamError) else "请输入有效的问题。"
    except (KeyError, TypeError):
        turn.error = "收到无法关联到展示组件的事件，请检查协议顺序。"
    if turn.error:
        view.status.update(label=REQUEST_FAILURE, state="error")
        view.notices.error(turn.error)


if __name__ == "__main__":
    main()
