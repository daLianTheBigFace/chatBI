"""扫一遍「会被提交的内容」里有没有机密。

只报高置信度的东西（真密钥形状），不做「含 password 字样」这种模糊匹配——
否则 config.py 里 `password: str | None` 这种字段全会被标红，扫两次就没人看了。

用法：
    python scripts/check_secrets.py            # 扫工作区里未被忽略的文件
    python scripts/check_secrets.py --staged   # 只扫暂存区（给 pre-commit 用）
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# (名称, 正则)：只放"一旦命中基本就是事故"的形状
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/DeepSeek 密钥", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("私钥", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("数据库地址带密码", re.compile(r"\b(?:postgres|mysql|redis)://[^\s:/]+:[^\s@]+@")),
]

# 这些文件本来就可能写满配置样例，允许出现占位符但依然扫真密钥，
# 只是把明显是占位的内容放过。
ALLOW_EMPTY = re.compile(r"=\s*$|=\s*<.*>\s*$|=\s*\$\{|=\s*xxx+", re.IGNORECASE)

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".sh", ".ps1", ".example", ".env",
}


def _tracked_or_staged(staged: bool) -> list[str]:
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描提交内容里的机密")
    parser.add_argument("--staged", action="store_true", help="只扫暂存区")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    problems: list[str] = []

    for rel in _tracked_or_staged(args.staged):
        path = root / rel
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ALLOW_EMPTY.search(line) and "PRIVATE KEY" not in line:
                continue
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    problems.append(f"{rel}:{lineno}  [{name}]  {line.strip()[:90]}")

    if not problems:
        print("没有发现密钥形状的内容")
        return 0

    print(f"发现 {len(problems)} 处可疑内容：\n")
    for item in problems:
        print("  " + item)
    print("\n机密只写 .env（已在 .gitignore 里），不要进代码或文档。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
