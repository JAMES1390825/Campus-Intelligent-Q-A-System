from __future__ import annotations

from typing import Dict, List, Tuple

from .config import get_settings
from .models import DocumentChunk, RetrievedChunk


class VectorStore:
    """PgVector-backed store (database-only)."""

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        from .vectorstore_pgvector import PGVectorStore

        if not self.settings.pg_dsn:
            raise RuntimeError("PgVector backend requires CAMPUS_RAG_PG_DSN")
        self.impl = PGVectorStore(self.settings)

    def build(self, chunks: List[DocumentChunk]):
        return self.impl.build(chunks)

    def upsert_documents(self, chunk_map: Dict[str, List[DocumentChunk]]):
        return self.impl.upsert_documents(chunk_map)

    def delete_documents(self, doc_names: List[str]) -> int:
        return self.impl.delete_documents(doc_names)

    def search(self, query: str, top_k: int) -> List[RetrievedChunk]:
        return self.impl.search(query, top_k)

    def stats(self) -> Tuple[int, int]:
        return self.impl.stats()
