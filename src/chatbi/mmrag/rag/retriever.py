"""检索：问题向量 → 矩阵点积 → top-k 块 → 补同页图片。

矩阵是 L2 归一化过的，所以点积就是余弦相似度，不需要再算范数。

「补同页图片」是这一版多模态的关键：图片块不进向量（图片没有文本可编码），
但如果某页的正文被命中，那页的图多半和问题相关，一起带给模型。
这是用文本召回图片，代价是同一页的图会被一起带出来——所以有 ``max_images`` 上限。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.db import chunks_in_pages, get_document, images_in_pages
from chatbi.mmrag.ingest.embedder import Embedder
from chatbi.mmrag.ingest.indexer import load_matrix
from chatbi.mmrag.models import Chunk, ChunkKind

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Hit:
    """一条检索结果。``images`` 是同页的图片块，用于图文混排回答。"""

    chunk: Chunk
    score: float
    filename: str = ""
    images: tuple[Chunk, ...] = field(default_factory=tuple)


def _chunk_by_vec_id(conn: sqlite3.Connection, vec_ids: list[int]) -> dict[int, Chunk]:
    if not vec_ids:
        return {}
    marks = ",".join("?" * len(vec_ids))
    rows = conn.execute(f"SELECT * FROM chunks WHERE vec_id IN ({marks})", vec_ids).fetchall()
    from chatbi.mmrag.db import _row_to_chunk  # 局部导入：只在这里需要

    return {int(row["vec_id"]): _row_to_chunk(row) for row in rows}


def search(
    conn: sqlite3.Connection,
    settings: MMRagSettings,
    question: str,
    *,
    embedder: Embedder | None = None,
    top_k: int | None = None,
) -> list[Hit]:
    """返回按相似度降序的命中块；库里没数据时返回空列表，不抛异常。"""
    query = question.strip()
    if not query:
        return []

    worker = embedder or Embedder(settings.embed)
    matrix = load_matrix(settings.paths.vectors_file, worker.dim)
    if matrix.shape[0] == 0:
        log.info("vectors empty, nothing to retrieve")
        return []

    limit = top_k or settings.llm.top_k
    vector = worker.encode([query])[0]
    scores = matrix @ vector
    # 只取正相关的前 limit 个；不相关的负分块带进去只会污染 prompt。
    # 候选放宽到 5 倍，因为后面要去重——同一份文档传两遍时，
    # 完全相同的块会挤满 top-k，把真正的表格块挤出去。
    order = np.argsort(-scores)[: max(limit * 5, limit)]
    picked = [int(index) for index in order if float(scores[index]) > 0][: max(limit * 2, limit)]
    chunks = _chunk_by_vec_id(conn, picked)

    hits: list[Hit] = []
    seen: set[str] = set()
    for vec_id in picked:
        chunk = chunks.get(vec_id)
        if chunk is None:
            continue  # 矩阵里有行但库里的块没了，说明索引过期，跳过即可
        fingerprint = hashlib.blake2b(
            f"{chunk.content}\n{chunk.image_path or ''}".encode(), digest_size=8
        ).hexdigest()
        if fingerprint in seen:
            continue  # 内容一模一样的块只留分最高的那条
        seen.add(fingerprint)
        if len(hits) >= limit:
            break
        document = get_document(conn, chunk.doc_id)
        images = tuple(images_in_pages(conn, chunk.doc_id, [chunk.page_idx]))
        hits.append(
            Hit(
                chunk=chunk,
                score=float(scores[vec_id]),
                filename=document.filename if document else chunk.doc_id,
                images=images,
            )
        )
    # 命中页上的表格块补进来：markdown 竖线对 embedding 不友好，
    # 「表格里有什么」这类问题常常排不进 top-k，但正文命中就说明这页相关。
    page_keys = {(hit.chunk.doc_id, hit.chunk.page_idx) for hit in hits}
    have = {hit.chunk.id for hit in hits}
    for doc_id, page_idx in sorted(page_keys):
        document = get_document(conn, doc_id)
        for table in chunks_in_pages(conn, doc_id, [page_idx], ChunkKind.TABLE):
            if table.id in have:
                continue
            have.add(table.id)
            hits.append(
                Hit(
                    chunk=table,
                    score=0.0,
                    filename=document.filename if document else doc_id,
                    images=tuple(images_in_pages(conn, doc_id, [page_idx])),
                )
            )

    log.info(
        "retrieved %d hits for %r (top score=%.3f)",
        len(hits),
        query[:30],
        hits[0].score if hits else 0.0,
    )
    return hits


def collect_images(hits: list[Hit], *, limit: int) -> list[tuple[str, Chunk]]:
    """把命中块的同页图片摊平、按文档+页去重，最多 ``limit`` 张。"""
    seen: set[tuple[str, int]] = set()
    picked: list[tuple[str, Chunk]] = []
    for hit in hits:
        for image in hit.images:
            key = (image.doc_id, image.page_idx)
            if key in seen:
                continue
            seen.add(key)
            picked.append((hit.filename, image))
            if len(picked) >= limit:
                return picked
    return picked
