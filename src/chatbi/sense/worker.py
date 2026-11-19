"""两段流水线的消费循环。

进程1（sense）：读 stream:sense → 判定 → 广播 judged + 投 stream:reply
进程2（reply）：读 stream:reply → 生成 → 广播 delta/done → 落库

三条纪律（设计文档 4.2 的落地）：
1. 幂等键先抢，抢不到直接 ACK——同一条消息被两个消费者拿到时只有第一个算数。
2. 处理成功才 ACK；不 ACK 的消息 60 秒后由 XAUTOCLAIM 交给别人。
3. 重试用尽进 stream:dead，不自动重投——死信是给人看的。

重投必须重发**业务消息本身**（attempt+1），不能重发 PipelineError：
消费端要 model_validate，塞个错误对象进去明天就会在别处炸。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

from chatbi.sense.config import SenseSettings
from chatbi.sense.contracts import (
    ChatTurn,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    JudgedEvent,
    PipelineError,
    ReplyJob,
    ReplyResult,
    SenseRequest,
    SenseResult,
)
from chatbi.sense.history import append, connect, load_turns
from chatbi.sense.planning import derive_plans
from chatbi.sense.runtimes import build_runtime
from chatbi.sense.runtimes.base import RuntimeErrorBase
from chatbi.sense.streams import StreamBus

log = logging.getLogger(__name__)

STAGE_SENSE = "sense"
STAGE_REPLY = "reply"


async def run_sense_worker(settings: SenseSettings, *, stop: asyncio.Event | None = None) -> None:
    """进程1：只做判定，不说话。"""
    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    bus = StreamBus(redis)
    # 判定固定用 agents 那一侧：它只吐一段 JSON，图与 checkpoint 用不上
    runtime = build_runtime(settings, "agents")
    consumer = f"sense-{os.getpid()}"
    log.info("sense worker up (consumer=%s)", consumer)
    try:
        while not (stop and stop.is_set()):
            await _sweep(bus, settings, STAGE_SENSE, consumer, runtime)
            try:
                entries = await bus.consume(
                    settings.redis.sense_stream, settings.redis.sense_group, consumer
                )
            except Exception:  # noqa: BLE001 - 一次抖动不该让进程退出
                log.warning("poll failed stage=sense", exc_info=True)
                await asyncio.sleep(1.0)
                continue
            for entry_id, payload in entries:
                await _handle_sense(bus, settings, runtime, entry_id, payload)
    finally:
        await _close(runtime)
        await bus.close()


async def run_reply_worker(settings: SenseSettings, *, stop: asyncio.Event | None = None) -> None:
    """进程2：拿判定结果决定怎么说。"""
    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    bus = StreamBus(redis)
    runtime = build_runtime(settings, settings.runtime)
    enter = getattr(runtime, "__aenter__", None)
    if enter is not None:
        await enter()
    consumer = f"reply-{os.getpid()}"
    log.info("reply worker up (runtime=%s consumer=%s)", runtime.name, consumer)
    try:
        while not (stop and stop.is_set()):
            await _sweep(bus, settings, STAGE_REPLY, consumer, runtime)
            try:
                entries = await bus.consume(
                    settings.redis.reply_stream, settings.redis.reply_group, consumer
                )
            except Exception:  # noqa: BLE001 - 一次抖动不该让进程退出
                log.warning("poll failed stage=reply", exc_info=True)
                await asyncio.sleep(1.0)
                continue
            for entry_id, payload in entries:
                await _handle_reply(bus, settings, runtime, entry_id, payload)
    finally:
        exit_ = getattr(runtime, "__aexit__", None)
        if exit_ is not None:
            await exit_(None, None, None)
        await _close(runtime)
        await bus.close()


# ------------------------------------------------------------------ 进程1
async def _handle_sense(
    bus: StreamBus,
    settings: SenseSettings,
    runtime: Any,
    entry_id: str,
    payload: dict[str, Any],
) -> None:
    """判定一条。任何异常都必须落到「ACK + 告诉前端 + 可能重投」上。"""
    try:
        request = SenseRequest.model_validate(payload)
    except Exception:  # noqa: BLE001 - 脏消息只能进死信，不能堵分区
        log.exception("bad sense payload")
        await bus.dead_letter(
            settings.redis.dead_stream,
            PipelineError(
                request_id=str(payload.get("request_id", "?")),
                stage=STAGE_SENSE,
                code="internal",
                message="无法解析的 SenseRequest",
            ),
            max_len=settings.redis.max_len,
        )
        await bus.ack(settings.redis.sense_stream, settings.redis.sense_group, entry_id)
        return

    done_key = settings.redis.done_key(STAGE_SENSE, request.request_id)
    ttl = settings.pipeline.request_timeout_seconds * 4
    if not await bus.claim_once(done_key, ttl_seconds=ttl):
        await bus.ack(settings.redis.sense_stream, settings.redis.sense_group, entry_id)
        return

    started = time.monotonic()
    try:
        history = _load_history(settings, request)
        verdict = await runtime.judge(request, history)
    except RuntimeErrorBase as exc:
        await _fail(bus, settings, request, STAGE_SENSE, exc.code, exc.message)
        await bus.ack(settings.redis.sense_stream, settings.redis.sense_group, entry_id)
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("sense crashed request=%s", request.request_id)
        await _fail(bus, settings, request, STAGE_SENSE, "internal", str(exc))
        await bus.ack(settings.redis.sense_stream, settings.redis.sense_group, entry_id)
        return

    elapsed = int((time.monotonic() - started) * 1000)
    result = SenseResult(
        request_id=request.request_id,
        label=verdict.label,
        score=verdict.score,
        evidence=verdict.evidence,
        segments=verdict.segments,
        model=getattr(runtime, "model_name", ""),
        elapsed_ms=elapsed,
    )

    # 先广播判定，用户立刻看到结论；再把活交给进程2
    await bus.publish(
        settings.redis.events_channel(request.request_id),
        JudgedEvent(
            request_id=request.request_id,
            session_id=request.session_id,
            label=result.label,
            score=result.score,
            evidence=result.evidence,
            segments=result.segments,
            elapsed_ms=elapsed,
        ),
    )

    job = ReplyJob(
        request_id=request.request_id,
        session_id=request.session_id,
        text=request.text,
        plans=derive_plans(request.text, result),
        sense=result,
        history=history,
        deadline_ms=_deadline(settings),
        attempt=request.attempt,
    )
    await bus.add(settings.redis.reply_stream, job, max_len=settings.redis.max_len)
    await bus.ack(settings.redis.sense_stream, settings.redis.sense_group, entry_id)


# ------------------------------------------------------------------ 进程2
async def _handle_reply(
    bus: StreamBus,
    settings: SenseSettings,
    runtime: Any,
    entry_id: str,
    payload: dict[str, Any],
) -> None:
    try:
        job = ReplyJob.model_validate(payload)
    except Exception:  # noqa: BLE001
        log.exception("bad reply payload")
        await bus.dead_letter(
            settings.redis.dead_stream,
            PipelineError(
                request_id=str(payload.get("request_id", "?")),
                stage=STAGE_REPLY,
                code="internal",
                message="无法解析的 ReplyJob",
            ),
            max_len=settings.redis.max_len,
        )
        await bus.ack(settings.redis.reply_stream, settings.redis.reply_group, entry_id)
        return

    done_key = settings.redis.done_key(STAGE_REPLY, job.request_id)
    ttl = settings.pipeline.request_timeout_seconds * 4
    if not await bus.claim_once(done_key, ttl_seconds=ttl):
        await bus.ack(settings.redis.reply_stream, settings.redis.reply_group, entry_id)
        return

    channel = settings.redis.events_channel(job.request_id)
    counter = _SeqCounter()

    async def emit(plan: int, piece: str) -> None:
        await bus.publish(
            channel,
            DeltaEvent(request_id=job.request_id, plan=plan, seq=counter.next(), text=piece),
        )

    try:
        result = await runtime.reply(job, emit)
    except RuntimeErrorBase as exc:
        await _fail(bus, settings, job, STAGE_REPLY, exc.code, exc.message)
        await bus.release_lock(settings.redis.lock_key(job.session_id), job.request_id)
        await bus.ack(settings.redis.reply_stream, settings.redis.reply_group, entry_id)
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("reply crashed request=%s", job.request_id)
        await _fail(bus, settings, job, STAGE_REPLY, "internal", str(exc))
        await bus.release_lock(settings.redis.lock_key(job.session_id), job.request_id)
        await bus.ack(settings.redis.reply_stream, settings.redis.reply_group, entry_id)
        return

    _persist(settings, job, result)
    await bus.publish(
        channel,
        DoneEvent(
            request_id=job.request_id,
            session_id=job.session_id,
            text=result.text,
            blocks=result.blocks,
            elapsed_ms=result.elapsed_ms,
        ),
    )
    # 回复发出去就把会话锁还掉：这一轮说完了，用户可以接着问下一句。
    # 只删自己持有的（值 = request_id），锁被别的请求接管时不会误删。
    await bus.release_lock(settings.redis.lock_key(job.session_id), job.request_id)
    await bus.ack(settings.redis.reply_stream, settings.redis.reply_group, entry_id)


# ------------------------------------------------------------------ 公共
async def _fail(
    bus: StreamBus,
    settings: SenseSettings,
    message: SenseRequest | ReplyJob,
    stage: str,
    code: str,
    detail: str,
) -> None:
    """失败：要么重投，要么进死信并告诉前端。

    只在**放弃时**才发 error 帧。之前每次失败都发，于是超时触发重试时用户先看到
    完整回答（第一次尝试已经把 delta 推完了），过几秒又收到一条「回应生成超时」，
    重试成功的那份回答反而被这条错误盖住。重试是内部动作，不该让用户看见半截。
    """
    request_id = message.request_id
    attempt = message.attempt
    error = PipelineError(
        request_id=request_id,
        stage=stage,
        code=_code_of(code),
        message=detail,
        attempt=attempt,
    )
    if attempt + 1 >= settings.pipeline.max_attempts:
        await bus.publish(
            settings.redis.events_channel(request_id),
            ErrorEvent(request_id=request_id, stage=stage, code=code, message=detail),
        )
        await bus.dead_letter(settings.redis.dead_stream, error, max_len=settings.redis.max_len)
        log.warning("dead letter request=%s stage=%s attempt=%s", request_id, stage, attempt)
        return

    retry = message.model_copy(update={"attempt": attempt + 1})
    if stage == STAGE_SENSE:
        await bus.add(settings.redis.sense_stream, retry, max_len=settings.redis.max_len)
    else:
        await bus.add(settings.redis.reply_stream, retry, max_len=settings.redis.max_len)
    log.info("retry request=%s stage=%s attempt=%s", request_id, stage, attempt + 1)


def _code_of(code: str) -> str:
    return code if code in {"timeout", "model_call", "model_output", "internal"} else "internal"


async def _sweep(
    bus: StreamBus,
    settings: SenseSettings,
    stage: str,
    consumer: str,
    runtime: Any,
) -> None:
    """每轮先救一次别人的活：进程被 kill 时消息会烂在 pending 里。"""
    if stage == STAGE_SENSE:
        stream, group = settings.redis.sense_stream, settings.redis.sense_group
    else:
        stream, group = settings.redis.reply_stream, settings.redis.reply_group
    try:
        stale = await bus.claim_stale(
            stream, group, consumer, min_idle_ms=settings.redis.claim_idle_ms
        )
    except Exception:  # noqa: BLE001 - 救不回来也不能让主循环停
        log.warning("claim failed stage=%s", stage, exc_info=True)
        return
    for entry_id, payload in stale:
        log.info("claimed stale entry=%s stage=%s", entry_id, stage)
        if stage == STAGE_SENSE:
            await _handle_sense(bus, settings, runtime, entry_id, payload)
        else:
            await _handle_reply(bus, settings, runtime, entry_id, payload)


async def _close(runtime: Any) -> None:
    closer = getattr(runtime, "aclose", None)
    if closer is not None:
        await closer()


def _deadline(settings: SenseSettings) -> int:
    return int(time.time() * 1000) + settings.pipeline.request_timeout_seconds * 1000


def _load_history(settings: SenseSettings, request: SenseRequest) -> list[ChatTurn]:
    try:
        conn = connect(settings.db_path)
    except Exception:  # noqa: BLE001 - 历史读不到不该让判定失败
        log.warning("history unavailable", exc_info=True)
        return []
    try:
        return load_turns(
            conn,
            request.session_id,
            turns=settings.pipeline.history_turns,
            char_limit=settings.pipeline.history_char_limit,
            single_char_limit=settings.pipeline.single_message_char_limit,
        )
    finally:
        conn.close()


def _persist(settings: SenseSettings, job: ReplyJob, result: ReplyResult) -> None:
    try:
        conn = connect(settings.db_path)
    except Exception:  # noqa: BLE001
        log.warning("persist skipped: db unavailable", exc_info=True)
        return
    try:
        append(
            conn,
            session_id=job.session_id,
            role="assistant",
            text=result.text,
            sense_label=job.sense.label.value,
            blocks=json.dumps(result.blocks, ensure_ascii=False),
        )
    finally:
        conn.close()


class _SeqCounter:
    """同一条流的序号：前端按它排序，跳号说明丢帧。"""

    def __init__(self) -> None:
        self._value = 0

    def next(self) -> int:
        self._value += 1
        return self._value


async def run(stage: str, settings: SenseSettings, *, stop: asyncio.Event | None = None) -> None:
    """统一入口：``python -m chatbi.sense.worker sense|reply``。"""
    if stage not in (STAGE_SENSE, STAGE_REPLY):
        raise ValueError(f"未知阶段：{stage}")
    runner: Callable[..., Awaitable[None]] = (
        run_sense_worker if stage == STAGE_SENSE else run_reply_worker
    )
    await runner(settings, stop=stop)
