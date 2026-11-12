"""实例侧的解析台账：一行一条 JSON，追加写，按天分文件。

为什么单独写一份：worker.log 混着 Kafka 的 INFO 噪声，脚本想统计
"这个月解析了多少页、多少命中缓存、平均耗时" 很难抠。JSONL 直接
`pandas.read_json(lines=True)` 就能出表。

位置：``{MMRAG_DATA_DIR}/logs/parse-YYYYMMDD.jsonl``，跟产物同盘，
实例释放一起消失（本来就是临时机，长期统计在本机 rag.db 里有）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chatbi.mmrag.config import MMRagSettings

log = logging.getLogger(__name__)


def log_file(settings: MMRagSettings, *, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d")
    return settings.paths.base / "logs" / f"parse-{stamp}.jsonl"


def record(settings: MMRagSettings, **fields: Any) -> Path:
    """追加一行；写失败只警告，绝不让台账影响解析结果。"""
    path = log_file(settings)
    line = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("audit write failed: %s (%s)", path, exc)
    return path
