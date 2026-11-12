"""入口：python -m chatbi.mmrag.bridge（跑在本机，接 worker 的 result）。"""

from __future__ import annotations

from pathlib import Path

from chatbi.mmrag.bridge.result_consumer import run
from chatbi.mmrag.config import load_settings


def main() -> None:
    settings = load_settings(Path.cwd())
    run(settings)


if __name__ == "__main__":
    main()
