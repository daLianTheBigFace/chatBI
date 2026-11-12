"""本机磁盘布局与上传来料落盘。

一个文档一个目录：

    data/mmrag/docs/{doc_id}/
        raw.pdf                 原始文件
        parsed/                 MinerU 产物（md、content_list.json、images/）
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from chatbi.mmrag.config import MMRagPaths

STATIC_PREFIX = "/static/docs"
CHUNK_SIZE = 1 << 20


class UploadRejected(Exception):
    """后缀不合规、超限或落盘失败。"""


def new_doc_id() -> str:
    return uuid.uuid4().hex[:12]


def save_upload(
    paths: MMRagPaths,
    *,
    doc_id: str,
    filename: str,
    stream: object,
    max_bytes: int,
    allowed_suffixes: tuple[str, ...],
) -> tuple[Path, int]:
    """把上传流写进 ``raw.pdf``，返回 (路径, 字节数)。

    先写临时文件再改名，避免半截文件被 worker 抢到。
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in allowed_suffixes:
        raise UploadRejected(f"unsupported file type: {suffix or '(none)'}")

    target = paths.raw_pdf(doc_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    written = 0
    try:
        with tmp.open("wb") as handle:
            while True:
                piece = stream.read(CHUNK_SIZE)  # type: ignore[attr-defined]
                if not piece:
                    break
                written += len(piece)
                if written > max_bytes:
                    raise UploadRejected("file too large")
                handle.write(piece)
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return target, written


def static_url(doc_id: str, relative: str) -> str:
    """answer_blocks 里图片只用 URL，前端自己去取。"""
    return f"{STATIC_PREFIX}/{doc_id}/{relative.lstrip('/')}"


def drop_document(paths: MMRagPaths, doc_id: str) -> None:
    shutil.rmtree(paths.doc_dir(doc_id), ignore_errors=True)
