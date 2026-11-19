"""``python -m chatbi.sense <api|sense|reply>``

三个角色分开起进程：api 要常驻、两个 worker 要能被单独重启或横向加几个。
所以这里不搞「一个进程全干」，只做分发。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from chatbi.sense import worker
from chatbi.sense.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chatbi.sense")
    parser.add_argument(
        "role",
        choices=("api", "sense", "reply"),
        help="api=HTTP 入口，sense=判定进程，reply=回应进程",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("SENSE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.role == "api":
        from chatbi.sense import api

        api.main()
        return 0

    root = Path(__file__).resolve().parents[3]
    settings = load_settings(root)
    try:
        asyncio.run(worker.run(args.role, settings))
    except KeyboardInterrupt:  # Ctrl-C 是正常退出，不打印堆栈
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
