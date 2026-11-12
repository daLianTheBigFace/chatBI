"""把模型输出组装成 answer_blocks。

回答的形态是「一段文字 + 若干引用 + 若干张图」，所以流式过程中：
- 文本边流边发 ``text_delta``，前端先渲染出来，不用等整段；
- 引用在**开始时**就定好（检索阶段就知道命中了哪几页），不依赖模型复述页码；
- 图在结束时一次性发 ``image`` 块，避免流一半插一张图把排版打乱。
"""

from __future__ import annotations

import re

from chatbi.mmrag.models import Answer, AnswerBlock, Citation, ImageBlock, TextBlock
from chatbi.mmrag.rag.retriever import Hit
from chatbi.mmrag.storage import static_url

CITATION_PATTERN = re.compile(r"\[来源\s*(\d+)\s*[·・]?\s*第?\s*(\d+)?\s*页?\]")


def citations_from(hits: list[Hit]) -> list[Citation]:
    """按命中顺序给引用，去重（同文档同页只留一条）。"""
    seen: set[tuple[str, int]] = set()
    result: list[Citation] = []
    for hit in hits:
        key = (hit.chunk.doc_id, hit.chunk.page_idx)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            Citation(
                doc_id=hit.chunk.doc_id,
                filename=hit.filename,
                page_idx=hit.chunk.page_idx,
            )
        )
    return result


def image_blocks(hits: list[Hit], *, limit: int) -> list[ImageBlock]:
    """同页图片转成可渲染的块；路径一律走静态 URL，不塞 base64。"""
    blocks: list[ImageBlock] = []
    seen: set[tuple[str, int]] = set()
    for hit in hits:
        for image in hit.images:
            key = (image.doc_id, image.page_idx)
            if key in seen or not image.image_path:
                continue
            seen.add(key)
            blocks.append(
                ImageBlock(
                    url=static_url(image.doc_id, image.image_path),
                    caption=(image.content or "").strip()[:120],
                    page_idx=image.page_idx,
                )
            )
            if len(blocks) >= limit:
                return blocks
    return blocks


def to_answer(text: str, hits: list[Hit], *, max_images: int) -> Answer:
    """完整回答（非流式场景 / 落库用）。"""
    blocks: list[AnswerBlock] = []
    body = text.strip()
    if body:
        blocks.append(TextBlock(text=body, citations=citations_from(hits)))
    blocks.extend(image_blocks(hits, limit=max_images))
    return Answer(blocks=blocks)


def cited_pages(text: str, hits: list[Hit]) -> list[Citation]:
    """从模型正文里抠 ``[来源 3 · 第 2 页]``，映射回真实文档。

    模型可能瞎写编号，所以越界的一律丢掉，只保留能对上命中列表的。
    """
    picked: list[Citation] = []
    for match in CITATION_PATTERN.finditer(text):
        index = int(match.group(1)) - 1
        if not 0 <= index < len(hits):
            continue
        hit = hits[index]
        picked.append(
            Citation(
                doc_id=hit.chunk.doc_id,
                filename=hit.filename,
                page_idx=hit.chunk.page_idx,
            )
        )
    return picked
