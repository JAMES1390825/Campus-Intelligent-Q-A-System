from __future__ import annotations

from typing import Any, List, Optional
import logging
import numpy as np
import numpy.typing as npt
from sentence_transformers import SentenceTransformer  # type: ignore[import]
from openai import OpenAI

from .config import Settings, get_settings


logger = logging.getLogger(__name__)
VectorArray = npt.NDArray[np.float32]


class EmbeddingProvider:
    """Abstraction over local ST embeddings and Baidu Qianfan embedding API."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.use_qianfan = bool(getattr(self.settings, "use_qianfan", False))
        self.use_openai_embeddings = bool(getattr(self.settings, "use_openai_embeddings", False)) and bool(
            self.settings.openai_api_key
        )
        self._st_model: Optional[SentenceTransformer] = None
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
            # OpenAI-compatible embeddings (e.g., Qianfan OpenAI compatible endpoint)
            self._openai_client = OpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
            )
        else:
            self._st_model = SentenceTransformer(self.settings.embedding_model)

    def _ensure_local_model(self):
        if self._st_model is None:
            if self.use_qianfan or self.use_openai_embeddings:
                if not getattr(self.settings, "allow_local_fallback", True):
                    raise RuntimeError("Local embedding fallback disabled by configuration")
            model_name = self.settings.embedding_model
            if not model_name:
                raise RuntimeError("No local embedding model configured; set CAMPUS_RAG_EMBEDDING_MODEL")
            try:
                self._st_model = SentenceTransformer(model_name)
            except Exception as exc:
                logger.error("Failed to load embedding model %s without fallback: %s", model_name, exc)
                raise

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

    def _embed_local(self, texts: List[str], model_override: Optional[str] = None) -> VectorArray:
        self._ensure_local_model()
        assert self._st_model is not None
        # model_override is ignored for local ST model; uses initialized embedding_model
        embeddings = self._st_model.encode(  # type: ignore[assignment]
            texts,
            batch_size=self.settings.embedding_batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        arr: VectorArray = np.asarray(embeddings, dtype="float32")
        return arr

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

        if self._qianfan_client:
            try:
                return self._embed_qianfan(texts, model_override=model_override)
            except Exception as exc:  # pragma: no cover - remote API failure
                logger.warning("Qianfan embedding failed, fallback=%s: %s", self.settings.allow_local_fallback, exc)
                if not getattr(self.settings, "allow_local_fallback", True):
                    raise

        if self._openai_client:
            try:
                return self._embed_openai(texts, model_override=model_override)
            except Exception as exc:  # pragma: no cover - remote API failure
                logger.warning("OpenAI-compatible embedding failed, fallback=%s: %s", self.settings.allow_local_fallback, exc)
                if not getattr(self.settings, "allow_local_fallback", True):
                    raise

        return self._embed_local(texts, model_override=model_override)
