from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple, Optional
import logging

import numpy as np
import numpy.typing as npt
import psycopg2
from psycopg2 import errors
from pgvector.psycopg2 import register_vector  # type: ignore[import]
from psycopg2.extras import execute_values  # type: ignore[import-untyped]

from .config import get_settings, Settings
from .models import DocumentChunk, RetrievedChunk
from .embedding_provider import EmbeddingProvider


logger = logging.getLogger(__name__)


VectorArray = npt.NDArray[np.float32]


class PGVectorStore:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        if not self.settings.pg_dsn:
            raise RuntimeError("PgVector enabled but CAMPUS_RAG_PG_DSN not configured")
        self.embedder = EmbeddingProvider(self.settings)
        self.table = self.settings.pg_table

    def _connect(self):
        conn = psycopg2.connect(self.settings.pg_dsn)
        register_vector(conn)
        return conn

    def _ensure_schema(self, dim: int):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT,
                    source TEXT,
                    source_type TEXT,
                    metadata JSONB,
                    content TEXT,
                    embedding vector({dim})
                )
                """
            )
            conn.commit()

    def _clear_table(self):
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {self.table}")
                conn.commit()
        except errors.UndefinedTable:
            logger.warning("PgVector table '%s' missing during clear", self.table)

    def _embed(self, texts: List[str]) -> VectorArray:
        return self.embedder.embed(texts)

    def build(self, chunks: List[DocumentChunk]):
        if not chunks:
            self._clear_table()
            return
        embeddings: VectorArray = self._embed([c.text for c in chunks])
        dim = embeddings.shape[1]
        self._ensure_schema(dim)

        rows = []
        for ch, emb in zip(chunks, embeddings):
            rows.append(
                (
                    ch.id,
                    ch.source,
                    ch.source,
                    ch.source_type,
                    json.dumps(ch.metadata or {}),
                    ch.text,
                    emb.tolist(),
                )
            )

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {self.table}")
            execute_values(
                cur,
                f"""
                INSERT INTO {self.table}
                (chunk_id, document_id, source, source_type, metadata, content, embedding)
                VALUES %s
                ON CONFLICT (chunk_id) DO UPDATE SET
                    document_id=EXCLUDED.document_id,
                    source=EXCLUDED.source,
                    source_type=EXCLUDED.source_type,
                    metadata=EXCLUDED.metadata,
                    content=EXCLUDED.content,
                    embedding=EXCLUDED.embedding
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s)"
            )
            conn.commit()

    def delete_documents(self, doc_names: List[str]) -> int:
        targets = [name for name in doc_names if name]
        if not targets:
            return 0
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.table} WHERE document_id = ANY(%s)", (targets,))
                deleted = cur.rowcount or 0
                conn.commit()
                return int(deleted)
        except errors.UndefinedTable:
            logger.warning("PgVector table '%s' missing during delete", self.table)
            return 0

    def upsert_documents(self, chunk_map: Dict[str, List[DocumentChunk]]):
        ordered: List[DocumentChunk] = []
        targets: List[str] = []
        for doc_name, chunks in chunk_map.items():
            if not doc_name or not chunks:
                continue
            targets.append(doc_name)
            ordered.extend(chunks)
        if not ordered:
            if targets:
                self.delete_documents(targets)
            return
        embeddings: VectorArray = self._embed([chunk.text for chunk in ordered])
        dim = embeddings.shape[1]
        self._ensure_schema(dim)
        rows = []
        for chunk, emb in zip(ordered, embeddings):
            rows.append(
                (
                    chunk.id,
                    chunk.source,
                    chunk.source,
                    chunk.source_type,
                    json.dumps(chunk.metadata or {}),
                    chunk.text,
                    emb.tolist(),
                )
            )

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.table} WHERE document_id = ANY(%s)", (targets,))
            execute_values(
                cur,
                f"""
                INSERT INTO {self.table}
                (chunk_id, document_id, source, source_type, metadata, content, embedding)
                VALUES %s
                ON CONFLICT (chunk_id) DO UPDATE SET
                    document_id=EXCLUDED.document_id,
                    source=EXCLUDED.source,
                    source_type=EXCLUDED.source_type,
                    metadata=EXCLUDED.metadata,
                    content=EXCLUDED.content,
                    embedding=EXCLUDED.embedding
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s)"
            )
            conn.commit()

    def search(self, query: str, top_k: int) -> List[RetrievedChunk]:
        q_emb = self._embed([query])[0].tolist()
        self._ensure_schema(len(q_emb))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT chunk_id, document_id, source, source_type, metadata, content,
                       (embedding <=> %s::vector)::float AS distance
                FROM {self.table}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (q_emb, q_emb, top_k),
            )
            rows = cur.fetchall()

        hits: List[RetrievedChunk] = []
        for row in rows:
            chunk_id, document_id, source, source_type, metadata, content, distance = row
            meta_obj: dict[str, Any]
            meta_obj = metadata or {}
            if isinstance(meta_obj, str):
                try:
                    meta_obj = json.loads(meta_obj)
                except Exception:
                    meta_obj = {"raw_metadata": meta_obj}
            chunk = DocumentChunk(
                id=chunk_id,
                text=content,
                source=source or document_id,
                source_type=source_type or "file",
                url=None,
                metadata=meta_obj,
            )
            score = float(1.0 - distance)  # cosine 距离越小越好，转为相似度
            hits.append(RetrievedChunk(chunk=chunk, score=score))
        return hits

    def stats(self) -> Tuple[int, int]:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self.table}")
                row = cur.fetchone()
                count = row[0] if row else 0
            total = int(count)
            return total, total
        except errors.UndefinedTable:
            logger.warning("PgVector table '%s' missing; returning empty stats", self.table)
            return 0, 0
