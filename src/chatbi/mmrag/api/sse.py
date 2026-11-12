"""状态广播。

worker 在远端实例上，推不动本机 SSE；所以状态由两个入口进本机：
1. 接口层自己改状态时直接 publish；
2. 本机常驻一个 ``doc.parse.result`` 消费者，收到 worker 回写后更新 rag.db 再 publish。

频道：``mmrag:status:{doc_id}`` 与 ``mmrag:status``（全局）。

为什么要两个频道：SSE 是按浏览器窗口连的，一个窗口要看所有文档 → 订阅全局；
详情页只看一份 → 订阅单文档频道，省得把别的文档事件也推过去。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import redis as redis_sync
import redis.asyncio as redis_async

from chatbi.mmrag.contracts import StatusEvent

CHANNEL_ALL = "mmrag:status"


def doc_channel(doc_id: str) -> str:
    return f"mmrag:status:{doc_id}"


class StatusBus:
    """redis pub/sub 的薄封装；连接懒建，断线由 redis-py 自己重连。

    只在 API 进程里用（asyncio 上下文）。worker / bridge 是同步代码，走 ``publish_now``。
    """

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._client: redis_async.Redis | None = None

    def _ensure(self) -> redis_async.Redis:
        if self._client is None:
            self._client = redis_async.from_url(
                self._url, decode_responses=True, health_check_interval=30
            )
        return self._client

    async def publish(self, event: StatusEvent) -> int:
        """同时发到全局频道与单文档频道；返回收到消息的订阅者数（0 表示没人听）。"""
        client = self._ensure()
        payload = event.model_dump_json()
        global_count = await client.publish(CHANNEL_ALL, payload)
        doc_count = await client.publish(doc_channel(event.doc_id), payload)
        return int(global_count) + int(doc_count)

    async def listen(self, doc_id: str | None = None) -> AsyncIterator[StatusEvent]:
        """订阅（单文档或全局）；坏消息跳过，别让一条脏数据掐断整条 SSE。"""
        client = self._ensure()
        pubsub = client.pubsub()
        await pubsub.subscribe(doc_channel(doc_id) if doc_id else CHANNEL_ALL)
        try:
            while True:
                raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if raw is None:
                    # 交给 SSE 层的 15 秒注释心跳保活，这里只让出控制权
                    await asyncio.sleep(0.1)
                    continue
                try:
                    yield StatusEvent.model_validate_json(raw["data"])
                except ValueError:
                    continue
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def publish_now(redis_url: str, event: StatusEvent) -> int:
    """同步侧（bridge / 脚本）发一条状态；每次都新建连接，量小无所谓。"""
    client = redis_sync.from_url(redis_url, decode_responses=True, socket_timeout=5)
    try:
        payload = event.model_dump_json()
        total = int(client.publish(CHANNEL_ALL, payload))
        total += int(client.publish(doc_channel(event.doc_id), payload))
        return total
    finally:
        client.close()
