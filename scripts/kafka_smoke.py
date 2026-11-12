"""Kafka 最小测试：生产一条、消费一条。

配置从项目根目录的 .env 读取：
    KAFKA_BOOTSTRAP_SERVERS / KAFKA_TOPIC

前提：本地 9092 已通过 SSH 隧道转发到实例。
用法：
    uv run python scripts/kafka_smoke.py
    uv run python scripts/kafka_smoke.py --topic doc.parse.request
"""

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from kafka import KafkaConsumer, KafkaProducer

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

ap = argparse.ArgumentParser()
ap.add_argument("--bootstrap", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092"))
ap.add_argument("--topic", default=os.getenv("KAFKA_TOPIC", "doc.parse.request"))
args = ap.parse_args()

bootstrap = [s.strip() for s in args.bootstrap.split(",") if s.strip()]
print(f"target: {bootstrap} topic={args.topic}")

producer = KafkaProducer(
    bootstrap_servers=bootstrap,
    value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
    key_serializer=lambda k: k.encode("utf-8") if k else None,
    request_timeout_ms=10000,
    acks="all",
)

doc_id = f"d_{uuid.uuid4().hex[:8]}"
msg = {"doc_id": doc_id, "version": 1, "ts": int(time.time())}

meta = producer.send(args.topic, key=doc_id, value=msg).get(timeout=10)
producer.flush()
print(f"send : partition={meta.partition} offset={meta.offset} {msg}")

consumer = KafkaConsumer(
    args.topic,
    bootstrap_servers=bootstrap,
    auto_offset_reset="earliest",
    consumer_timeout_ms=15000,
    group_id=None,
    request_timeout_ms=10000,
)

for record in consumer:
    print(f"recv : partition={record.partition} offset={record.offset}")
    print(f"       {record.value.decode('utf-8')}")
    break
else:
    print("recv : (timeout, nothing consumed)")

consumer.close()

