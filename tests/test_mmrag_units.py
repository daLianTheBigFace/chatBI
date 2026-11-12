"""mmrag 的纯逻辑单测：不碰 Kafka / Redis / SSH，跑得快，改错了立刻红。

外部依赖（Kafka、Redis、实例上的 MinerU）由 scripts/mmrag_plumbing_smoke.py 覆盖，
这里只管"给定输入能不能切出对的块"和"路径拼得对不对"。
"""

from __future__ import annotations

from pathlib import Path

from chatbi.mmrag.bridge.transfer import remote_in_path, remote_out_path
from chatbi.mmrag.bridge.watchdog import sweep
from chatbi.mmrag.config import MineruSettings
from chatbi.mmrag.db import (
    add_message,
    create_document,
    get_document,
    images_in_pages,
    init_db,
    list_documents,
    list_messages,
    replace_chunks,
    set_status,
)
from chatbi.mmrag.ingest.chunker import build_chunks, load_items, split_text
from chatbi.mmrag.models import ChunkKind, DocStatus, ImageBlock, Role

SAMPLE_MARKDOWN = """<!-- page 1 of 2 -->

## Page 1

这是一段正文，用来验证切页。

| 项目 | 数值 |
| --- | --- |
| 测试行 | 123 |

![图一](images/p1_0.png)

<!-- page 2 of 2 -->

## Page 2

第二页正文。
"""


def _sample_items() -> list[dict]:
    return [
        {"type": "text", "text": "第一页正文", "page_idx": 0, "bbox": [10, 10, 100, 30]},
        {"type": "table", "table_body": "| a | b |\n| 1 | 2 |", "page_idx": 0},
        {"type": "image", "img_path": "images/p1.png", "page_idx": 0, "img_caption": ["图一"]},
        {"type": "text", "text": "第二页正文", "page_idx": 1},
    ]


def test_split_text_respects_window() -> None:
    long_text = "这是一句测试。" * 200
    pieces = split_text(long_text)
    assert len(pieces) > 1
    assert all(len(piece) <= 800 for piece in pieces)
    assert "".join(pieces).replace(" ", "") == long_text.replace(" ", "")


def test_build_chunks_keeps_three_kinds(tmp_path: Path) -> None:
    chunks = list(build_chunks("d1", _sample_items(), parsed_dir=tmp_path))
    kinds = {chunk.kind for chunk in chunks}
    assert kinds == {ChunkKind.TEXT, ChunkKind.TABLE, ChunkKind.IMAGE}

    image = next(chunk for chunk in chunks if chunk.kind is ChunkKind.IMAGE)
    # 图片块不进向量：vec_id 为空，content 用 caption 当召回钩子
    assert image.vec_id is None
    assert image.content == "图一"
    assert image.image_path == "parsed/images/p1.png"


def test_load_items_falls_back_to_markdown(tmp_path: Path) -> None:
    """MinerU 4.x 只给 markdown 时必须能切，不能因为缺 content_list.json 就失败。"""
    (tmp_path / "parsed.md").write_text(SAMPLE_MARKDOWN, encoding="utf-8")

    items = load_items(tmp_path)
    kinds = [item["type"] for item in items]
    assert kinds == ["text", "table", "image", "text"]

    assert items[0]["page_idx"] == 0 and "正文" in items[0]["text"]
    assert items[1]["table_body"].startswith("| 项目 |")
    assert items[2]["img_path"] == "images/p1_0.png"
    assert items[3]["page_idx"] == 1


def test_remote_paths_are_posix() -> None:
    """Windows 上 Path 会把 / 变成 \\，拼进 ssh/scp 就直接废了。"""
    settings = MineruSettings()
    assert remote_in_path(settings, "abc") == "/workspace/software/data/mmrag/in/abc.pdf"
    assert remote_out_path(settings, "abc") == "/workspace/software/data/mmrag/out/abc"
    assert "\\" not in remote_in_path(settings, "abc")


def test_db_roundtrip(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "rag.db")
    create_document(conn, doc_id="d1", filename="a.pdf", size_bytes=123)
    assert list_documents(conn)[0].status is DocStatus.PENDING

    set_status(conn, "d1", DocStatus.DONE, pages=2)
    doc = list_documents(conn)[0]
    assert doc.status is DocStatus.DONE and doc.pages == 2

    chunks = list(build_chunks("d1", _sample_items(), parsed_dir=tmp_path))
    assert replace_chunks(conn, "d1", chunks) == len(chunks)

    images = images_in_pages(conn, "d1", [0])
    assert [chunk.kind for chunk in images] == [ChunkKind.IMAGE]

    replace_chunks(conn, "d1", [])
    assert images_in_pages(conn, "d1", [0]) == []
    conn.close()


def test_message_blocks_roundtrip(tmp_path: Path) -> None:
    """assistant 消息的图片块要能存下来再原样读回，刷新页面不丢图。"""
    conn = init_db(tmp_path / "rag.db")
    blocks = [
        {
            "type": "text",
            "text": "见第 2 页的架构图",
            "citations": [{"doc_id": "d1", "filename": "a.pdf", "page_idx": 1}],
        },
        {"type": "image", "url": "/static/docs/d1/parsed/images/p1.png", "caption": "图 1"},
    ]
    add_message(
        conn,
        message_id="m1",
        session_id="s1",
        role=Role.ASSISTANT,
        text="见第 2 页的架构图",
        blocks=blocks,
    )
    stored = list_messages(conn, "s1")
    assert len(stored) == 1
    assert [block.type for block in stored[0].blocks] == ["text", "image"]
    image = stored[0].blocks[1]
    assert isinstance(image, ImageBlock)
    assert image.url.endswith("p1.png")
    conn.close()


def test_watchdog_fails_stale_parsing(tmp_path: Path) -> None:
    """卡在 parsing 太久的文档要判失败，否则前端永远转圈。"""
    conn = init_db(tmp_path / "rag.db")
    create_document(conn, doc_id="d1", filename="a.pdf", size_bytes=1)
    set_status(conn, "d1", DocStatus.PARSING)
    # 把 updated_at 拨回 2020 年，模拟 worker 被 OOM 杀掉、result 永远不来
    conn.execute(
        "UPDATE documents SET updated_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", "d1")
    )
    conn.commit()

    notified: list[str] = []
    expired = sweep(conn, notify=lambda event: notified.append(event.status), timeout_minutes=30)

    assert expired == ["d1"]
    assert notified == ["failed"]
    document = get_document(conn, "d1")
    assert document is not None and document.status is DocStatus.FAILED
    assert document.error is not None and "超时" in document.error
    conn.close()
