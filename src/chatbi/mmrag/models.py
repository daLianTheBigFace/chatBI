"""领域模型：文档、块、消息、回答块。

设计要点（见 docs/mmrag-structure.md）：
- 一份文档里放两种 kind 的块（text/table/image），不按图文拆成两份文档；
  doc_id 唯一，页码引用、删除、重解析都只有一套主键。
- 文本块进向量库；图片块不进向量，靠「命中同页文本」召回。
- 用户提问附带的那张图只在会话里（messages.image_path），不入库、不建块。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class DocStatus(StrEnum):
    """前端徽标一一对应：待解析 / 解析中 / 解析完成 / 失败。"""

    PENDING = "pending"
    PARSING = "parsing"
    DONE = "done"
    FAILED = "failed"


class ChunkKind(StrEnum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    filename: str
    status: DocStatus = DocStatus.PENDING
    pages: int = 0
    size_bytes: int = 0
    attempt: int = 1
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Chunk(BaseModel):
    """检索与引用的最小单位。

    ``vec_id`` 与 ``vectors.npy`` 的行号对齐；图片块为 None（不向量化）。
    ``content``：text 是正文，table 是 markdown/HTML，image 是同页上下文摘要。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    doc_id: str
    kind: ChunkKind
    page_idx: int = Field(ge=0)
    bbox: tuple[float, float, float, float] | None = None
    content: str = ""
    image_path: str | None = None
    vec_id: int | None = None


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    filename: str
    page_idx: int


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
    citations: list[Citation] = Field(default_factory=list)


class ImageBlock(BaseModel):
    """图只给 URL，绝不返回 base64。"""

    type: Literal["image"] = "image"
    url: str
    caption: str = ""
    page_idx: int | None = None


class TableBlock(BaseModel):
    type: Literal["table"] = "table"
    markdown: str
    citations: list[Citation] = Field(default_factory=list)


AnswerBlock = Annotated[TextBlock | ImageBlock | TableBlock, Field(discriminator="type")]


class ChatMessage(BaseModel):
    """会话消息。

    ``blocks`` 存 assistant 的结构化回复（含图片 URL），刷新页面能原样渲染；
    ``text`` 是拼好的纯文本，给复制/检索用，不给前端渲染。
    role=user 时 ``image_path`` 指向用户上传的那张图（一次性，不进库）。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    session_id: str
    role: Role
    text: str = ""
    image_path: str | None = None
    blocks: list[AnswerBlock] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class Answer(BaseModel):
    blocks: list[AnswerBlock] = Field(default_factory=list)


class RetrievedChunk(BaseModel):
    chunk: Chunk
    score: float
