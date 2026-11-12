"""入口：python -m chatbi.mmrag.worker（部署在实例上）。"""

from __future__ import annotations

from pathlib import Path

from chatbi.mmrag.config import load_settings
from chatbi.mmrag.worker.consumer import run


def main() -> None:
    settings = load_settings(Path.cwd())
    run(settings)


if __name__ == "__main__":
    main()
