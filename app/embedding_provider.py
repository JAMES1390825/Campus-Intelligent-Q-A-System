from __future__ import annotations

from typing import Any, List, Optional
import logging
import time
import numpy as np
import numpy.typing as npt
from openai import OpenAI
try:  # pragma: no cover - optional import guard for rate limit type
    from openai import RateLimitError
except Exception:  # pragma: no cover
    class RateLimitError(Exception):
        """Fallback RateLimitError placeholder when SDK doesn't expose it."""


from .config import Settings, get_settings


logger = logging.getLogger(__name__)
VectorArray = npt.NDArray[np.float32]


class EmbeddingProvider:
    """Abstraction over Qianfan/OpenAI-compatible embedding APIs (no local fallback)."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.use_qianfan = bool(getattr(self.settings, "use_qianfan", False))
        self.use_openai_embeddings = bool(getattr(self.settings, "use_openai_embeddings", False)) and bool(
            self.settings.openai_api_key
        )
        self._qianfan_client: Any = None
        self._openai_client: Optional[OpenAI] = None

        if self.use_qianfan:
            try:
                from qianfan import Embedding  # type: ignore
            except Exception as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "Qianfan embeddings requested but qianfan package is not installed"
                ) from exc
            if not (self.settings.qianfan_access_key and self.settings.qianfan_secret_key):
                raise RuntimeError("Qianfan enabled but CAMPUS_RAG_QIANFAN_ACCESS_KEY/SECRET_KEY not set")
            self._qianfan_client = Embedding(
                ak=self.settings.qianfan_access_key,
                sk=self.settings.qianfan_secret_key,
            )
        elif self.use_openai_embeddings:
            # OpenAI-compatible embeddings (e.g., DeepSeek/Sealos endpoint)
            self._openai_client = OpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
            )
        else:
            raise RuntimeError(
                "No external embedding provider configured; set CAMPUS_RAG_OPENAI_API_KEY or enable Qianfan embeddings"
            )

    def _embed_qianfan(self, texts: List[str], model_override: Optional[str] = None) -> VectorArray:
        assert self._qianfan_client is not None
        resp: dict[str, Any] = self._qianfan_client.do(
            model=model_override or self.settings.qianfan_embedding_model,
            input=texts,
        )
        error_code = resp.get("error_code")
        if error_code:
            error_msg = resp.get("error_msg") or resp.get("error_message") or "Unknown error"
            raise RuntimeError(f"Qianfan embedding error {error_code}: {error_msg}")
        data: List[dict[str, Any]] = resp.get("data") or []
        if len(data) != len(texts):
            raise RuntimeError("Qianfan embedding returned unexpected result length")
        vecs = [item["embedding"] for item in data]
        arr: VectorArray = np.asarray(vecs, dtype="float32")
        # Normalize to cosine space, avoid division by zero
        norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
        return arr / norms

    def _embed_openai(self, texts: List[str], model_override: Optional[str] = None) -> VectorArray:
        assert self._openai_client is not None
        resp = self._openai_client.embeddings.create(
            model=model_override or self.settings.embedding_model,
            input=texts,
        )
        vecs = [item.embedding for item in resp.data]
        arr: VectorArray = np.asarray(vecs, dtype="float32")
        norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
        return arr / norms

    def embed(self, texts: List[str], model_override: Optional[str] = None) -> VectorArray:
        if not texts:
            return np.zeros((0, 0), dtype="float32")
        provider = None
        if self._qianfan_client:
            provider = self._embed_qianfan
        elif self._openai_client:
            provider = self._embed_openai
        else:
            raise RuntimeError("Embedding provider not initialized; configure Qianfan or OpenAI-compatible embeddings")

        batch_size = max(1, int(self.settings.embedding_batch_size or 1))
        max_retries = max(1, int(self.settings.embedding_max_retries or 1))
        base_delay = max(0.1, float(self.settings.embedding_retry_base_delay or 0.1))
        max_delay = max(base_delay, float(self.settings.embedding_retry_max_delay or base_delay))

        batches: List[VectorArray] = []
        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]
            attempt = 0
            delay = base_delay
            while True:
                try:
                    vecs = provider(chunk, model_override=model_override)
                    batches.append(vecs)
                    break
                except Exception as exc:
                    attempt += 1
                    if attempt >= max_retries or not self._is_retryable_error(exc):
                        raise
                    logger.warning(
                        "Embedding request throttled (attempt %s/%s); retrying in %.2fs",
                        attempt,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, max_delay)

        if not batches:
            return np.zeros((0, 0), dtype="float32")
        return np.vstack(batches)

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        if isinstance(exc, RateLimitError):
            return True
        msg = str(exc).lower()
        keywords = ("429", "rate limit", "too many requests", "tpm", "rpm")
        return any(token in msg for token in keywords)
