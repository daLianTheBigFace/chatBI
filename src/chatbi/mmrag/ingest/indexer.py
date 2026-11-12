"""向量落盘：``vectors.npy`` 一行一个块，行号就是 ``chunks.vec_id``。

为什么不用向量库：单机、几万块以内，numpy 矩阵 + 点积足够快（1024 维 × 5k 块 = 20MB），
少一个要运维的进程。真到十万块以上再换 FAISS，接口不用变。

一致性规则：
- 只有 kind=text/table 的块进矩阵，图片块 ``vec_id`` 永远 NULL；
- 追加时新行从 ``matrix.shape[0]`` 开始，绝不覆盖；
- 重解析一份文档前要先 ``drop_document_vectors`` 腾行，否则旧行会变成孤儿。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.db import clear_vectors, list_chunks, set_chunk_vectors
from chatbi.mmrag.ingest.embedder import Embedder
from chatbi.mmrag.models import ChunkKind

log = logging.getLogger(__name__)

VECTORIZABLE = {ChunkKind.TEXT, ChunkKind.TABLE}


def load_matrix(path: Path, dim: int) -> np.ndarray:
    """文件不存在就是空矩阵；维度不符直接报错，不要静默截断。"""
    if not path.exists():
        return np.zeros((0, dim), dtype=np.float32)
    matrix = np.load(path)
    if matrix.ndim != 2 or matrix.shape[1] != dim:
        raise ValueError(f"vectors.npy shape {matrix.shape} 与 dim={dim} 不符，先 rebuild")
    return matrix.astype(np.float32, copy=False)


def save_matrix(path: Path, matrix: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, matrix)


def index_document(
    conn, settings: MMRagSettings, doc_id: str, *, embedder: Embedder | None = None
) -> int:
    """把这份文档里还没向量的块编码并追加进矩阵，返回新增行数。"""
    pending = [
        chunk
        for chunk in list_chunks(conn, doc_id)
        if chunk.kind in VECTORIZABLE and chunk.vec_id is None
    ]
    if not pending:
        return 0

    worker = embedder or Embedder(settings.embed)
    matrix = load_matrix(settings.paths.vectors_file, worker.dim)
    vectors = worker.encode([chunk.content for chunk in pending])
    start = matrix.shape[0]
    save_matrix(settings.paths.vectors_file, np.vstack([matrix, vectors]))
    set_chunk_vectors(conn, [(start + offset, chunk.id) for offset, chunk in enumerate(pending)])
    log.info(
        "indexed doc=%s rows=%d..%d backend=%s",
        doc_id,
        start,
        start + len(pending) - 1,
        worker.backend,
    )
    return len(pending)


def rebuild_vectors(conn, settings: MMRagSettings) -> int:
    """全量重建：所有块重新编码，vec_id 从 0 重排。换模型 / 矩阵损坏时用。"""
    clear_vectors(conn)
    settings.paths.vectors_file.unlink(missing_ok=True)
    doc_ids = [
        str(row["doc_id"])
        for row in conn.execute("SELECT DISTINCT doc_id FROM chunks ORDER BY doc_id")
    ]
    embedder = Embedder(settings.embed)
    total = 0
    for doc_id in doc_ids:
        total += index_document(conn, settings, doc_id, embedder=embedder)
    log.info("rebuilt vectors: docs=%d chunks=%d", len(doc_ids), total)
    return total
