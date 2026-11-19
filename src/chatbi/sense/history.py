"""多轮记忆：SQLite 存会话与消息，和 mmrag 的 rag.db 分开（两个进程并发写).

取历史时按「轮」而不是按「条」——一轮算一问一答，要带就带完整的；
半截的上下文比没有上下文更容易让回应跑偏。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from chatbi.sense.contracts import ChatTurn, Emotion

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    sense_label TEXT,
    blocks TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _label_of(sense_label: Emotion | str | None) -> str | None:
    """存字符串而不是枚举值：库里的东西要能被别的语言读。

    接受 str 是为了让调用方不用为了一个字段先包一层枚举。
    """
    if sense_label is None:
        return None
    return sense_label.value if isinstance(sense_label, Emotion) else str(sense_label)


def append(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    text: str,
    sense_label: Emotion | str | None = None,
    blocks: list[dict] | None = None,
) -> str:
    message_id = uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO conversations (session_id, created_at, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at",
        (session_id, now, now),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, role, text, sense_label, blocks, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            session_id,
            role,
            text,
            _label_of(sense_label),
            None if blocks is None else json.dumps(blocks, ensure_ascii=False),
            now,
        ),
    )
    conn.commit()
    return message_id


def load_turns(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    turns: int,
    char_limit: int,
    single_char_limit: int,
) -> list[ChatTurn]:
    """取最近 N 轮，按时间正序返回。超预算就整轮丢弃，不留半轮。"""
    rows = conn.execute(
        "SELECT role, text, sense_label FROM messages WHERE session_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (session_id, turns * 2),
    ).fetchall()

    budget = char_limit
    newest_first: list[ChatTurn] = []
    # rows 已经是最新在前：装到装不下为止，丢的是更早的那几轮，不是刚说的
    for row in rows:
        text = (row["text"] or "")[:single_char_limit]
        if len(text) > budget:
            break
        budget -= len(text)
        label = Emotion(row["sense_label"]) if row["sense_label"] else None
        newest_first.append(ChatTurn(role=row["role"], text=text, sense_label=label))
    return list(reversed(newest_first))


def list_sessions(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT c.session_id, c.updated_at,"
        " (SELECT COUNT(*) FROM messages m WHERE m.session_id = c.session_id) AS total,"
        " COALESCE((SELECT u.text FROM messages u WHERE u.session_id = c.session_id"
        "   AND u.role = 'user' ORDER BY u.created_at, u.rowid LIMIT 1), '') AS title"
        " FROM conversations c ORDER BY c.updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
