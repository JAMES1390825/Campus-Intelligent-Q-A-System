# pyright: reportMissingImports=false, reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import List, Optional, Iterable, Any, Dict
import logging

import pypdf  # type: ignore
import docx  # type: ignore
import openpyxl  # type: ignore

from .config import get_settings, Settings

import numpy as np
import httpx
from openai import OpenAI
import qianfan
from .models import DocumentChunk, RetrievedChunk, SourceAttribution
from .prompts import ANSWER_PROMPT
from .vectorstore import VectorStore
from .embedding_provider import EmbeddingProvider


ALLOWED_DOC_EXTS = {".txt", ".md", ".pdf", ".docx", ".xlsx"}


class QianfanReranker:
    """Use Baidu Qianfan's native rerank API instead of embedding similarity hacks."""

    SCORE_KEYS = ("relevance_score", "score", "similarity", "rerank_score", "probability")
    INDEX_KEYS = ("index", "position", "document_index", "doc_index", "order")

    def __init__(self, settings: Settings, model_name: str):
        if not (settings.qianfan_access_key and settings.qianfan_secret_key):
            raise RuntimeError("Qianfan reranker requires CAMPUS_RAG_QIANFAN_ACCESS_KEY/SECRET_KEY")
        if not getattr(qianfan, "Reranker", None):
            raise RuntimeError("qianfan package does not expose Reranker API")
        self.settings = settings
        self.model_name = model_name
        self._client = qianfan.Reranker(
            ak=settings.qianfan_access_key,
            sk=settings.qianfan_secret_key,
        )

    def rerank(self, query: str, hits: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
        if not hits:
            return []

        candidate_limit = max(top_k or self.settings.top_k, self.settings.rerank_top_n)
        candidates = hits[:candidate_limit]
        documents = [h.chunk.text for h in candidates]
        try:
            resp = self._client.do(
                query=query,
                documents=documents,
                top_n=min(top_k or len(candidates), len(candidates)),
                model=self.model_name,
            )
        except Exception as exc:  # pragma: no cover - remote API failure
            logger.warning("Qianfan reranker request failed: %s", exc)
            raise

        entries = self._extract_entries(resp)
        if not entries:
            logger.warning("Qianfan reranker returned empty result; falling back to original ranking")
            return candidates[:top_k]

        scored: List[RetrievedChunk] = []
        used: set[int] = set()
        for entry in entries:
            idx = self._resolve_index(entry, documents)
            if idx is None or not 0 <= idx < len(candidates):
                continue
            if idx in used:
                continue
            score = self._resolve_score(entry, default=candidates[idx].score)
            scored.append(RetrievedChunk(chunk=candidates[idx].chunk, score=score))
            used.add(idx)
            if len(scored) >= top_k:
                break

        if len(scored) < min(top_k, len(candidates)):
            for i, item in enumerate(candidates):
                if len(scored) >= top_k:
                    break
                if i in used:
                    continue
                scored.append(RetrievedChunk(chunk=item.chunk, score=item.score))

        return scored

    def _extract_entries(self, resp: Any) -> List[Dict[str, Any]]:
        if resp is None:
            return []
        body: Dict[str, Any]
        if hasattr(resp, "body") and isinstance(resp.body, dict):
            body = resp.body
        elif isinstance(resp, dict):
            body = resp
        else:
            return []

        candidates = self._first_list(body, ("result", "results", "data"))
        if isinstance(candidates, dict):
            candidates = self._first_list(candidates, ("documents", "items", "data"))
        if isinstance(candidates, list):
            return candidates
        for key in ("documents", "items", "data"):
            val = body.get(key)
            if isinstance(val, list):
                return val
        return []

    @staticmethod
    def _first_list(payload: Dict[str, Any], keys: Iterable[str]) -> Any:
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list) or isinstance(val, dict):
                return val
        return None

    def _resolve_index(self, entry: Dict[str, Any], documents: List[str]) -> Optional[int]:
        for key in self.INDEX_KEYS:
            value = entry.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        doc_text = entry.get("document") or entry.get("text") or entry.get("content")
        if isinstance(doc_text, str):
            try:
                return documents.index(doc_text)
            except ValueError:
                return None
        return None

    def _resolve_score(self, entry: Dict[str, Any], default: float) -> float:
        for key in self.SCORE_KEYS:
            value = entry.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return float(default)
