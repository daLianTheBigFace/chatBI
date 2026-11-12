"""Redis 最小测试：set 一个、get 一个。

配置从项目根目录的 .env 读取，命令行参数可临时覆盖：
    REDIS_HOST / REDIS_PORT / REDIS_DB / REDIS_PASSWORD

用法：
    uv run python scripts/redis_smoke.py
    uv run python scripts/redis_smoke.py --password xxx
"""

import argparse
import os
from pathlib import Path

import redis
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.getenv("REDIS_HOST", "127.0.0.1"))
ap.add_argument("--port", type=int, default=int(os.getenv("REDIS_PORT", "6379")))
ap.add_argument("--db", type=int, default=int(os.getenv("REDIS_DB", "0")))
ap.add_argument("--password", default=os.getenv("REDIS_PASSWORD") or None)
args = ap.parse_args()

r = redis.Redis(
    host=args.host,
    port=args.port,
    db=args.db,
    password=args.password,
    decode_responses=True,
    socket_timeout=5,
)

print(f"target: {args.host}:{args.port}/{args.db}")
print("ping:", r.ping())

r.set("test:key", "hello")
print("set  : test:key = hello")
print("get  :", r.get("test:key"))

r.delete("test:key")
