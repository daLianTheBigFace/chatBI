"""上传流程：落盘 → 建行(pending) → scp 到实例 → 投 Kafka。

为什么单独一个模块：``/docs/upload`` 和 ``/chat/stream``（一次带多个附件）都要走，
两条入口的行为必须完全一致，否则「批量上传」和「聊天里带上」会出现文档状态对不上。

顺序不能反：**先把 PDF 推到实例，再投 Kafka**。反过来的话 worker 可能先拿到消息，
去远端读一个还没传完的文件，只能报 ``input pdf missing``。
"""

from __future__ import annotations

import logging

from anyio import to_thread
from fastapi import HTTPException, UploadFile

from chatbi.mmrag.api.deps import Context, Publisher
from chatbi.mmrag.bridge.transfer import push_pdf
from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.contracts import ParseRequest
from chatbi.mmrag.db import create_document, get_document, set_status
from chatbi.mmrag.models import DocStatus, Document
from chatbi.mmrag.storage import UploadRejected, new_doc_id, save_upload

log = logging.getLogger(__name__)


async def ingest_files(
    files: list[UploadFile],
    *,
    ctx: Context,
    settings: MMRagSettings,
    publisher: Publisher,
) -> list[Document]:
    """逐份处理；单份失败只让它自己 failed，不拖累同批其他文件。"""
    created: list[Document] = []
    for upload_file in files:
        doc_id = new_doc_id()
        filename = upload_file.filename or f"{doc_id}.pdf"
        try:
            path, size = save_upload(
                settings.paths,
                doc_id=doc_id,
                filename=filename,
                stream=upload_file.file,
                max_bytes=settings.upload.max_megabytes * 1024 * 1024,
                allowed_suffixes=settings.upload.allowed_suffixes,
            )
        except UploadRejected as exc:
            raise HTTPException(status_code=400, detail=f"{filename}: {exc}") from exc

        create_document(ctx.conn, doc_id=doc_id, filename=filename, size_bytes=size)
        try:
            remote = await to_thread.run_sync(push_pdf, settings.mineru, doc_id, path)
        except Exception as exc:  # noqa: BLE001 - 网络问题都收在这一层
            log.exception("push failed doc=%s", doc_id)
            document = set_status(
                ctx.conn, doc_id, DocStatus.FAILED, error=f"上传到实例失败：{exc}"
            )
            created.append(document)
            continue

        try:
            await publisher.send(
                ParseRequest(doc_id=doc_id, filename=filename, raw_path=remote)
            )
        except Exception as exc:  # noqa: BLE001 - Kafka 挂了要能看见
            log.exception("kafka send failed doc=%s", doc_id)
            document = set_status(
                ctx.conn, doc_id, DocStatus.FAILED, error=f"投递失败：{exc}"
            )
            created.append(document)
            continue

        document = get_document(ctx.conn, doc_id)
        if document is None:  # 理论上不可能，防御一下
            raise HTTPException(status_code=500, detail=f"{filename}: 建行失败")
        created.append(document)

    return created

