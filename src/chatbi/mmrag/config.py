"""mmrag 运行时配置。

所有值来自项目根 ``.env``；import 期不做文件 I/O，由调用方显式构造 ``MMRagSettings``。
命名沿用既有约定（``REDIS_*`` / ``KAFKA_*`` / ``OPENAI_*``），新增项见 ``.env.example``。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_KAFKA_SERVERS = "127.0.0.1:9092"
DEFAULT_REDIS_HOST = "127.0.0.1"
DEFAULT_REDIS_PORT = 6379
DEFAULT_VISION_MODEL = "deepseek-flash"
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"
DEFAULT_EMBED_DIM = 1024


@dataclass(frozen=True, slots=True)
class KafkaSettings:
    """Kafka 连接与消费组；key 统一用 doc_id 保证单文档有序。"""

    bootstrap_servers: str = DEFAULT_KAFKA_SERVERS
    worker_group: str = "mmrag-worker"
    request_topic: str = "doc.parse.request"
    result_topic: str = "doc.parse.result"
    dlq_topic: str = "doc.parse.dlq"


@dataclass(frozen=True, slots=True)
class RedisSettings:
    """Redis 只做三件事：幂等锁、任务心跳、SSE 状态广播。"""

    host: str = DEFAULT_REDIS_HOST
    port: int = DEFAULT_REDIS_PORT
    db: int = 0
    password: str | None = None
    # 必须大于 MineruSettings.timeout_seconds（1800），否则解析还没超时锁先掉了，
    # 第二个消费者会重复解析同一份文档。多出的 600 秒是给"判超时 → 清理"留的余量。
    lock_ttl_seconds: int = 2400

    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


@dataclass(frozen=True, slots=True)
class MineruSettings:
    """远端实例上的 MinerU 4.x 客户端调用参数。

    MinerU 4.x 与 2.x 参数不兼容：`parse -p all --tier standard --wait N`，
    默认只解析前 10 页，长文档必须显式 `--pages all`。
    """

    ssh_host: str = "mmrag"
    remote_root: str = "/workspace/software"
    mineru_bin: str = "/workspace/software/mineru-venv/bin/mineru"
    tier: str = "standard"
    pages: str = "all"
    timeout_seconds: int = 1800

    @property
    def remote_out_dir(self) -> Path:
        return Path(self.remote_root) / "data" / "mmrag"


@dataclass(frozen=True, slots=True)
class EmbedSettings:
    """bge-m3 走本机 CPU。

    ``cache_dir`` / ``offline`` 是给国内网络准备的：huggingface.co 直连会
    60 秒超时、重试 5 次（一轮就是 3 分钟），模型早就下好了还被卡住。
    所以缓存目录固定放项目 data 下，装好之后直接 ``offline=True`` 走本地。
    """

    model: str = DEFAULT_EMBED_MODEL
    device: str = "cpu"
    dim: int = DEFAULT_EMBED_DIM
    batch_size: int = 8
    cache_dir: str = ""
    offline: bool = False
    endpoint: str = "https://hf-mirror.com"


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """问答模型；视觉能力决定用户上传的图是直传还是先 caption。"""

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    vision_model: str = DEFAULT_VISION_MODEL
    supports_vision: bool = False
    timeout_seconds: float = 60.0
    top_k: int = 8
    max_images: int = 3


@dataclass(frozen=True, slots=True)
class UploadSettings:
    max_megabytes: int = 100
    allowed_suffixes: tuple[str, ...] = (".pdf",)


@dataclass(frozen=True, slots=True)
class MMRagPaths:
    """磁盘布局：一个文档一个目录，原文件与解析产物分开。"""

    base: Path

    @property
    def db_file(self) -> Path:
        return self.base / "rag.db"

    @property
    def docs_dir(self) -> Path:
        return self.base / "docs"

    @property
    def sessions_dir(self) -> Path:
        return self.base / "sessions"

    @property
    def vector_dir(self) -> Path:
        return self.base / "vector"

    @property
    def vectors_file(self) -> Path:
        return self.vector_dir / "vectors.npy"

    def doc_dir(self, doc_id: str) -> Path:
        return self.docs_dir / doc_id

    def raw_pdf(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "raw.pdf"

    def parsed_dir(self, doc_id: str) -> Path:
        return self.doc_dir(doc_id) / "parsed"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def ensure(self) -> None:
        for path in (self.docs_dir, self.sessions_dir, self.vector_dir):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class MMRagSettings:
    project_root: Path
    paths: MMRagPaths
    kafka: KafkaSettings = field(default_factory=KafkaSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    mineru: MineruSettings = field(default_factory=MineruSettings)
    embed: EmbedSettings = field(default_factory=EmbedSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    upload: UploadSettings = field(default_factory=UploadSettings)


def _env(
    key: str,
    values: Mapping[str, object],
    environ: Mapping[str, str],
    default: str = "",
) -> str:
    """显式环境变量优先于 .env；两者都没有就回默认值。"""
    found = environ.get(key) or values.get(key)
    if found is None:
        return default
    text = str(found).strip()
    return text or default


def _flag(key: str, values: Mapping[str, object], environ: Mapping[str, str]) -> bool:
    return _env(key, values, environ).lower() in {"1", "true", "yes", "on"}


def load_settings(
    project_root: Path, *, environment: Mapping[str, str] | None = None
) -> MMRagSettings:
    values = dotenv_values(project_root / ".env", interpolate=False)
    environ = os.environ if environment is None else environment

    base = Path(_env("MMRAG_DATA_DIR", values, environ, str(project_root / "data" / "mmrag")))
    kafka = KafkaSettings(
        bootstrap_servers=_env("KAFKA_BOOTSTRAP_SERVERS", values, environ, DEFAULT_KAFKA_SERVERS),
        worker_group=_env("MMRAG_KAFKA_GROUP", values, environ, "mmrag-worker"),
    )
    redis = RedisSettings(
        host=_env("REDIS_HOST", values, environ, DEFAULT_REDIS_HOST),
        port=int(_env("REDIS_PORT", values, environ, str(DEFAULT_REDIS_PORT))),
        db=int(_env("REDIS_DB", values, environ, "0")),
        password=_env("REDIS_PASSWORD", values, environ) or None,
    )
    mineru = MineruSettings(
        ssh_host=_env("MMRAG_SSH_HOST", values, environ, "mmrag"),
        tier=_env("MMRAG_MINERU_TIER", values, environ, "standard"),
        pages=_env("MMRAG_MINERU_PAGES", values, environ, "all"),
    )
    embed = EmbedSettings(
        model=_env("MMRAG_EMBED_MODEL", values, environ, DEFAULT_EMBED_MODEL),
        device=_env("MMRAG_EMBED_DEVICE", values, environ, "cpu"),
        batch_size=int(_env("MMRAG_EMBED_BATCH", values, environ, "8")),
        cache_dir=_env("MMRAG_EMBED_CACHE", values, environ, str(base / "hf-cache")),
        offline=_flag("MMRAG_EMBED_OFFLINE", values, environ),
        endpoint=_env("MMRAG_EMBED_ENDPOINT", values, environ, "https://hf-mirror.com"),
    )
    llm = LLMSettings(
        api_key=_env("OPENAI_API_KEY", values, environ),
        base_url=_env("OPENAI_BASE_URL", values, environ, "https://api.openai.com/v1"),
        model=_env("OPENAI_MODEL", values, environ),
        vision_model=_env("OPENAI_VISION_MODEL", values, environ, DEFAULT_VISION_MODEL),
        supports_vision=_flag("MMRAG_LLM_SUPPORTS_VISION", values, environ),
        top_k=int(_env("MMRAG_TOP_K", values, environ, "8")),
        max_images=int(_env("MMRAG_MAX_IMAGES", values, environ, "3")),
    )
    return MMRagSettings(
        project_root=project_root,
        paths=MMRagPaths(base=base),
        kafka=kafka,
        redis=redis,
        mineru=mineru,
        embed=embed,
        llm=llm,
    )
