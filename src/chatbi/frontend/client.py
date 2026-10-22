"""Synchronous SSE consumer with validated event ordering and explicit completion."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from chatbi.sql_agent.events import ChatEvent, ChatRequest

DEFAULT_API_URL = "http://127.0.0.1:8000"
STREAM_PATH = "/api/v1/chat/stream"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 120.0
MAX_FRAME_CHARACTERS = 4 * 1024 * 1024
INVALID_EVENT = "SSE 事件格式不符合约定，请检查前后端版本。"
INTERRUPTED = "连接已中断，未收到 run.completed，本次请求不能视为成功。"


class StreamError(Exception):
    """Safe message suitable for display without backend payload details."""


def stream_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    try:
        parts = urlsplit(base_url)
        valid = (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise StreamError("请输入有效的 HTTP/HTTPS 后端地址，不含凭据、查询参数或片段。")
    return base_url + STREAM_PATH


def parse_events(lines: Iterable[str]) -> Iterator[ChatEvent]:
    data: list[str] = []
    event_type = event_id = None
    frame_size = 0
    sequence = 0
    identity = None

    for line in lines:
        if line.startswith(":"):
            continue
        if line:
            frame_size += len(line)
            if frame_size > MAX_FRAME_CHARACTERS:
                raise StreamError("单个 SSE 事件超过展示预算。")
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "data":
                data.append(value)
            elif field == "event":
                event_type = value
            elif field == "id":
                event_id = value
            continue
        if not data:
            event_type = event_id = None
            frame_size = 0
            continue
        try:
            event = ChatEvent.model_validate_json("\n".join(data))
        except ValidationError:
            raise StreamError(INVALID_EVENT) from None
        current_identity = (
            event.conversation_id,
            event.user_message_id,
            event.message_id,
            event.run_id,
        )
        if (
            event.sequence != sequence + 1
            or (sequence == 0 and event.payload.type != "run.started")
            or (identity is not None and identity != current_identity)
            or event_type != event.payload.type
            or event_id != str(event.event_id)
        ):
            raise StreamError("SSE 序号、消息标识或事件类型不一致，已停止展示。")
        sequence = event.sequence
        identity = current_identity
        yield event
        if event.payload.type == "run.completed":
            return
        data = []
        event_type = event_id = None
        frame_size = 0
    raise StreamError(INTERRUPTED)


@contextmanager
def open_chat_stream(
    base_url: str,
    request: ChatRequest,
    *,
    on_response: Callable[[dict[str, str]], None] | None = None,
) -> Iterator[Iterator[ChatEvent]]:
    url = stream_url(base_url)
    timeout = httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    try:
        with (
            httpx.Client(timeout=timeout, trust_env=False) as client,
            client.stream(
                "POST",
                url,
                json=request.model_dump(mode="json"),
                headers={"Accept": "text/event-stream"},
            ) as response,
        ):
            if response.status_code != httpx.codes.OK:
                raise StreamError(f"后端返回 HTTP {response.status_code}，请检查服务及请求配置。")
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                raise StreamError("后端未返回 SSE，请确认地址指向 ChatBI FastAPI 服务。")
            if on_response is not None:
                on_response(
                    {
                        "url": url,
                        "request_content_type": "application/json",
                        "response_content_type": response.headers["content-type"],
                    }
                )
            yield parse_events(response.iter_lines())
    except httpx.TimeoutException:
        raise StreamError("等待后端超时，请检查服务状态后重试。") from None
    except httpx.HTTPError:
        raise StreamError("无法连接后端或流已中断，请确认 FastAPI 服务已启动。") from None
