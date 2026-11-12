"""调本地 MinerU 4.x 客户端（worker 与 mineru 同机，走 subprocess，不绕 SSH）。

命令形状：

    {mineru_bin} parse {pdf} --tier standard --pages all --format markdown \
        -o {out_dir} --no-wait --json

坑（已踩过）：
- 4.x 与 2.x 参数不兼容，``-p/-o/-b`` 语义全变；
- ``--pages`` 默认只解析前 10 页，长文档必须显式 all；
- ``--wait`` 默认 60 秒，长文档要用 ``--no-wait`` 再轮询，或把 wait 调大；
- 首次调用前要确认 ``mineru server status`` 里 Local parse-server 是 Healthy。
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from chatbi.mmrag.config import MineruSettings


@dataclass(frozen=True, slots=True)
class MineruOutput:
    out_dir: Path
    pages: int
    elapsed_ms: int
    cache_hit: bool = False
    tier: str = ""


class MineruError(RuntimeError):
    pass


def build_command(settings: MineruSettings, pdf: Path, out_dir: Path) -> list[str]:
    """``-o`` 是「输出文件路径」不是目录：给目录会得到一个同名普通文件。"""
    return [
        settings.mineru_bin,
        "parse",
        str(pdf),
        "--tier",
        settings.tier,
        "--pages",
        settings.pages,
        "--format",
        "markdown",
        "-o",
        str(out_dir / "parsed.md"),
        "--json",
    ]


def _loads_output(stdout: str) -> dict:
    """4.x 的 ``--json`` 打的是多行 pretty JSON，不是单行；解析失败再退到末行。"""
    text = stdout.strip()
    if not text:
        raise MineruError("mineru printed nothing")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise MineruError("cannot parse mineru json output")


def _pages(payload: dict) -> int:
    """页数藏在 ``parse.page_range``（形如 ``1-2``），拿不到就回 0。"""
    page_range = str((payload.get("parse") or {}).get("page_range") or "")
    tail = page_range.split("-")[-1].strip()
    return int(tail) if tail.isdigit() else 0


def parse_pdf(settings: MineruSettings, pdf: Path, out_dir: Path) -> MineruOutput:
    """同步阻塞直到解析完成。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - 参数来自配置，不是用户输入
        build_command(settings, pdf, out_dir),
        capture_output=True,
        text=True,
        timeout=settings.timeout_seconds,
        check=False,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        raise MineruError(completed.stderr.strip()[-2000:] or "mineru parse failed")
    payload = _loads_output(completed.stdout)
    status = str((payload.get("parse") or {}).get("status") or "")
    if status and status != "done":
        raise MineruError(f"mineru parse status={status}: {completed.stdout.strip()[-800:]}")
    written = str((payload.get("output") or {}).get("path") or "")
    # -o 给的是文件，output.path 也就是那个文件；产物目录是它的父目录
    produced = Path(written) if written else out_dir
    if produced.suffix:
        produced = produced.parent
    parsed = payload.get("parse") or {}
    return MineruOutput(
        out_dir=produced,
        pages=_pages(payload),
        elapsed_ms=elapsed_ms,
        cache_hit=bool(parsed.get("cache_hit")),
        tier=str(parsed.get("tier") or settings.tier),
    )
