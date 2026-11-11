"""Embedding 向量化服务

- ``RealEmbedding``：通义千问 text-embedding-v3（真实供应商，需 API key）。
- ``DeterministicEmbedding``：内容驱动的确定性替身（哈希 n-gram），同空间余弦相似度
  与内容重叠相关，供自动化验收构造向量/查询，不调用收费模型。
- 向量与内容/模型/维度绑定：维度与 ``INDEX_EMBEDDING_DIMENSIONS`` 不符时构建拒绝，
  不混写不可比较向量。
"""

import hashlib
import logging
import math
from typing import Optional

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingProvider:
    """向量提供者接口：embed_batch 返回与 self.dimensions 一致的向量列表。"""

    model: str = ""
    dimensions: int = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    async def embed_text(self, text: str) -> list[float]:
        rows = await self.embed_batch([text])
        return rows[0]


class DeterministicEmbedding(EmbeddingProvider):
    """内容驱动的确定性替身：字符 n-gram 哈希投影 + 符号累加 + L2 归一化。"""

    model = "fake-deterministic"
    name = "fake-deterministic"

    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    @staticmethod
    def _grams(text: str) -> set[str]:
        t = text.strip().lower()
        grams: set[str] = set()
        for i in range(len(t)):
            grams.add(t[i])
            if i + 1 < len(t):
                grams.add(t[i:i + 2])
        return grams

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for g in self._grams(text):
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dimensions
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]


class RealEmbedding(EmbeddingProvider):
    """真实供应商 embedding（未配置 key 时 embed_batch 返回 None 语义失败）。"""

    name = "dashscope"

    def __init__(self):
        self.model = settings.EMBEDDING_MODEL
        self.dimensions = settings.EMBEDDING_DIMENSIONS
        self.client = (
            AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,
            )
            if settings.OPENAI_API_KEY
            else None
        )

    async def embed_text(self, text: str) -> Optional[list[float]]:
        if not self.client:
            logger.warning("OPENAI_API_KEY 未配置，跳过向量化")
            return None
        try:
            response = await self.client.embeddings.create(model=self.model, input=text)
            return response.data[0].embedding
        except Exception as e:  # noqa: BLE001
            logger.error("向量化失败: %s", e)
            return None

    async def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        if not self.client:
            return [None] * len(texts)
        try:
            response = await self.client.embeddings.create(model=self.model, input=texts)
            sorted_data = sorted(response.data, key=lambda x: x.index)
            return [d.embedding for d in sorted_data]
        except Exception as e:  # noqa: BLE001
            logger.error("批量向量化失败: %s", e)
            return [None] * len(texts)


class ClassroomEmbedding(EmbeddingProvider):
    """Bounded, paid-trial provider; failures never silently become fake vectors."""

    def __init__(self):
        self.model = settings.INDEX_EMBEDDING_MODEL
        self.dimensions = settings.INDEX_EMBEDDING_DIMENSIONS

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        from openai import APIStatusError, APIConnectionError

        from app.services.asr_provider import ProviderError
        from app.services.backoff import parse_retry_after
        from app.services.trial_budget import reserve_call, record_usage
        if not texts:
            return []
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("Embedding凭证未配置")
        if any(not t.strip() or len(t.encode("utf-8")) > 8000 for t in texts):
            raise ValueError("试用embedding单段应为非空且不超过8KB")
        if sum(len(t.encode("utf-8")) for t in texts) > 32000:
            raise ValueError("试用embedding单请求输入不超过32KB")
        # 网关启用时预算由统一网关接管，真实 adapter 为纯 transport，不写旧 SQLite 账本（CA-15/CA-19）。
        request_id = None
        if not settings.CALL_GATEWAY_ENABLED:
            request_id = reserve_call("embedding", self.model)
        try:
            async with AsyncOpenAI(api_key=settings.OPENAI_API_KEY,
                                   base_url=settings.OPENAI_BASE_URL,
                                   max_retries=0, timeout=30) as client:
                response = await client.embeddings.create(
                    model=self.model, input=texts, dimensions=self.dimensions,
                    encoding_format="float")
        except APIStatusError as exc:
            if exc.status_code == 429:
                retry_after = None
                try:
                    retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))
                except Exception:  # noqa: BLE001
                    retry_after = None
                raise ProviderError("rate_limited", "embedding限流", retryable=True,
                                    code="rate_limited", retry_after_seconds=retry_after) from exc
            if exc.status_code >= 500:
                raise ProviderError("provider_error", f"embedding HTTP {exc.status_code}", retryable=True) from exc
            raise ProviderError(
                "provider_error", f"embedding HTTP {exc.status_code}", retryable=False,
                code="invalid_auth" if exc.status_code in (401, 403) else "invalid_request",
            ) from exc
        except APIConnectionError as exc:
            raise ProviderError("connection", "embedding连接失败或超时", retryable=True) from exc
        if response.usage and request_id:
            record_usage(request_id, response.usage.model_dump())
        rows = sorted(response.data, key=lambda r: r.index)
        if [r.index for r in rows] != list(range(len(texts))):
            raise ValueError("Embedding返回数量或顺序非法")
        if any(len(r.embedding) != self.dimensions or
               any(not math.isfinite(x) for x in r.embedding) for r in rows):
            raise ValueError("Embedding返回维度或数值非法")
        return [r.embedding for r in rows]


def get_classroom_embedding() -> EmbeddingProvider:
    if settings.INDEX_EMBEDDING_PROVIDER == "deterministic":
        return DeterministicEmbedding(settings.INDEX_EMBEDDING_DIMENSIONS)
    if settings.INDEX_EMBEDDING_PROVIDER != "real":
        raise ValueError("INDEX_EMBEDDING_PROVIDER必须是real或deterministic")
    return ClassroomEmbedding()


# 全局实例：真实 provider（保留旧知识模块调用兼容）
embedding_service = RealEmbedding()
