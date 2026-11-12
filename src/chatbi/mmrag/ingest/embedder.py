"""文本向量化。

两个后端，按可用性自动选：

- ``sentence-transformers`` + bge-m3（1024 维，本机 3080Ti 上 fp16）——正式方案；
- ``hash``：字符 n-gram 哈希到固定维度，无依赖、毫秒级，**只用来把链路跑通**。
  它没有语义能力，检索质量不能当真，装上 sentence-transformers 后自动切走。

为什么必须在本机算：DeepSeek 官方 API 不提供 embeddings 接口，而切块也发生在本机。
"""

from __future__ import annotations

import hashlib
import logging
import os

import numpy as np

from chatbi.mmrag.config import EmbedSettings

log = logging.getLogger(__name__)


class Embedder:
    """懒加载：第一次 encode 才把模型读进显存，进程启动不受影响。"""

    def __init__(self, settings: EmbedSettings) -> None:
        self._settings = settings
        self._model = None
        self._backend: str | None = None
        self._dim = settings.dim

    @property
    def backend(self) -> str:
        if self._backend is None:
            self._load()
        assert self._backend is not None
        return self._backend

    @property
    def dim(self) -> int:
        if self._backend is None:
            self._load()
        return self._dim

    def warmup(self) -> None:
        """提前把权重读进来。

        纯 CPU 加载 bge-m3 要 13 秒左右（391 个权重文件）；这个时间如果落在
        第一个用户身上，表现为「问一句要等半分钟」。进程起来时先在后台跑一次。
        """
        try:
            self.encode(["warmup"])
            log.info("embedder warmed up on %s", self._device_label())
        except Exception:  # noqa: BLE001 - 预热失败不该影响服务启动
            log.warning("embedder warmup failed", exc_info=True)

    def _device_label(self) -> str:
        return self._settings.device

    def _load(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            self._backend = "hash"
            log.warning("sentence-transformers 未安装，退到 hash 向量（无语义能力，仅供联调）")
            return

        settings = self._settings
        # huggingface.co 在国内常常 60 秒超时再重试 5 次，一次索引能卡三分钟；
        # 模型已经下好的机器上直接 offline，不再发任何 HEAD 请求。
        os.environ.setdefault("HF_ENDPOINT", settings.endpoint)
        if settings.offline:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")

        device = settings.device
        kwargs: dict[str, object] = {"device": device}
        if settings.cache_dir:
            kwargs["cache_folder"] = settings.cache_dir
        if settings.offline:
            kwargs["local_files_only"] = True
        self._model = SentenceTransformer(settings.model, **kwargs)  # type: ignore[arg-type]
        probe = getattr(self._model, "get_embedding_dimension", None)
        if probe is None:
            probe = self._model.get_sentence_embedding_dimension
        self._dim = int(probe())
        self._backend = "sentence-transformers"
        log.info("embedder ready: %s on %s (dim=%d)", settings.model, device, self._dim)

    def encode(self, texts: list[str]) -> np.ndarray:
        """返回 L2 归一化后的 float32 矩阵；归一化后点积即余弦相似度。"""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        if self.backend == "sentence-transformers":
            vectors = self._model.encode(  # type: ignore[union-attr]
                texts,
                batch_size=self._settings.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return np.asarray(vectors, dtype=np.float32)

        return np.vstack([self._hash_one(text) for text in texts])

    def _hash_one(self, text: str) -> np.ndarray:
        """字符 3-gram 哈希 + L2 归一化；确定性、无依赖，只为让链路能跑。"""
        vector = np.zeros(self._dim, dtype=np.float32)
        cleaned = " ".join(text.split())
        if not cleaned:
            return vector
        for size in (2, 3):
            for start in range(max(1, len(cleaned) - size + 1)):
                gram = cleaned[start : start + size]
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self._dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector
