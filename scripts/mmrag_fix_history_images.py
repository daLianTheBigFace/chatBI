"""修历史消息里的图片 URL。

修图之前入库的回答，blocks 里的 url 指向 MinerU 的块定位符
（``/static/docs/{doc}/parsed/doc:xxx/page:3/block:1``），文件不存在。
这里按页码把它们对回真实图片：定位符的 page:N 是 1 起，chunks.page_idx 是 0 起。
一页只有一张图时才敢替换，多了就跳过——宁可不显示，也不显示错的。
"""

from __future__ import annotations

import json
import re
import sqlite3

PATTERN = re.compile(r"/parsed/doc:.*?/page:(\d+)/block:")

conn = sqlite3.connect("data/mmrag/rag.db")
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "select id, blocks from messages where role='assistant' and blocks like '%/parsed/doc:%'"
).fetchall()
print("待修消息:", len(rows))

fixed = skipped = 0
for row in rows:
    blocks = json.loads(row["blocks"])
    changed = False
    for block in blocks:
        url = block.get("url") or ""
        match = PATTERN.search(url)
        if not match:
            continue
        doc_id = url.split("/static/docs/")[-1].split("/")[0]
        page_idx = int(match.group(1)) - 1
        images = conn.execute(
            "select image_path from chunks where doc_id=? and kind='image'"
            " and page_idx=? and image_path like '%.png'",
            (doc_id, page_idx),
        ).fetchall()
        if len(images) != 1:
            skipped += 1
            continue
        block["url"] = f"/static/docs/{doc_id}/{images[0]['image_path']}"
        changed = True

    if changed:
        conn.execute(
            "update messages set blocks=? where id=?",
            (json.dumps(blocks, ensure_ascii=False), row["id"]),
        )
        fixed += 1

conn.commit()
print(f"修好 {fixed} 条，跳过 {skipped} 张（对应页不是唯一图）")
conn.close()

