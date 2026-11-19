"""流水线的配置。两个 runtime 读同一份，保证对比时变量只有「谁在跑」。

环境变量一律带 ``SENSE_`` 前缀，避免和 chatbi 现有配置打架。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_MODEL = "deepseek-v4-pro"


@dataclass(frozen=True, slots=True)
class LLMSettings:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = DEFAULT_MODEL
    temperature: float = 0.4
    max_tokens: int = 800


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    sense_timeout_seconds: float = 10.0
    reply_timeout_seconds: float = 45.0
    request_timeout_seconds: float = 60.0
    max_attempts: int = 3
    history_turns: int = 6
    history_char_limit: int = 24000
    single_message_char_limit: int = 8000
    session_lock_seconds: int = 30


@dataclass(frozen=True, slots=True)
class RedisSettings:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str | None = None
    sense_stream: str = "stream:sense"
    reply_stream: str = "stream:reply"
    dead_stream: str = "stream:dead"
    sense_group: str = "sense-workers"
    reply_group: str = "reply-workers"
    max_len: int = 10000
    claim_idle_ms: int = 60000

    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"

    def events_channel(self, request_id: str) -> str:
        return f"sense:events:{request_id}"

    def lock_key(self, session_id: str) -> str:
        return f"lock:sess:{session_id}"

    def done_key(self, stage: str, request_id: str) -> str:
        return f"{stage}:done:{request_id}"


@dataclass(frozen=True, slots=True)
class SenseSettings:
    project_root: Path
    llm: LLMSettings = field(default_factory=LLMSettings)
    pipeline: PipelineSettings = field(default_factory=PipelineSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    runtime: str = "agents"  # agents | graph

    @property
    def db_path(self) -> Path:
        return self.project_root / "data" / "sense" / "sense.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.project_root / "data" / "sense" / "checkpoints.sqlite"


def load_settings(
    project_root: Path, *, environment: dict[str, str] | None = None
) -> SenseSettings:
    """读 `.env` 再让环境变量覆盖——和 mmrag 那边保持一致，别两套规矩。"""
    dotenv_path = project_root / ".env"
    values: dict[str, str] = {}
    if dotenv_path.exists():
        values = {
            key: value for key, value in dotenv_values(dotenv_path).items() if value is not None
        }
    override = os.environ if environment is None else environment

    def get(key: str, default: str = "") -> str:
        return override.get(key) or values.get(key) or default

    llm = LLMSettings(
        api_key=get("OPENAI_API_KEY"),
        base_url=get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        model=get("SENSE_MODEL", get("OPENAI_MODEL", DEFAULT_MODEL)),
        temperature=float(get("SENSE_TEMPERATURE", "0.4")),
    )
    pipeline = PipelineSettings(
        sense_timeout_seconds=float(get("SENSE_JUDGE_TIMEOUT", "10")),
        reply_timeout_seconds=float(get("SENSE_REPLY_TIMEOUT", "45")),
        max_attempts=int(get("SENSE_MAX_ATTEMPTS", "3")),
    )
    redis = RedisSettings(
        host=get("REDIS_HOST", "127.0.0.1"),
        port=int(get("REDIS_PORT", "6379")),
        db=int(get("REDIS_DB", "0")),
        password=get("REDIS_PASSWORD") or None,
    )
    return SenseSettings(
        project_root=project_root,
        llm=llm,
        pipeline=pipeline,
        redis=redis,
        runtime=get("SENSE_RUNTIME", "agents"),
    )
