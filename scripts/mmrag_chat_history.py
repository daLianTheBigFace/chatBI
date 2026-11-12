"""看文档问答的对话记录。

用法：
    python scripts/mmrag_chat_history.py                 # 列出最近的会话
    python scripts/mmrag_chat_history.py <session_id>    # 打印该会话全文
    python scripts/mmrag_chat_history.py <session_id> --md out.md   # 导出 markdown

记录都在本机 data/mmrag/rag.db 的 messages 表，实例上只有解析产物，没有对话。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "mmrag" / "rag.db"


def sessions(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "select s.session_id, s.n, s.last,"
        " coalesce((select text from messages m where m.session_id = s.session_id"
        "   and m.role = 'user' order by m.created_at limit 1), '') as title"
        " from (select session_id, count(*) n, max(created_at) last from messages"
        "   group by session_id) s order by s.last desc"
    ).fetchall()
    print(f"{'session':16} {'消息':>4}  {'最后活动':20}  首个问题")
    for row in rows:
        print(f"{row['session_id']:16} {row['n']:>4}  {row['last'][:19]:20}  {row['title'][:40]}")


def messages(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "select role, text, blocks, created_at from messages where session_id = ?"
        " order by created_at, rowid",
        (session_id,),
    ).fetchall()


def render(rows: list[sqlite3.Row], to_markdown: bool) -> str:
    out: list[str] = []
    for row in rows:
        who = "我" if row["role"] == "user" else "助手"
        stamp = row["created_at"][:19].replace("T", " ")
        if to_markdown:
            out.append(f"### {who} · {stamp}\n\n{row['text']}\n")
        else:
            out.append(f"--- {who} {stamp} ---")
            out.append(row["text"])
        blocks = json.loads(row["blocks"]) if row["blocks"] else []
        images = [b.get("url") for b in blocks if b.get("type") == "image"]
        if images:
            out.append(
                "\n".join(f"![图]({url})" if to_markdown else f"[图] {url}" for url in images)
            )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", nargs="?", help="不填就列出所有会话")
    parser.add_argument("--md", help="导出到指定 markdown 文件")
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    if not args.session:
        sessions(conn)
        return 0

    rows = messages(conn, args.session)
    if not rows:
        print(f"会话 {args.session} 没有消息")
        return 1
    text = render(rows, bool(args.md))
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
        print(f"已导出 {len(rows)} 条 -> {args.md}")
    else:
        print(text)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
