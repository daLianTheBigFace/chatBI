"""一条任务的处理流程（worker 侧，实例上跑）。

    ① 消费 + 幂等（Redis SETNX parse:lock:{doc_id}:{attempt}，TTL 30min）
    ② 调 MinerU（本地 CLI）
    ③ 产物留在实例磁盘（本机稍后 scp 拉走，不在这里推）
    ④ 回写 result + 记一行台账

失败重试策略：MinerU 报错直接 result.ok=false；进程崩溃靠消费组重投 +
接口层看门狗（parsing 超过 15 分钟判 failed）。
"""

from __future__ import annotations

import logging

from chatbi.mmrag.config import MMRagSettings
from chatbi.mmrag.contracts import ParseRequest, ParseResult
from chatbi.mmrag.worker import audit
from chatbi.mmrag.worker.mineru import MineruError, parse_pdf

log = logging.getLogger(__name__)


def handle(request: ParseRequest, settings: MMRagSettings) -> ParseResult:
    """纯函数式入口，便于单测；副作用只有磁盘、MinerU 进程与台账。"""
    remote_in = settings.mineru.remote_out_dir / "in" / f"{request.doc_id}.pdf"
    out_dir = settings.mineru.remote_out_dir / "out" / request.doc_id
    base = {
        "doc_id": request.doc_id,
        "filename": request.filename,
        "attempt": request.attempt,
        "tier": settings.mineru.tier,
        "pages_requested": settings.mineru.pages,
    }
    if not remote_in.exists():
        audit.record(settings, **base, ok=False, error=f"input pdf missing: {remote_in}")
        return ParseResult(
            doc_id=request.doc_id,
            ok=False,
            attempt=request.attempt,
            error=f"input pdf missing: {remote_in}",
        )
    try:
        produced = parse_pdf(settings.mineru, remote_in, out_dir)
    except MineruError as exc:
        log.warning("parse failed doc=%s: %s", request.doc_id, exc)
        audit.record(settings, **base, ok=False, error=str(exc)[:500])
        return ParseResult(doc_id=request.doc_id, ok=False, attempt=request.attempt, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 台账要记下所有异常，否则实例上查不到原因
        log.exception("parse crashed doc=%s", request.doc_id)
        audit.record(settings, **base, ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        raise
    audit.record(
        settings,
        **base,
        ok=True,
        pages=produced.pages,
        elapsed_ms=produced.elapsed_ms,
        cache_hit=produced.cache_hit,
        out_dir=str(produced.out_dir),
    )
    log.info(
        "parsed doc=%s file=%r pages=%d cache_hit=%s elapsed=%dms",
        request.doc_id,
        request.filename,
        produced.pages,
        produced.cache_hit,
        produced.elapsed_ms,
    )
    return ParseResult(
        doc_id=request.doc_id,
        ok=True,
        pages=produced.pages,
        parsed_dir=str(produced.out_dir),
        elapsed_ms=produced.elapsed_ms,
        attempt=request.attempt,
    )
