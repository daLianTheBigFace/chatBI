"""API 层测试：不连 Kafka、不调模型、不读真模型文件。

Kafka 生产者与 Redis 广播都用猴子补丁顶掉——这些是协议层，
已经在 scripts/mmrag_plumbing_smoke.py 里对着真实实例验过；
这里只保证「路由 → 数据流 → SSE 帧」这条逻辑链是对的。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# 测试里不预热 embedding：每个用例都加载一次模型要十几秒，还会拖住进程退出
os.environ.setdefault("MMRAG_EMBED_WARMUP", "0")
from fastapi.testclient import TestClient

from chatbi.mmrag.api.app import create_app
from chatbi.mmrag.api.publisher import RequestPublisher
from chatbi.mmrag.contracts import ParseRequest
from chatbi.mmrag.rag.retriever import Hit
from chatbi.mmrag.storage import new_doc_id

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class _FakePublisher:
    def __init__(self) -> None:
        self.sent: list[ParseRequest] = []

    async def send(self, request: ParseRequest) -> None:
        self.sent.append(request)

    def close(self) -> None:
        return None


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app(project_root=tmp_path)

    fake = _FakePublisher()
    monkeypatch.setattr(RequestPublisher, "__init__", lambda self, *_a, **_k: None)
    monkeypatch.setattr(app.state, "publisher", fake, raising=False)
    # 上传会真的 scp 到实例；测试里顶掉，只验「先推后投」的顺序与状态
    monkeypatch.setattr(
        "chatbi.mmrag.api.uploads.push_pdf",
        lambda _settings, doc_id, _path: f"/remote/in/{doc_id}.pdf",
    )

    with TestClient(app) as test_client:
        test_client.app.state.publisher = fake
        yield test_client, fake


def test_upload_creates_pending_documents(client) -> None:
    test_client, fake = client
    response = test_client.post(
        "/api/mmrag/docs/upload",
        files=[
            ("files", ("a.pdf", PDF, "application/pdf")),
            ("files", ("b.pdf", PDF, "application/pdf")),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["status"] for item in body] == ["pending", "pending"]
    assert len(fake.sent) == 2
    assert {item["filename"] for item in body} == {"a.pdf", "b.pdf"}


def test_upload_rejects_non_pdf(client) -> None:
    test_client, _ = client
    response = test_client.post(
        "/api/mmrag/docs/upload", files=[("files", ("a.txt", b"hi", "text/plain"))]
    )
    assert response.status_code == 400


def test_list_status_and_detail(client) -> None:
    test_client, _ = client
    created = test_client.post(
        "/api/mmrag/docs/upload", files=[("files", ("a.pdf", PDF, "application/pdf"))]
    ).json()[0]

    listed = test_client.get("/api/mmrag/docs").json()
    assert [item["id"] for item in listed] == [created["id"]]

    status = test_client.get(f"/api/mmrag/docs/status?ids={created['id']},nope").json()
    assert len(status) == 1 and status[0]["status"] == "pending"

    assert test_client.get(f"/api/mmrag/docs/{created['id']}").status_code == 200
    assert test_client.get("/api/mmrag/docs/nope").status_code == 404


def test_reparse_bumps_attempt(client) -> None:
    test_client, fake = client
    created = test_client.post(
        "/api/mmrag/docs/upload", files=[("files", ("a.pdf", PDF, "application/pdf"))]
    ).json()[0]

    response = test_client.post(f"/api/mmrag/docs/{created['id']}/reparse")
    assert response.status_code == 200, response.text
    assert response.json()["attempt"] == 2
    assert fake.sent[-1].attempt == 2


def test_sessions_empty_then_message_blocks(client) -> None:
    test_client, _ = client
    assert test_client.get("/api/mmrag/sessions/s1/messages").json() == []


def test_chat_stream_emits_meta_delta_done(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _ = client

    monkeypatch.setattr("chatbi.mmrag.api.chat.search", lambda *a, **k: [])
    monkeypatch.setattr(
        "chatbi.mmrag.api.chat.prompt_mod.build_messages", lambda *a, **k: [{"role": "user"}]
    )

    async def fake_stream(_messages, _settings) -> AsyncIterator[str]:
        yield "第一段"
        yield "，第二段"

    monkeypatch.setattr("chatbi.mmrag.api.chat.llm_mod.stream", fake_stream)

    with test_client.stream(
        "POST",
        "/api/mmrag/chat/stream",
        data={"question": "这篇讲什么", "session_id": "s9"},
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    assert "event: meta" in text
    assert "event: delta" in text
    assert "event: done" in text
    assert "第一段" in text

    history = test_client.get("/api/mmrag/sessions/s9/messages").json()
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[-1]["blocks"][0]["type"] == "text"


def test_answer_blocks_include_same_page_images() -> None:
    """命中落在某页时，同页图片要能带出来（图不进向量，靠页号召回）。"""
    from chatbi.mmrag.models import Chunk, ChunkKind
    from chatbi.mmrag.rag.answer import image_blocks

    image = Chunk(
        id="img1",
        doc_id="d1",
        kind=ChunkKind.IMAGE,
        page_idx=1,
        content="架构图",
        image_path="d1/parsed/images/p1_0.png",
    )
    text = Chunk(id="t1", doc_id="d1", kind=ChunkKind.TEXT, page_idx=1, content="正文")
    hit = Hit(chunk=text, score=0.9, filename="a.pdf", images=(image,))
    blocks = image_blocks([hit], limit=3)
    assert len(blocks) == 1
    assert blocks[0].url.endswith("p1_0.png")
    assert "base64" not in blocks[0].url


def test_new_doc_id_is_short_and_unique() -> None:
    a, b = new_doc_id(), new_doc_id()
    assert a != b and len(a) == 12


def test_web_page_served(tmp_path: Path) -> None:
    """页面要能被直接打开：一个 HTML 文件，不依赖构建。"""
    app = create_app(project_root=tmp_path)
    with TestClient(app) as client:
        client.app.state.publisher = _FakePublisher()
        html = client.get("/mmrag")
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]
    assert "多模态 RAG" in html.text
    assert "EventSource" in html.text and "/chat/stream" in html.text
