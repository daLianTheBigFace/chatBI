"""情感识别流水线的数据契约。

两个 runtime（OpenAI Agents SDK / LangGraph）共用这里的模型，谁也不许各写一套：
字段一改两边同时受影响，这正是要的。

约定：
- 模型输出用 ``SenseVerdict``（只含模型能给的东西），外面再包 ``SenseResult``
  （补 request_id / model / 耗时）。这样模型 schema 不会被工程字段污染。
- 全部 ``extra="forbid"``：多一个字段就是 bug，不是「先这样吧」。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Emotion(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class ReplyMode(StrEnum):
    ENCOURAGE = "encourage"
    CONSOLE = "console"
    NEUTRAL = "neutral"


class Segment(Contract):
    """一句话里的一段情绪。用原文子串而不是 offset：中文偏移常差一两个字。"""

    text: str
    polarity: Literal["positive", "negative"]
    weight: float = Field(ge=0, le=1)


class SenseVerdict(Contract):
    """模型直接产出的东西。判定阶段只认这个，其余字段由调度层补。"""

    label: Emotion
    score: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=3)
    segments: list[Segment] = Field(default_factory=list, max_length=2)


def parse_verdict(raw: str) -> SenseVerdict:
    """从模型输出里抠出 JSON 并校验。

    为什么要自己抠：DeepSeek 不支持 ``response_format=json_schema``
    （会回 400 "This response_format type is unavailable now"），
    所以两个 runtime 都让模型直接吐 JSON，解析与校验归我们。
    模型偶尔会包一层 ```json 或者加一句「好的，结果如下」，这里一并兜住。
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型输出里没有 JSON 对象")
    payload = json.loads(text[start : end + 1])
    if "score" not in payload:
        payload["score"] = 0.5  # 少给一个字段不至于整条失败
    return SenseVerdict.model_validate(payload)


class SenseRequest(Contract):
    request_id: str
    session_id: str
    text: str = ""
    image_ref: str | None = None
    attempt: int = 0  # 重试计数：到 max_attempts 就进死信，不再重投
    created_at: datetime = Field(default_factory=utcnow)


class SenseResult(Contract):
    request_id: str
    label: Emotion
    score: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=3)
    segments: list[Segment] = Field(default_factory=list, max_length=2)
    model: str = ""
    elapsed_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class ReplyPlan(Contract):
    mode: ReplyMode
    targets: list[str] = Field(default_factory=list, max_length=2)
    order: int


class ChatTurn(Contract):
    role: Literal["user", "assistant"]
    text: str
    sense_label: Emotion | None = None


class ReplyJob(Contract):
    request_id: str
    session_id: str
    text: str = ""  # 原始那句话，回应时要用
    plans: list[ReplyPlan] = Field(max_length=2)
    sense: SenseResult
    history: list[ChatTurn] = Field(default_factory=list)
    deadline_ms: int = 0
    attempt: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class ReplyResult(Contract):
    request_id: str
    session_id: str
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    elapsed_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class PipelineError(Contract):
    request_id: str
    stage: Literal["sense", "reply"]
    code: Literal["timeout", "model_call", "model_output", "internal"]
    message: str
    attempt: int = 0
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- SSE 事件
# 事件都带 request_id：SSE 端是按频道收的，但一帧里没有 id，前端就没法把
# 「上一次提问的迟到帧」和「这一条」分开。多一个字段换这个确定性，值。


class AcceptedEvent(Contract):
    type: Literal["accepted"] = "accepted"
    request_id: str
    session_id: str


class JudgedEvent(Contract):
    type: Literal["judged"] = "judged"
    request_id: str
    session_id: str = ""
    label: Emotion
    score: float
    evidence: list[str] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list, max_length=2)
    elapsed_ms: int = 0


class DeltaEvent(Contract):
    type: Literal["delta"] = "delta"
    request_id: str
    plan: int
    seq: int
    text: str


class DoneEvent(Contract):
    type: Literal["done"] = "done"
    request_id: str
    session_id: str = ""
    text: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int = 0


class ErrorEvent(Contract):
    type: Literal["error"] = "error"
    request_id: str
    stage: Literal["sense", "reply"] = "sense"
    code: str
    message: str


SenseEvent = Annotated[
    AcceptedEvent | JudgedEvent | DeltaEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]
