"""页面级路由：只给人看的，不参与 API 契约。

前端就是一个单文件（``web/index.html``），没有构建步骤：
上传、状态徽标、图文问答全在里面，用原生 fetch + EventSource。
改完刷新浏览器就生效，不用重启 uvicorn（文件是每次请求现读的）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response

router = APIRouter(tags=["mmrag-web"])

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


@router.get("/mmrag", include_in_schema=False)
def page() -> Response:
    index = WEB_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse("<h1>前端还没生成</h1>", status_code=404)
    return FileResponse(index, media_type="text/html; charset=utf-8")

