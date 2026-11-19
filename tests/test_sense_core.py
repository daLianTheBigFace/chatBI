"""sense 内核的单元测试：判定解析、拆段排plan、历史、重试策略。

不碰网络、不碰 Redis：这些东西一旦进单测，跑一次要十几秒，很快就没人跑了。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from chatbi.sense.contracts import (
    Emotion,
    ReplyJob,
    ReplyMode,
    ReplyPlan,
    ReplyResult,
    Segment,
    SenseRequest,
    SenseResult,
    parse_verdict,
)
from chatbi.sense.history import append, connect, list_sessions, load_turns
from chatbi.sense.planning import compose, derive_plans, locate
from chatbi.sense.worker import _fail, _handle_reply

# ---------------------------------------------------------------- 判定解析


def test_parse_verdict_plain_json() -> None:
    raw = '{"label": "negative", "score": 0.8, "evidence": ["烦"]}'
    verdict = parse_verdict(raw)
    assert verdict.label is Emotion.NEGATIVE
    assert verdict.score == 0.8


def test_parse_verdict_with_fence_and_prose() -> None:
    raw = '好的，结果如下：\n```json\n{"label": "positive", "score": 0.7}\n```\n'
    assert parse_verdict(raw).label is Emotion.POSITIVE


def test_parse_verdict_fills_missing_score() -> None:
    verdict = parse_verdict('{"label": "neutral"}')
    assert verdict.score == 0.5


def test_parse_verdict_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_verdict("我觉得是负面")


# ---------------------------------------------------------------- 拆段与排序
def _verdict(label: Emotion, segments: list[tuple[str, str, float]]) -> SenseResult:
    return SenseResult(
        request_id="r1",
        label=label,
        score=0.9,
        segments=[
            Segment(text=text, polarity="positive" if pol == "p" else "negative", weight=weight)
            for text, pol, weight in segments
        ],
    )


def test_derive_plans_single_mode_for_plain_emotion() -> None:
    plans = derive_plans("今天真开心", _verdict(Emotion.POSITIVE, []))
    assert [p.mode for p in plans] == [ReplyMode.ENCOURAGE]


def test_derive_plans_console_first_when_negative_dominates() -> None:
    text = "项目上线了很开心，但爷爷刚走"
    verdict = _verdict(Emotion.MIXED, [("项目上线了很开心", "p", 0.4), ("但爷爷刚走", "n", 0.6)])
    plans = derive_plans(text, verdict)
    assert [p.mode for p in plans] == [ReplyMode.CONSOLE, ReplyMode.ENCOURAGE]
    assert [p.order for p in plans] == [0, 1]
    assert plans[0].targets == ["但爷爷刚走"]


def test_derive_plans_positive_dominates_still_console_first() -> None:
    """排序是固定的「先接住再给力气」，权重只影响要不要拆。"""
    text = "升职了但室友搬走了"
    verdict = _verdict(Emotion.MIXED, [("升职了", "p", 0.8), ("但室友搬走了", "n", 0.2)])
    plans = derive_plans(text, verdict)
    assert plans[0].mode is ReplyMode.CONSOLE


def test_derive_plans_falls_back_when_one_side_missing() -> None:
    text = "有点复杂"
    verdict = _verdict(Emotion.MIXED, [("有点复杂", "n", 0.5)])
    plans = derive_plans(text, verdict)
    assert len(plans) == 1
    assert plans[0].mode is ReplyMode.CONSOLE


def test_locate_drops_rewritten_segments() -> None:
    text = "项目上线了很开心，但爷爷刚走"
    segments = [
        Segment(text="很开心", polarity="positive", weight=0.5),
        Segment(text="爷爷去世了", polarity="negative", weight=0.5),  # 模型改写过了
    ]
    kept = locate(text, segments)
    assert [s.text for s in kept] == ["很开心"]


def test_compose_skips_blank_parts() -> None:
    assert compose(["  ", "第一段", "\n", "第二段"]) == "第一段\n\n第二段"


# ---------------------------------------------------------------- 多轮记忆
def test_history_roundtrip(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sense.db")
    append(conn, session_id="s1", role="user", text="我好累")
    append(conn, session_id="s1", role="assistant", text="先坐下", sense_label="negative")
    turns = load_turns(conn, "s1", turns=6, char_limit=24000, single_char_limit=8000)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].sense_label is Emotion.NEGATIVE

    sessions = list_sessions(conn, limit=10)
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["title"] == "我好累"
    conn.close()


def test_load_turns_keeps_newest_and_drops_oldest(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sense.db")
    append(conn, session_id="s", role="user", text="x" * 500)
    append(conn, session_id="s", role="assistant", text="y" * 500)
    turns = load_turns(conn, "s", turns=6, char_limit=600, single_char_limit=8000)
    # 只放得下一条：留下的是刚说的那句，不是最早那句
    assert len(turns) == 1
    assert turns[0].role == "assistant"
    conn.close()


# ---------------------------------------------------------------- 重试策略
class _FakeBus:
    def __init__(self) -> None:
        self.added: list[tuple[str, object]] = []
        self.published: list[tuple[str, object]] = []
        self.dead: list[object] = []
        self.acked: list[tuple[str, str, str]] = []
        self.released: list[tuple[str, str]] = []

    async def add(self, stream: str, payload: object, *, max_len: int) -> str:
        self.added.append((stream, payload))
        return "1-0"

    async def publish(self, channel: str, payload: object) -> int:
        self.published.append((channel, payload))
        return 1

    async def dead_letter(self, stream: str, payload: object, *, max_len: int) -> str:
        self.dead.append(payload)
        return "1-0"

    async def ack(self, stream: str, group: str, entry_id: str) -> int:
        self.acked.append((stream, group, entry_id))
        return 1

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        return True

    async def release_lock(self, key: str, token: str) -> bool:
        self.released.append((key, token))
        return True


def _settings(tmp_path: Path, *, max_attempts: int = 3):
    from chatbi.sense.config import (
        LLMSettings,
        PipelineSettings,
        RedisSettings,
        SenseSettings,
    )

    return SenseSettings(
        project_root=tmp_path,
        llm=LLMSettings(api_key="test-key"),
        pipeline=PipelineSettings(max_attempts=max_attempts),
        redis=RedisSettings(),
        runtime="agents",
    )


def test_fail_resends_business_payload_with_next_attempt(tmp_path: Path) -> None:
    bus = _FakeBus()
    settings = _settings(tmp_path)
    request = SenseRequest(request_id="r1", session_id="s1", text="在吗")

    asyncio.run(_fail(bus, settings, request, "sense", "model_call", "boom"))

    assert bus.dead == []
    stream, payload = bus.added[0]
    assert stream == settings.redis.sense_stream
    assert isinstance(payload, SenseRequest)
    assert payload.attempt == 1
    assert payload.request_id == "r1"


def test_fail_goes_to_dead_letter_at_limit(tmp_path: Path) -> None:
    bus = _FakeBus()
    settings = _settings(tmp_path, max_attempts=2)
    request = SenseRequest(request_id="r2", session_id="s1", text="在吗", attempt=1)

    asyncio.run(_fail(bus, settings, request, "sense", "timeout", "等太久了"))

    assert bus.added == []
    assert len(bus.dead) == 1
    assert bus.dead[0].code == "timeout"


def test_fail_alerts_frontend_every_time(tmp_path: Path) -> None:
    bus = _FakeBus()
    settings = _settings(tmp_path, max_attempts=1)
    request = SenseRequest(request_id="r3", session_id="s1")

    asyncio.run(_fail(bus, settings, request, "reply", "weird_code", "x"))

    channel, event = bus.published[0]
    assert channel == settings.redis.events_channel("r3")
    assert event.type == "error"
    assert event.code == "weird_code"  # 前端看到原样错误，死信里才归一成 internal
    assert json.loads(bus.dead[0].model_dump_json())["code"] == "internal"

def test_reply_worker_releases_session_lock(tmp_path: Path) -> None:
    """回复说完必须把会话锁还掉，否则用户 30 秒内再问一句就被 409 挡住。"""

    class Runtime:
        name = "fake"

        async def reply(self, job, emit):
            await emit(0, "在的")
            return ReplyResult(
                request_id=job.request_id,
                session_id=job.session_id,
                text="在的",
                blocks=[{"type": "text", "text": "在的"}],
                model="fake",
                elapsed_ms=1,
            )

    job = ReplyJob(
        request_id="r1",
        session_id="s1",
        text="我饿了",
        plans=[ReplyPlan(mode=ReplyMode.CONSOLE, targets=["我饿了"], order=0)],
        sense=SenseResult(request_id="r1", label=Emotion.NEGATIVE, score=0.8),
    )
    bus = _FakeBus()
    settings = _settings(tmp_path)

    asyncio.run(_handle_reply(bus, settings, Runtime(), "1-0", job.model_dump(mode="json")))

    assert ("lock:sess:s1", "r1") in bus.released
    assert [event.type for _, event in bus.published] == ["delta", "done"]

