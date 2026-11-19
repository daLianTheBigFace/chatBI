"""把判定结果翻译成「要回哪几段」。纯函数，两个 runtime 共用，好测。

规则（见 docs/sense-pipeline-design.md 4.6）：
- 排序固定 A：先接住负面，再给正面力气。反过来像先敷衍再补救；
  按权重浮动也不行——同一句话问两次得到两种顺序，用户会觉得忽冷忽热。
- mixed 拆两段；只定位到一段就只回那一段；两段都定位不到，按 negative 处理
  （宁可接住，不要错夸）。
"""

from __future__ import annotations

from chatbi.sense.contracts import Emotion, ReplyMode, ReplyPlan, Segment, SenseVerdict


def locate(text: str, segments: list[Segment]) -> list[Segment]:
    """只留下能在原文里真的找到的片段——模型偶尔会改写，改写过的没法引用。"""
    found: list[Segment] = []
    for segment in segments:
        needle = segment.text.strip()
        if needle and needle in text:
            found.append(segment)
    return found


def derive_plans(text: str, verdict: SenseVerdict) -> list[ReplyPlan]:
    """按判定结果排 plan，列表顺序就是输出顺序。"""
    if verdict.label is not Emotion.MIXED:
        return _plans_for_single(verdict.label)

    segments = locate(text, verdict.segments)
    negative = [s for s in segments if s.polarity == "negative"]
    positive = [s for s in segments if s.polarity == "positive"]

    if not negative and not positive:
        # 判了 mixed 却一段都定位不到：当负面处理，先把人接住
        return [ReplyPlan(mode=ReplyMode.CONSOLE, targets=[], order=0)]
    if not negative:
        return [ReplyPlan(mode=ReplyMode.ENCOURAGE, targets=[s.text for s in positive], order=0)]
    if not positive:
        return [ReplyPlan(mode=ReplyMode.CONSOLE, targets=[s.text for s in negative], order=0)]

    return [
        ReplyPlan(mode=ReplyMode.CONSOLE, targets=[s.text for s in negative], order=0),
        ReplyPlan(mode=ReplyMode.ENCOURAGE, targets=[s.text for s in positive], order=1),
    ]


def _plans_for_single(label: Emotion) -> list[ReplyPlan]:
    mode = {
        Emotion.POSITIVE: ReplyMode.ENCOURAGE,
        Emotion.NEGATIVE: ReplyMode.CONSOLE,
        Emotion.NEUTRAL: ReplyMode.NEUTRAL,
    }[label]
    return [ReplyPlan(mode=mode, targets=[], order=0)]


def compose(parts: list[str]) -> str:
    """把各段的文本拼成一条回复，空段丢掉。"""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())
