"""SQLite 存取层（rag.db）。

表只三张：documents / chunks / messages，够用即可，不上 ORM。
向量不进 SQLite：``vectors.npy`` 按 ``chunks.vec_id`` 行号对齐。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from chatbi.mmrag.models import (
    ChatMessage,
    Chunk,
    ChunkKind,
    DocStatus,
    Document,
    Role,
    utcnow,
)

DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL,
    pages       INTEGER NOT NULL DEFAULT 0,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    attempt     INTEGER NOT NULL DEFAULT 1,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status, created_at);

CREATE TABLE IF NOT EXISTS chunks (
    id          TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    page_idx    INTEGER NOT NULL,
    bbox        TEXT,
    content     TEXT NOT NULL DEFAULT '',
    image_path  TEXT,
    vec_id      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc_page ON chunks(doc_id, page_idx);
CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(kind);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    image_path  TEXT,
    blocks      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(DDL)
    _migrate(conn)
    conn.commit()
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """缺列就补；返回是否真的动过。老库不重建，数据留着。"""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def _migrate(conn: sqlite3.Connection) -> None:
    """补列：升级的人不用重建库。

    - documents.attempt：重投次数，幂等锁按它区分轮次
    - messages.blocks：图文混排的 answer blocks（历史消息刷新不丢图）
    """
    _add_column(conn, "documents", "attempt", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "messages", "blocks", "TEXT")
    conn.commit()


def _row_to_document(row: sqlite3.Row) -> Document:
    keys = row.keys()
    return Document(
        id=row["id"],
        filename=row["filename"],
        status=DocStatus(row["status"]),
        pages=row["pages"],
        size_bytes=row["size_bytes"],
        attempt=row["attempt"] if "attempt" in keys else 1,
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    bbox = row["bbox"]
    return Chunk(
        id=row["id"],
        doc_id=row["doc_id"],
        kind=ChunkKind(row["kind"]),
        page_idx=row["page_idx"],
        bbox=tuple(float(x) for x in bbox.split(",")) if bbox else None,
        content=row["content"],
        image_path=row["image_path"],
        vec_id=row["vec_id"],
    )


def create_document(
    conn: sqlite3.Connection, *, doc_id: str, filename: str, size_bytes: int
) -> Document:
    now = utcnow().isoformat()
    conn.execute(
        "INSERT INTO documents (id, filename, status, pages, size_bytes, attempt, created_at,"
        " updated_at) VALUES (?, ?, ?, 0, ?, 1, ?, ?)",
        (doc_id, filename, DocStatus.PENDING.value, size_bytes, now, now),
    )
    conn.commit()
    found = get_document(conn, doc_id)
    assert found is not None
    return found


def bump_attempt(conn: sqlite3.Connection, doc_id: str) -> int:
    """重投时递增，幂等锁 key 里带它，避免新任务被上一轮的锁挡住。"""
    conn.execute(
        "UPDATE documents SET attempt = attempt + 1, updated_at = ? WHERE id = ?",
        (utcnow().isoformat(), doc_id),
    )
    conn.commit()
    row = conn.execute("SELECT attempt FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return int(row["attempt"]) if row else 1


def set_status(
    conn: sqlite3.Connection,
    doc_id: str,
    status: DocStatus,
    *,
    pages: int | None = None,
    error: str | None = None,
) -> None:
    now = utcnow().isoformat()
    if pages is None:
        conn.execute(
            "UPDATE documents SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status.value, error, now, doc_id),
        )
    else:
        conn.execute(
            "UPDATE documents SET status = ?, pages = ?, error = ?, updated_at = ? WHERE id = ?",
            (status.value, pages, error, now, doc_id),
        )
    conn.commit()


def get_document(conn: sqlite3.Connection, doc_id: str) -> Document | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return _row_to_document(row) if row else None


def stale_parsing(conn: sqlite3.Connection, *, older_than: datetime) -> list[Document]:
    """卡在 parsing 且最后更新时间早于阈值的文档——看门狗要收的尸。

    worker 被 OOM 杀掉、实例被回收、消息重投不上来时，这些文档会永远停在
    「解析中」，前端一直转圈。靠时间判断比靠「有没有心跳」简单，也不用额外存东西。
    """
    rows = conn.execute(
        "SELECT * FROM documents WHERE status = ? AND updated_at < ? ORDER BY updated_at",
        (DocStatus.PARSING.value, older_than.isoformat()),
    ).fetchall()
    return [_row_to_document(row) for row in rows]


def list_documents(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Document]:
    """新的在前。SSE 首帧快照和列表接口都靠它，别把几万份全吐出去。"""
    sql = "SELECT * FROM documents ORDER BY created_at DESC"
    params: tuple[object, ...] = ()
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_document(row) for row in rows]


def replace_chunks(conn: sqlite3.Connection, doc_id: str, chunks: Iterable[Chunk]) -> int:
    """重解析时整份替换，避免旧块残留；调用方负责先删旧向量。"""
    payload = [
        (
            chunk.id,
            chunk.doc_id,
            chunk.kind.value,
            chunk.page_idx,
            ",".join(str(v) for v in chunk.bbox) if chunk.bbox else None,
            chunk.content,
            chunk.image_path,
            chunk.vec_id,
        )
        for chunk in chunks
    ]
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    conn.executemany(
        "INSERT INTO chunks (id, doc_id, kind, page_idx, bbox, content, image_path, vec_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()
    return len(payload)


def list_chunks(conn: sqlite3.Connection, doc_id: str) -> list[Chunk]:
    rows = conn.execute(
        "SELECT * FROM chunks WHERE doc_id = ? ORDER BY page_idx, kind", (doc_id,)
    ).fetchall()
    return [_row_to_chunk(row) for row in rows]


def text_chunks_with_vectors(conn: sqlite3.Connection) -> list[Chunk]:
    """索引与检索都只关心有向量的块（kind=text/table）。"""
    rows = conn.execute("SELECT * FROM chunks WHERE vec_id IS NOT NULL ORDER BY vec_id").fetchall()
    return [_row_to_chunk(row) for row in rows]


def set_chunk_vectors(conn: sqlite3.Connection, pairs: Sequence[tuple[int, str]]) -> int:
    """回填 vec_id：数字是 ``vectors.npy`` 的行号，按行对齐，改完必须落盘。"""
    if not pairs:
        return 0
    conn.executemany("UPDATE chunks SET vec_id = ? WHERE id = ?", pairs)
    conn.commit()
    return len(pairs)


def clear_vectors(conn: sqlite3.Connection) -> None:
    """全量重建前先把所有引用解开，避免行号错位。"""
    conn.execute("UPDATE chunks SET vec_id = NULL")
    conn.commit()


def chunks_in_pages(
    conn: sqlite3.Connection, doc_id: str, pages: Sequence[int], kind: ChunkKind
) -> list[Chunk]:
    """同页召回：命中某一页后，把这页上的指定类型块取出来。

    图片块不进向量（没有可编码的文本），表格块的 markdown 竖线对 embedding
    不友好、常常排不进 top-k——两者都靠「同页正文被命中」带出来。
    """
    if not pages:
        return []
    marks = ",".join("?" for _ in pages)
    rows = conn.execute(
        f"SELECT * FROM chunks WHERE doc_id = ? AND kind = ? AND page_idx IN ({marks})"
        " ORDER BY page_idx, id",
        (doc_id, kind.value, *pages),
    ).fetchall()
    return [_row_to_chunk(row) for row in rows]


def images_in_pages(conn: sqlite3.Connection, doc_id: str, pages: Sequence[int]) -> list[Chunk]:
    """同页召回：命中文本块后，取这些页上的图片块。"""
    return chunks_in_pages(conn, doc_id, pages, ChunkKind.IMAGE)


def add_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    session_id: str,
    role: Role,
    text: str = "",
    image_path: str | None = None,
    blocks: list[dict] | None = None,
) -> ChatMessage:
    now = utcnow().isoformat()
    blocks_json = json.dumps(blocks, ensure_ascii=False) if blocks else None
    conn.execute(
        "INSERT INTO messages (id, session_id, role, text, image_path, blocks, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (message_id, session_id, role.value, text, image_path, blocks_json, now),
    )
    conn.commit()
    return ChatMessage(
        id=message_id,
        session_id=session_id,
        role=role,
        text=text,
        image_path=image_path,
        blocks=blocks or [],
    )


def image_chunks(conn: sqlite3.Connection, doc_id: str) -> list[Chunk]:
    """整篇的图片块；用来把 MinerU 的块定位符换成真实 PNG。"""
    rows = conn.execute(
        "SELECT * FROM chunks WHERE doc_id = ? AND kind = ? ORDER BY page_idx, id",
        (doc_id, ChunkKind.IMAGE.value),
    ).fetchall()
    return [_row_to_chunk(row) for row in rows]


def set_image_paths(conn: sqlite3.Connection, pairs: Sequence[tuple[int, str]]) -> int:
    """批量改写图片路径（chunk_id → parsed/images/xx.png）。"""
    if not pairs:
        return 0
    conn.executemany("UPDATE chunks SET image_path = ? WHERE id = ?", pairs)
    conn.commit()
    return len(pairs)


def list_messages(
    conn: sqlite3.Connection, session_id: str, *, limit: int | None = None
) -> list[ChatMessage]:
    """按时间正序，带 blocks；前端直接渲染，不用自己拼。

    ``limit`` 取**最新** N 条（先倒序截断再翻回来），长会话不至于整段吐给前端。
    """
    if limit is not None and limit > 0:
        sql = (
            "SELECT * FROM (SELECT rowid AS rid, * FROM messages WHERE session_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, rid"
        )
        rows = conn.execute(sql, (session_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid",
            (session_id,),
        ).fetchall()
    messages: list[ChatMessage] = []
    for row in rows:
        blocks = json.loads(row["blocks"]) if row["blocks"] else []
        messages.append(
            ChatMessage(
                id=row["id"],
                session_id=row["session_id"],
                role=Role(row["role"]),
                text=row["text"],
                image_path=row["image_path"],
                blocks=blocks,
                created_at=row["created_at"],
            )
        )
    return messages


def list_sessions(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, object]]:
    """会话列表：最后活动时间、条数、首条用户提问当标题。

    只为了让前端左侧能列出「历史会话」；标题没单独存，用第一句问话截断代替，
    这样不用维护额外字段，历史会话也不会因为改标题而多一次写。
    """
    rows = conn.execute(
        """
        SELECT s.session_id, s.updated_at, s.total,
               COALESCE((SELECT u.text FROM messages AS u
                          WHERE u.session_id = s.session_id AND u.role = 'user'
                          ORDER BY u.created_at, u.rowid LIMIT 1), '') AS title
        FROM (
            SELECT session_id, MAX(created_at) AS updated_at, COUNT(*) AS total
            FROM messages GROUP BY session_id
        ) AS s
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
