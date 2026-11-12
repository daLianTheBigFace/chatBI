"""把 MinerU 的图片块导成真实 PNG。

MinerU 4.x 的 `parse -f markdown` 只写一份 `parsed.md`，里面的图是
`![Image block](doc:bdfaa68/tier:standard/page:3/block:1)` 这种**块定位符**，
不是文件路径。直接塞进 <img src> 当然 404（前端显示裂图）。

所以切块之后要补一步：拿这些定位符回实例执行 `mineru read <locator> -f image`
把图导出来，scp 回本机，再把 chunks.image_path 改写成真实相对路径。
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from chatbi.mmrag.bridge.transfer import TransferError, _posix, remote_out_path
from chatbi.mmrag.config import MineruSettings, MMRagSettings

log = logging.getLogger(__name__)

IMAGE_DIR = "images"


def _run(argv: list[str], *, timeout: int = 600) -> None:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise TransferError(completed.stderr.strip()[-1500:] or f"command failed: {argv[0]}")


def _locator_of(image_path: str) -> str:
    """chunker 存的是 ``parsed/doc:xxx/page:3/block:1``，但 mineru read 只认
    ``doc:xxx/page:3/block:1``（多了 parsed/ 就报 invalid_locator）。"""
    value = image_path.strip()
    return value[len("parsed/") :] if value.startswith("parsed/") else value


def _file_name(chunk_id: int) -> str:
    """chunk id 里带冒号（``732861bf26d9:p2:i24``），Windows 上不能做文件名。"""
    return f"{chunk_id}.png".replace(":", "_")


def export_figures(mineru: MineruSettings, doc_id: str, jobs: list[tuple[int, str]]) -> Path:
    """在实例上把每个定位符导成 PNG，再 scp 回本机。返回本地图片目录。"""
    remote_out = remote_out_path(mineru, doc_id)
    remote_images = f"{_posix(remote_out)}/{IMAGE_DIR}"
    lines = [f"mkdir -p {shlex.quote(remote_images)}"]
    for chunk_id, locator in jobs:
        target = f"{remote_images}/{_file_name(chunk_id)}"
        lines.append(
            f"{mineru.mineru_bin} read {shlex.quote(_locator_of(locator))} -f image "
            f"-o {shlex.quote(target)} --json >/dev/null 2>&1 || true"
        )
    script = " && ".join(lines)
    _run(["ssh", mineru.ssh_host, script], timeout=900)
    return Path(remote_images)


def pull_images(mineru: MineruSettings, doc_id: str, local_parsed: Path) -> int:
    """把实例上的 images/ 拉回 data/mmrag/docs/{doc_id}/parsed/images/。"""
    remote_dir = f"{_posix(remote_out_path(mineru, doc_id))}/{IMAGE_DIR}"
    local_dir = local_parsed / IMAGE_DIR
    local_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "scp",
            "-rq",
            f"{mineru.ssh_host}:{remote_dir}/.",
            str(local_dir).replace("\\", "/"),
        ]
    )
    return len([p for p in local_dir.iterdir() if p.is_file()])


def is_locator(image_path: str | None) -> bool:
    """MinerU 的块定位符长这样：``parsed/doc:bdfaa68/tier:standard/page:3/block:1``。

    真正的图片路径都以 .png 结尾，按后缀区分，别猜前缀（chunker 会加 ``parsed/``）。
    """
    value = (image_path or "").strip()
    return "doc:" in value and not value.lower().endswith((".png", ".jpg", ".jpeg"))


def materialize(settings: MMRagSettings, conn, doc_id: str, local_parsed: Path) -> int:
    """完整流程：查定位符 → 实例导出 → 拉回 → 改写 image_path。返回成图数量。"""
    from chatbi.mmrag.db import image_chunks, set_image_paths

    image_dir = local_parsed / IMAGE_DIR
    targets = [
        (chunk.id, (chunk.image_path or "").strip())
        for chunk in image_chunks(conn, doc_id)
        if is_locator(chunk.image_path)
    ]
    missing = [(cid, loc) for cid, loc in targets if not (image_dir / _file_name(cid)).exists()]

    if missing:
        try:
            export_figures(settings.mineru, doc_id, missing)
            pull_images(settings.mineru, doc_id, local_parsed)
        except (TransferError, subprocess.SubprocessError) as exc:
            # 图导不出来不该让整篇文档失败：文字和表格已经能用了
            log.warning("figures failed doc=%s: %s", doc_id, exc)

    # 注意顺序：(新路径, chunk_id)，和 db.set_image_paths 的 SQL 对齐
    pairs = [
        (f"parsed/{IMAGE_DIR}/{_file_name(cid)}", cid)
        for cid, _ in targets
        if (image_dir / _file_name(cid)).exists()
    ]
    if pairs:
        set_image_paths(conn, pairs)
    log.info("figures doc=%s targets=%d rewritten=%d", doc_id, len(targets), len(pairs))
    return len(pairs)

