"""FastAPI 应用工厂：把 mmrag 挂进现有服务（也可以单独起）。

挂载点：
    /api/mmrag/docs/*   上传、列表、状态、SSE
    /api/mmrag/chat/*   图文问答
    /static/docs        data/mmrag/docs 静态映射（answer_blocks 里的图走这里）
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from chatbi.mmrag.api import chat as chat_api
from chatbi.mmrag.api import docs as docs_api
from chatbi.mmrag.api import sessions as sessions_api
from chatbi.mmrag.api import web as web_api
from chatbi.mmrag.api.publisher import RequestPublisher
from chatbi.mmrag.config import MMRagSettings, load_settings
from chatbi.mmrag.db import init_db
from chatbi.mmrag.ingest.embedder import Embedder

log = logging.getLogger(__name__)


def create_app(project_root: Path | None = None) -> FastAPI:
    root = project_root or Path(__file__).resolve().parents[4]
    settings: MMRagSettings = load_settings(root)
    settings.paths.ensure()
    init_db(settings.paths.db_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动时只建连接，不加载模型：模型第一次用到才读，进程起来快
        app.state.publisher = RequestPublisher(settings.kafka)
        embedder = Embedder(settings.embed)
        app.state.embedder = embedder
        # 后台预热：加载模型要十几秒，别让第一个提问的人等这一下。
        # 测试里设 MMRAG_EMBED_WARMUP=0 关掉——否则每个用例都白等一次加载，
        # 进程退出时线程池还要等它跑完，整个测试套件能卡好几分钟。
        warm = None
        if os.environ.get("MMRAG_EMBED_WARMUP", "1") != "0":
            warm = asyncio.create_task(asyncio.to_thread(embedder.warmup))
        log.info("mmrag api ready (data=%s)", settings.paths.base)
        try:
            yield
        finally:
            if warm is not None:
                warm.cancel()
            app.state.publisher.close()

    app = FastAPI(title="multimodal RAG", version="0.1.0", lifespan=lifespan)
    app.state.mmrag = settings
    app.include_router(docs_api.router)
    app.include_router(chat_api.router)
    app.include_router(sessions_api.router)
    app.include_router(web_api.router)
    app.mount(
        "/static/docs",
        StaticFiles(directory=settings.paths.docs_dir),
        name="mmrag-static",
    )
    return app



