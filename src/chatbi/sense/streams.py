"""Redis Streams + Pub/Sub 的薄封装。

为什么用 Streams 而不是 Pub/Sub 传任务：Pub/Sub 是「发出去就不管了」，消费者没起来
消息就丢；Streams 有消费组、有 ACK、有 pending 列表，进程崩了消息还在，能捞回来。
事件推给前端才用 Pub/Sub——那边丢一帧无所谓，前端会重连补快照。

兼容性：`XAUTOCLAIM` 是 Redis 6.2 才有的命令，实例上是 6.0.16（Ubuntu 22.04 的 apt
版本），直接调会 `unknown command`。所以这里先试 XAUTOCLAIM，报错就退回
`XPENDING` + `XCLAIM`（5.0 起就有），结果一样，只是多一次往返。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError


class StreamBus:
    """一个连接干三件事：投递、消费、广播。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        # 探一次就够了：同一个服务端不会中途换版本
        self._has_autoclaim = True

    # ---------------------------------------------------------------- 投递
    async def add(self, stream: str, payload: Any, *, max_len: int) -> str:
        """追加一条。``max_len`` 用近似裁剪（~）——精确裁剪要额外扫，不值当。"""
        body = payload if isinstance(payload, str) else _dump(payload)
        return await self._redis.xadd(stream, {"data": body}, maxlen=max_len, approximate=True)

    # ---------------------------------------------------------------- 消费
    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 1,
        block_ms: int = 5000,
    ) -> list[tuple[str, dict[str, Any]]]:
        """读一批未投递消息。返回 ``[(entry_id, payload), ...]``。

        先 ``XGROUP CREATE ... MKSTREAM`` 兜底：第一次跑的时候流还不存在，
        没有 MKSTREAM 会 ``NOGROUP`` 报错，新机器上第一次启动就挂。

        ``BLOCK`` 读超时是**正常现象**（就是没消息），必须吞掉：把它当异常抛出去，
        循环会整个退出，进程看着还在、其实已经不消费了。
        """
        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError:
            pass  # BUSYGROUP：组已存在

        try:
            response = await self._redis.xreadgroup(
                group,
                consumer,
                {stream: ">"},
                count=count,
                block=block_ms,
            )
        except (RedisTimeoutError, RedisConnectionError):
            return []
        return _flatten(response)

    async def ack(self, stream: str, group: str, entry_id: str) -> None:
        await self._redis.xack(stream, group, entry_id)

    async def claim_stale(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int, count: int = 10
    ) -> list[tuple[str, dict[str, Any]]]:
        """把别人卡住超过 ``min_idle_ms`` 的消息认领过来。

        这是「进程被 kill 之后消息不丢」的那条命脉。6.0 上没有 XAUTOCLAIM，
        走 XPENDING → XCLAIM 的等价路径。
        """
        if self._has_autoclaim:
            try:
                response = await self._redis.xautoclaim(
                    stream,
                    group,
                    consumer,
                    min_idle_time=min_idle_ms,
                    start_id="0-0",
                    count=count,
                )
                entries = response[1] if len(response) > 1 else []
                return [(entry_id, _load(fields)) for entry_id, fields in entries]
            except ResponseError as exc:
                if "unknown command" not in str(exc).lower():
                    raise
                self._has_autoclaim = False  # 服务端版本低，之后都走退路

        # 注意：不要把 idle 传给 XPENDING。redis-py 把 `IDLE` 拼在 count 后面，
        # 而 Redis 要求 `XPENDING key group [IDLE ms] start end count`，6.0 会直接
        # 回 `syntax error`。这里取全量 pending（自带 idle 字段）再自己过滤。
        pending = await self._redis.xpending_range(
            stream, group, min="-", max="+", count=count * 10
        )
        ids = [item["message_id"] for item in pending if int(item.get("idle", 0)) >= min_idle_ms][
            :count
        ]
        if not ids:
            return []
        claimed = await self._redis.xclaim(
            stream,
            group,
            consumer,
            min_idle_time=min_idle_ms,
            message_ids=ids,
        )
        return [(entry_id, _load(fields)) for entry_id, fields in claimed]

    async def pending_count(self, stream: str, group: str) -> int:
        summary = await self._redis.xpending(stream, group)
        return int(summary.get("pending", 0)) if isinstance(summary, dict) else 0

    async def dead_letter(self, stream: str, payload: Any, *, max_len: int) -> str:
        """死信。不自动重投：重投交给人，或者下一次上线时手动搬回去。"""
        return await self.add(stream, payload, max_len=max_len)

    # ---------------------------------------------------------------- 广播
    async def publish(self, channel: str, payload: Any) -> int:
        body = payload if isinstance(payload, str) else _dump(payload)
        return int(await self._redis.publish(channel, body))

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """订阅事件频道。坏消息直接跳过，别让一条脏数据掐断 SSE。"""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    yield {"type": "tick"}  # 让上层有机会发心跳
                    continue
                try:
                    yield json.loads(message["data"])
                except (KeyError, ValueError):
                    continue
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    # ---------------------------------------------------------------- 幂等
    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        """``SET NX EX``：第一次返回 True，重复返回 False。

        ``EX`` 只收整数/tdelta，传 float 会 ``DataError: ex must be datetime.timedelta or int``。
        """
        return bool(await self._redis.set(key, "1", nx=True, ex=max(1, int(ttl_seconds))))

    async def session_lock(self, key: str, *, ttl_seconds: int, token: str = "1") -> bool:
        """同会话串行锁。``token`` 写进值里，释放时比对，别人的锁不会被误删。"""
        return bool(
            await self._redis.set(key, token, nx=True, ex=max(1, int(ttl_seconds)))
        )

    # 比对后再删。GET+DEL 两次往返之间有竞态，这里用 Lua 一次做完。
    _RELEASE = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """

    async def release_lock(self, key: str, token: str) -> bool:
        """只释放自己持有的锁：锁被后来的请求接管时，老请求不能把它删了。"""
        try:
            return bool(await self._redis.eval(self._RELEASE, 1, key, token))
        except Exception:  # noqa: BLE001 - 释放失败就等 TTL，不能因此中断回复
            return False

    async def release(self, key: str) -> None:
        await self._redis.delete(key)

    async def healthz(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001 - 探活失败就是 False
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def _dump(payload: Any) -> str:
    if hasattr(payload, "model_dump_json"):
        return str(payload.model_dump_json())
    return json.dumps(payload, ensure_ascii=False, default=str)


def _load(fields: dict[str, Any]) -> dict[str, Any]:
    raw = fields.get("data")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        loaded = json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw}
    return loaded if isinstance(loaded, dict) else {"raw": raw}


def _flatten(response: Any) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for _stream, items in response or []:
        for entry_id, fields in items:
            entries.append((entry_id, _load(fields)))
    return entries


__all__ = ["StreamBus"]
