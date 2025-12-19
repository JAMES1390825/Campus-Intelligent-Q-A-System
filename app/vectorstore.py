from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import faiss
import numpy as np

from .config import get_settings
from .models import DocumentChunk, RetrievedChunk
from .embedding_provider import EmbeddingProvider


class FaissVectorStore:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self.embedder = EmbeddingProvider(self.settings)
        self.index_path = Path(self.settings.index_path) / "faiss.index"
        self.meta_path = Path(self.settings.index_path) / "meta.json"
        self.index = None
        self.chunks: List[DocumentChunk] = []

    def _ensure_loaded(self):
        if self.index is not None and self.chunks:
            return
        if not self.index_path.exists() or not self.meta_path.exists():
            raise FileNotFoundError(
                "Index or metadata not found. Run the ingestion script to build the vector index."
            )
        self.index = faiss.read_index(str(self.index_path))
        with open(self.meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.chunks = [DocumentChunk(**item) for item in meta]

    def _embed(self, texts: List[str]) -> np.ndarray:
        return self.embedder.embed(texts)

    def build(self, chunks: List[DocumentChunk]):
        self.chunks = chunks
        embeddings = self._embed([c.text for c in chunks])
        dim = embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dim)
        self.index.add(embeddings)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.index_path))
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump([c.model_dump() for c in chunks], f, ensure_ascii=False, indent=2)

    def search(self, query: str, top_k: int) -> List[RetrievedChunk]:
        self._ensure_loaded()
        q_emb = self._embed([query])
        scores, indices = self.index.search(q_emb, top_k)
        hits = []
        for idx, score in zip(indices[0], scores[0]):
            if idx == -1:
                continue
            chunk = self.chunks[idx]
            hits.append(RetrievedChunk(chunk=chunk, score=float(score)))
        return hits

    def stats(self) -> Tuple[int, int]:
        self._ensure_loaded()
        return len(self.chunks), self.index.ntotal if self.index is not None else 0


class VectorStore:
    """Wrapper that can switch between FAISS and PgVector."""

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        if getattr(self.settings, "use_pgvector", False):
            from .vectorstore_pgvector import PGVectorStore

            self.impl = PGVectorStore(self.settings)
        else:
            self.impl = FaissVectorStore(self.settings)

    def build(self, chunks: List[DocumentChunk]):
        return self.impl.build(chunks)

    def search(self, query: str, top_k: int) -> List[RetrievedChunk]:
        return self.impl.search(query, top_k)

    def stats(self) -> Tuple[int, int]:
        return self.impl.stats()
