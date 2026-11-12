"""MinerU 产物 → chunks。

``content_list.json`` 每条至少要有 ``type`` / ``page_idx``，文本与表格带 ``text``，
图片带 ``img_path``，部分版本带 ``bbox``。缺哪个字段就跳过哪种处理，不猜。

切块规则：
- text  ：500–800 字，按句末标点收口；保留 page_idx 与 bbox
- table ：整块不切（切了会丢表头语义）
- image ：块内容填「同页前后文本摘要」当召回钩子，图片本体留在磁盘，不向量化
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from chatbi.mmrag.models import Chunk, ChunkKind

MAX_CHARS = 800
MIN_CHARS = 500
# 一天里最短也长得像句话：低于这个长度的孤立片段（标题、残行）并进相邻正文
MIN_FRAGMENT_CHARS = 40
SENTENCE_END = re.compile(r"(?<=[。！？.!?；;])")


PAGE_MARKER = re.compile(r"<!--\s*page\s+(\d+)\s+of\s+(\d+)\s*-->", re.IGNORECASE)
IMAGE_REF = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def find_content_list(parsed_dir: Path) -> Path | None:
    """MinerU 会按 ``{stem}/auto/*_content_list.json`` 落盘，这里递归找第一个。"""
    for candidate in sorted(parsed_dir.rglob("*content_list.json")):
        return candidate
    return None


def find_markdown(parsed_dir: Path) -> Path | None:
    for candidate in sorted(parsed_dir.rglob("*.md")):
        return candidate
    return None


def _items_from_markdown(md_path: Path) -> list[dict]:
    """4.x 的 ``parse -o x.md`` 只给一份 markdown，没有 content_list.json。

    退路：按 ``<!-- page N of M -->`` 切页，再在页内把表格/图片/正文分开。
    bbox 拿不到就留空——丢定位信息，但切块与图文对齐还能做。
    """
    text = md_path.read_text(encoding="utf-8")
    items: list[dict] = []

    marks = list(PAGE_MARKER.finditer(text))
    segments: list[tuple[int, str]] = []
    if not marks:
        segments.append((0, text))
    else:
        if text[: marks[0].start()].strip():
            segments.append((0, text[: marks[0].start()]))
        for index, mark in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            segments.append((int(mark.group(1)) - 1, text[mark.end() : end]))

    for page_idx, segment in segments:
        items.extend(_split_segment(page_idx, segment))
    return items


def _split_segment(page_idx: int, segment: str) -> list[dict]:
    """页内按「表格块 / 图片行 / 其余正文」切开，保持出现顺序。"""
    items: list[dict] = []
    buffer: list[str] = []
    table: list[str] = []

    def flush_text(force: bool = False) -> None:
        """短片段不单独成块：标题、孤立一行会跟后面的正文合并，避免碎块污染检索。"""
        body = "\n".join(buffer).strip()
        if body and (force or len(body) >= MIN_FRAGMENT_CHARS):
            items.append({"type": "text", "text": body, "page_idx": page_idx})
            buffer.clear()

    def flush_table() -> None:
        body = "\n".join(table).strip()
        if body:
            items.append({"type": "table", "table_body": body, "page_idx": page_idx})
        table.clear()

    for line in segment.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            flush_text(force=True)
            table.append(stripped)
            continue
        if table:
            flush_table()
        if not stripped:
            flush_text()
            continue
        for ref in IMAGE_REF.finditer(stripped):
            items.append({"type": "image", "img_path": ref.group(1), "page_idx": page_idx})
        plain = IMAGE_REF.sub("", stripped).lstrip("#").strip()
        if plain:
            buffer.append(plain)
        elif IMAGE_REF.search(stripped):
            flush_text(force=True)
    flush_text(force=True)
    flush_table()
    return items


def load_items(parsed_dir: Path) -> list[dict]:
    """优先读 content_list.json（有 bbox），没有就退到 markdown 切页。"""
    path = find_content_list(parsed_dir)
    if path is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("content_list.json must be a JSON array")
        return [item for item in data if isinstance(item, dict)]

    markdown = find_markdown(parsed_dir)
    if markdown is None:
        raise FileNotFoundError(f"no content_list.json or *.md under {parsed_dir}")
    return _items_from_markdown(markdown)


def split_text(text: str) -> list[str]:
    """按句末标点做窗口，尽量落在 500–800 字之间。"""
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    if len(cleaned) <= MAX_CHARS:
        return [cleaned]

    pieces: list[str] = []
    buffer = ""
    for sentence in filter(None, SENTENCE_END.split(cleaned)):
        if buffer and len(buffer) + len(sentence) > MAX_CHARS:
            pieces.append(buffer)
            buffer = ""
        buffer += sentence
        while len(buffer) > MAX_CHARS:
            pieces.append(buffer[:MAX_CHARS])
            buffer = buffer[MAX_CHARS:]
    if buffer:
        # 尾巴太短就并进上一块，避免出现几十字的碎片
        if pieces and len(buffer) < MIN_CHARS // 4:
            pieces[-1] += buffer
        else:
            pieces.append(buffer)
    return [piece for piece in pieces if piece.strip()]


def _bbox(item: dict) -> tuple[float, float, float, float] | None:
    raw = item.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        values = tuple(float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return values  # type: ignore[return-value]


def _page(item: dict) -> int:
    value = item.get("page_idx", 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _caption(item: dict) -> str:
    for key in ("img_caption", "image_caption", "caption"):
        raw = item.get(key)
        if isinstance(raw, list):
            joined = " ".join(str(part) for part in raw if part)
        elif isinstance(raw, str):
            joined = raw
        else:
            continue
        if joined.strip():
            return joined.strip()
    return ""


def _page_context(items: list[dict], page_idx: int, limit: int = 200) -> str:
    """同页文本摘要，给图片块当召回钩子用。"""
    chunks = [
        str(item.get("text", ""))
        for item in items
        if _page(item) == page_idx and item.get("type") in {"text", "table"}
    ]
    joined = " ".join(part for part in chunks if part.strip())
    return joined[:limit]


def build_chunks(
    doc_id: str,
    items: Iterable[dict],
    *,
    parsed_dir: Path,
    image_prefix: str = "parsed/",
) -> Iterator[Chunk]:
    """把 content_list 逐条转成 Chunk；id 稳定，重解析不会漂移。"""
    materialized = list(items)
    for index, item in enumerate(materialized):
        kind_raw = str(item.get("type", "")).strip().lower()
        page_idx = _page(item)
        bbox = _bbox(item)

        if kind_raw in {"text", "equation"}:
            for offset, piece in enumerate(split_text(str(item.get("text", "")))):
                yield Chunk(
                    id=f"{doc_id}:p{page_idx}:t{index}:{offset}",
                    doc_id=doc_id,
                    kind=ChunkKind.TEXT,
                    page_idx=page_idx,
                    bbox=bbox,
                    content=piece,
                )
            continue

        if kind_raw in {"table", "table_body"}:
            body = str(item.get("table_body") or item.get("text") or "").strip()
            if not body:
                continue
            yield Chunk(
                id=f"{doc_id}:p{page_idx}:b{index}",
                doc_id=doc_id,
                kind=ChunkKind.TABLE,
                page_idx=page_idx,
                bbox=bbox,
                content=body,
            )
            continue

        if kind_raw == "image":
            relative = str(item.get("img_path") or "").strip()
            if not relative:
                continue
            caption = _caption(item)
            context = _page_context(materialized, page_idx)
            yield Chunk(
                id=f"{doc_id}:p{page_idx}:i{index}",
                doc_id=doc_id,
                kind=ChunkKind.IMAGE,
                page_idx=page_idx,
                bbox=bbox,
                content=(caption or context or f"第 {page_idx + 1} 页图片"),
                image_path=f"{image_prefix}{relative.lstrip('/')}",
            )
