"""scp / ssh 封装。

约定（与远端 worker 一致）：
    远端入料  {remote_root}/data/mmrag/in/{doc_id}.pdf
    远端产物  {remote_root}/data/mmrag/out/{doc_id}/
    本机产物  {base}/docs/{doc_id}/parsed/
"""

from __future__ import annotations

import posixpath
import shlex
import subprocess
from pathlib import Path

from chatbi.mmrag.config import MineruSettings


class TransferError(RuntimeError):
    pass


def _posix(path: Path | str) -> str:
    """远端路径一律 POSIX：本机是 Windows 时 ``Path('/a/b')`` 会变成 ``\\a\\b``。"""
    return str(path).replace("\\", "/")


def _run(argv: list[str], *, timeout: int = 600) -> None:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise TransferError(completed.stderr.strip()[-2000:] or f"command failed: {argv[0]}")


def remote_in_path(settings: MineruSettings, doc_id: str) -> str:
    return posixpath.join(_posix(settings.remote_out_dir), "in", f"{doc_id}.pdf")


def remote_out_path(settings: MineruSettings, doc_id: str) -> str:
    return posixpath.join(_posix(settings.remote_out_dir), "out", doc_id)


def push_pdf(settings: MineruSettings, doc_id: str, local_pdf: Path) -> str:
    """上传原文件；返回远端路径（写进 ParseRequest.raw_path）。"""
    target = remote_in_path(settings, doc_id)
    _run(["ssh", settings.ssh_host, f"mkdir -p {shlex.quote(posixpath.dirname(target))}"])
    _run(["scp", str(local_pdf), f"{settings.ssh_host}:{target}"])
    return target


def pull_parsed(settings: MineruSettings, doc_id: str, local_parsed: Path) -> int:
    """把整个产物目录拉回本机，返回拉回的文件数。"""
    local_parsed.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "scp",
            "-r",
            f"{settings.ssh_host}:{remote_out_path(settings, doc_id)}/.",
            str(local_parsed),
        ],
        timeout=1800,
    )
    return sum(1 for path in local_parsed.rglob("*") if path.is_file())
