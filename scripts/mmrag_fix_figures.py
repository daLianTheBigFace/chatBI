"""给已入库的文档补图：把 MinerU 的块定位符导成 PNG 并改写路径。

新文档在 bridge 里自动做；这个脚本用于修复历史数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chatbi.mmrag.bridge.figures import is_locator, materialize  # noqa: E402
from chatbi.mmrag.config import load_settings  # noqa: E402
from chatbi.mmrag.db import image_chunks, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    settings = load_settings(ROOT)
    conn = init_db(settings.paths.db_file)
    docs = conn.execute(
        "SELECT doc_id, COUNT(*) n FROM chunks WHERE kind='image' GROUP BY doc_id"
    ).fetchall()
    for row in docs:
        doc_id = row["doc_id"]
        pending = [
            c
            for c in image_chunks(conn, doc_id)
            if is_locator(c.image_path)
        ]
        print(f"[{doc_id}] 图片块 {row['n']}，待导出 {len(pending)}", flush=True)
        if not pending:
            continue
        parsed = settings.paths.parsed_dir(doc_id)
        got = materialize(settings, conn, doc_id, parsed)
        print(f"[{doc_id}] 成图 {got}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

