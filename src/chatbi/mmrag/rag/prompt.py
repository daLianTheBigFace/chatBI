"""拼 prompt。

三条规则：
1. **只给块，不给整篇**——上下文越长模型越容易忽略中间部分，也越贵。
2. **每条都带页码**——回答里的引用是 ``[来源 3 · 第 2 页]``，让用户能回去核。
3. **图先给文字占位**——模型不支持视觉时，把同页文字摘要塞进 prompt，
   告诉它"这里有一张图，内容是……"，不要凭空描述图里有什么。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from chatbi.mmrag.config import LLMSettings
from chatbi.mmrag.rag.retriever import Hit

SYSTEM_PROMPT = """你是文档问答助手。只能依据下面提供的资料回答，不许编造。

规则：
- 每条结论后面标注来源编号与页码，格式：[来源 2 · 第 3 页]
- 资料里没有的，直接说"资料里没有提到"，不要推测
- 如果资料附图，可以在回答里提到"第 N 页有相关图示"
- 中文回答，简洁，不要复述问题"""


def _format_hits(hits: list[Hit]) -> str:
    if not hits:
        return "（没有检索到相关资料）"
    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        page = hit.chunk.page_idx + 1
        kind = hit.chunk.kind.value
        lines.append(f"[来源 {index} · 第 {page} 页 · {kind}] {hit.filename}")
        lines.append(hit.chunk.content.strip())
        for image in hit.images:
            caption = image.content.strip() or "无说明"
            lines.append(f"  （第 {image.page_idx + 1} 页有一张图，文字摘要：{caption}）")
        lines.append("")
    return "\n".join(lines)


def _image_part(path: Path) -> dict[str, Any] | None:
    """本地图转 data URL 给多模态模型；只发给模型，不发给前端。"""
    if not path.exists():
        return None
    suffix = path.suffix.lower().lstrip(".") or "png"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/{suffix};base64,{payload}"}}


def build_messages(
    settings: LLMSettings,
    question: str,
    hits: list[Hit],
    *,
    user_image: Path | None = None,
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """返回 OpenAI chat 格式的 messages。"""
    context = _format_hits(hits)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-4:])  # 只带最近两轮，长对话容易跑偏

    text = f"资料：\n{context}\n\n问题：{question}"
    can_send_image = settings.supports_vision and user_image is not None
    part = _image_part(user_image) if can_send_image and user_image else None

    if part is not None:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    part,
                ],
            }
        )
    else:
        messages.append({"role": "user", "content": text})
    return messages


def needs_caption(settings: LLMSettings, user_image: Path | None) -> bool:
    """模型看不了图，就得先给它一段文字描述用户图，否则那张图等于没发。"""
    return user_image is not None and not settings.supports_vision