class _APIReranker:
    """API-based reranker using embedding similarity (no local HF download).

    Uses the configured EmbeddingProvider (Qianfan/OpenAI-compatible or local ST) and
    optionally a model name override (settings.reranker_model) to compute cosine scores.
    """

    def __init__(self, embedder: EmbeddingProvider, model_name: Optional[str] = None):
        self.embedder = embedder
        self.model_name = model_name

    def rerank(self, query: str, hits: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
        if not hits:
            return []
        # Limit to rerank_top_n candidates for efficiency
        candidates = hits[: top_k * 2] if top_k else hits
        texts = [h.chunk.text for h in candidates]
        q_vec = self.embedder.embed([query], model_override=self.model_name)[0]
        doc_vecs = self.embedder.embed(texts, model_override=self.model_name)
        scores = np.matmul(doc_vecs, q_vec)
        reranked = sorted(zip(candidates, scores), key=lambda x: float(x[1]), reverse=True)[:top_k]
        return [RetrievedChunk(chunk=h.chunk, score=float(s)) for h, s in reranked]


class OpenAICompatibleReranker:
    """Calls Qianfan's OpenAI-compatible /rerank endpoint and emits relevance scores."""

    SCORE_KEYS = ("relevance_score", "score", "similarity")

    def __init__(self, settings: Settings, model_name: str, timeout: float = 20.0):
        if not settings.openai_api_key or not settings.openai_base_url:
            raise RuntimeError("OpenAI-compatible reranker requires CAMPUS_RAG_OPENAI_API_KEY and BASE_URL")
        self.settings = settings
        self.model_name = model_name
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.timeout = timeout

    def rerank(self, query: str, hits: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
        if not hits:
            return []

        candidate_limit = max(top_k or self.settings.top_k, self.settings.rerank_top_n)
        candidates = hits[:candidate_limit]
        documents = [h.chunk.text for h in candidates]
        payload = {
            "model": self.model_name,
            "query": query,
            "documents": documents,
            "top_n": min(top_k or len(candidates), len(candidates)),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/rerank"
        resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        entries = self._extract_entries(data)
        if not entries:
            logger.warning("OpenAI-compatible reranker returned empty result; fallback to recall order")
            return candidates[:top_k]

        scored: List[RetrievedChunk] = []
        used: set[int] = set()
        for entry in entries:
            idx = self._resolve_index(entry)
            if idx is None or idx >= len(candidates) or idx in used:
                continue
            score = self._resolve_score(entry, default=candidates[idx].score)
            scored.append(RetrievedChunk(chunk=candidates[idx].chunk, score=score))
            used.add(idx)
            if len(scored) >= min(top_k, len(candidates)):
                break

        if len(scored) < min(top_k, len(candidates)):
            for i, item in enumerate(candidates):
                if len(scored) >= top_k:
                    break
                if i in used:
                    continue
                scored.append(RetrievedChunk(chunk=item.chunk, score=item.score))

        return scored

    @staticmethod
    def _extract_entries(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        for key in ("results", "data", "result"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        return []

    @staticmethod
    def _resolve_index(entry: Dict[str, Any]) -> Optional[int]:
        idx = entry.get("index")
        if isinstance(idx, int):
            return idx
        if isinstance(idx, str) and idx.isdigit():
            return int(idx)
        return None

    def _resolve_score(self, entry: Dict[str, Any], default: float) -> float:
        for key in self.SCORE_KEYS:
            value = entry.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return float(default)



logger = logging.getLogger(__name__)


def chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        chunks.append(chunk)
        if end == len(text):
            break
        start = end - overlap
    return chunks


def extract_text_from_bytes(data: bytes, ext: str) -> str:
    ext = (ext or "").lower()
    if ext in {".txt", ".md"}:
        return data.decode("utf-8", errors="ignore")
    if ext == ".pdf":
        reader = pypdf.PdfReader(io.BytesIO(data))
        texts: List[str] = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(texts)
    if ext == ".docx":
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    if ext == ".xlsx":
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts: List[str] = []
        for sheet in wb:
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" \t ".join(cells))
        wb.close()
        return "\n".join(parts)
    return ""


def load_documents(
    documents: Iterable[Dict[str, Any]],
    chunk_size: int,
    overlap: int,
    overlap_ratio: float = 0.15,
) -> List[DocumentChunk]:
    docs: List[DocumentChunk] = []
    effective_overlap = overlap if overlap > 0 else max(1, int(chunk_size * overlap_ratio))
    for doc in documents:
        name = str(doc.get("name") or "")
        content = doc.get("content")
        if not name or not isinstance(content, (bytes, bytearray)):
            continue
        ext = str(doc.get("ext") or Path(name).suffix).lower()
        if ext not in ALLOWED_DOC_EXTS:
            continue
        text = extract_text_from_bytes(bytes(content), ext)
        if not text.strip():
            continue
        base_id = Path(name).stem or Path(name).name or "doc"
        for idx, chunk in enumerate(chunk_text(text, chunk_size, effective_overlap)):
            docs.append(
                DocumentChunk(
                    id=f"{base_id}-{idx}",
                    text=chunk.strip(),
                    source=name,
                    source_type="file",
                    url=None,
                    metadata={"chunk_id": idx, "ext": ext},
                )
            )
    return docs


class LLMClient:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings: Settings = settings or get_settings()
        self.client = None
        self.qianfan_chat = None
        if self.settings.use_qianfan:
            if not (self.settings.qianfan_access_key and self.settings.qianfan_secret_key):
                raise RuntimeError("Qianfan enabled but access/secret key not configured")
            self.qianfan_chat = qianfan.ChatCompletion(
                ak=self.settings.qianfan_access_key,
                sk=self.settings.qianfan_secret_key,
            )
        elif self.settings.openai_api_key:
            # Allow custom base_url for DeepSeek/Sealos or other compatible providers
            self.client = OpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
            )
        else:
            raise RuntimeError("No external LLM configured; set CAMPUS_RAG_OPENAI_API_KEY or enable Qianfan")

    def generate(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        max_tokens = max_tokens or self.settings.generation_max_tokens
        if self.qianfan_chat:
            resp = self.qianfan_chat.do(
                model=self.settings.qianfan_chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_output_tokens=max_tokens,
                stream=False,
            )
            if isinstance(resp, dict):
                return resp.get("result") or resp.get("body", {}).get("result", "")
            return ""
        if self.client:
            completion = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            return completion.choices[0].message.content or ""

        raise RuntimeError("LLM client not initialized")

    def generate_stream(self, prompt: str, max_tokens: Optional[int] = None) -> Iterable[str]:
        """Yield text chunks. If client supports streaming, stream tokens; otherwise yield one chunk."""
        max_tokens = max_tokens or self.settings.generation_max_tokens
        if self.qianfan_chat:
            stream = self.qianfan_chat.stream(  # type: ignore[attr-defined]
                model=self.settings.qianfan_chat_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_output_tokens=max_tokens,
            )
            for chunk in stream:
                delta = chunk.get("result") or ""
                if delta:
                    yield delta
            return
        if self.client:
            stream = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=max_tokens,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    yield delta
            return
        # Should not happen because __init__ enforces external providers
        yield self.generate(prompt, max_tokens=max_tokens)


class RAGPipeline:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings: Settings = settings or get_settings()
        self.vectorstore = VectorStore(self.settings)
        self.llm = LLMClient(self.settings)
        self.embedder = EmbeddingProvider(self.settings)
        self.reranker = None
        model_name = (self.settings.reranker_model or "").strip()
        if model_name:
            self.reranker = self._init_reranker(model_name)

    def retrieve(self, query: str, top_k: Optional[int] = None) -> List[RetrievedChunk]:
        k = top_k or self.settings.top_k
        hits = self.vectorstore.search(query, max(k, self.settings.rerank_top_n if self.reranker else k))
        if self.reranker:
            try:
                reranked = self.reranker.rerank(query, hits, top_k=k)
                return reranked
            except Exception:
                logger.exception("Reranker failed; falling back to original recall results")
                return hits[:k]
        return hits

    @staticmethod
    def _build_context(chunks: List[RetrievedChunk], max_chars: int) -> str:
        parts = []
        current_len = 0
        for item in chunks:
            snippet = f"[来源:{item.chunk.source}] {item.chunk.text}\n"
            if current_len + len(snippet) > max_chars:
                break
            parts.append(snippet)
            current_len += len(snippet)
        return "\n".join(parts)

    def generate_answer(self, query: str, hits: List[RetrievedChunk], max_tokens: Optional[int] = None) -> str:
        context = self._build_context(hits, self.settings.max_context_chars)
        prompt = ANSWER_PROMPT.format(query=query, context=context)
        return self.llm.generate(prompt, max_tokens=max_tokens)

    def generate_answer_stream(self, query: str, hits: List[RetrievedChunk], max_tokens: Optional[int] = None) -> Iterable[str]:
        context = self._build_context(hits, self.settings.max_context_chars)
        prompt = ANSWER_PROMPT.format(query=query, context=context)
        return self.llm.generate_stream(prompt, max_tokens=max_tokens)

    def _init_reranker(self, model_name: str):
        reranker = None
        if self._should_use_openai_reranker(model_name):
            try:
                reranker = OpenAICompatibleReranker(self.settings, model_name=model_name)
                logger.info("Initialized OpenAI-compatible reranker model=%s", model_name)
            except Exception as exc:
                logger.warning("Failed to init OpenAI-compatible reranker (%s): %s", model_name, exc)
        if reranker is None and self._should_use_qianfan_reranker(model_name):
            try:
                reranker = QianfanReranker(self.settings, model_name=model_name)
                logger.info("Initialized Qianfan reranker model=%s", model_name)
            except Exception as exc:
                logger.warning("Failed to init Qianfan reranker (%s), falling back to embedding similarity: %s", model_name, exc)
        if not reranker:
            reranker = _APIReranker(self.embedder, model_name=model_name)
            logger.info("Initialized embedding similarity reranker model=%s", model_name)
        return reranker

    def _should_use_openai_reranker(self, model_name: str) -> bool:
        if not (self.settings.openai_api_key and self.settings.openai_base_url):
            return False
        base = (self.settings.openai_base_url or "").lower()
        if any(marker in base for marker in ("qianfan", "baidubce")):
            return True
        return "reranker" in model_name.lower()

    def _should_use_qianfan_reranker(self, model_name: str) -> bool:
        if not (self.settings.qianfan_access_key and self.settings.qianfan_secret_key):
            return False
        if getattr(self.settings, "use_qianfan", False):
            return True
        lowered = model_name.lower()
        qianfan_markers = ("qwen", "ernie", "bce", "qianfan")
        return any(marker in lowered for marker in qianfan_markers)

    def answer(self, query: str, top_k: Optional[int] = None, max_tokens: Optional[int] = None):
        start = time.time()
        hits = self.retrieve(query, top_k)
        answer = self.generate_answer(query, hits, max_tokens)
        latency_ms = (time.time() - start) * 1000
        sources = [
            SourceAttribution(
                source=hit.chunk.source,
                snippet=hit.chunk.text[:160],
                score=hit.score,
            )
            for hit in hits
        ]
        return answer, sources, latency_ms
