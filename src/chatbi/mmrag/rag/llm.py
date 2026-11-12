"""调模型。

走 OpenAI 兼容接口，所以 DeepSeek / 本地 vLLM / Ollama 都能用，只换 ``OPENAI_BASE_URL``。
视觉请求单独用 ``vision_model``：能读图的模型通常更贵、更慢，只在带图时才用。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from chatbi.mmrag.config import LLMSettings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """模型没配好 / 调用失败，两种情况都要让前端看到原因而不是空白。"""


def _client(settings: LLMSettings) -> AsyncOpenAI:
    if not settings.api_key:
        raise LLMError("OPENAI_API_KEY 未配置，问答不可用")
    if not settings.model:
        raise LLMError("OPENAI_MODEL 未配置，问答不可用")
    return AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.timeout_seconds,
    )


def _pick_model(settings: LLMSettings, *, has_image: bool) -> str:
    return settings.vision_model if has_image and settings.supports_vision else settings.model


async def stream(messages: list[dict[str, Any]], settings: LLMSettings) -> AsyncIterator[str]:
    """逐段吐文本增量，调用方负责拼帧。"""
    has_image = any(isinstance(m.get("content"), list) for m in messages)
    model = _pick_model(settings, has_image=has_image)
    client = _client(settings)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            temperature=0.2,
        )
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - 上游各种超时/限流都归一类
        log.exception("llm call failed model=%s", model)
        raise LLMError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc
    finally:
        await client.close()


async def complete(messages: list[dict[str, Any]], settings: LLMSettings) -> str:
    """非流式，给"图先 caption"这类内部调用用。"""
    has_image = any(isinstance(m.get("content"), list) for m in messages)
    model = _pick_model(settings, has_image=has_image)
    client = _client(settings)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=False,
            temperature=0.2,
        )
        return response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc
    finally:
        await client.close()
