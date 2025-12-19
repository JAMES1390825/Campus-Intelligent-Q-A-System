from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional, Iterable
import logging
from .config import get_settings, Settings

import numpy as np
from openai import OpenAI
import qianfan
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline
from sentence_transformers import SentenceTransformer
from .models import DocumentChunk, RetrievedChunk, SourceAttribution
from .prompts import ANSWER_PROMPT
from .vectorstore import VectorStore
from .embedding_provider import EmbeddingProvider
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


def load_documents(docs_path: Path, chunk_size: int, overlap: int, overlap_ratio: float = 0.15) -> List[DocumentChunk]:
    docs: List[DocumentChunk] = []
    effective_overlap = overlap if overlap > 0 else max(1, int(chunk_size * overlap_ratio))
    for path in docs_path.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        for idx, chunk in enumerate(chunk_text(text, chunk_size, effective_overlap)):
            docs.append(
                DocumentChunk(
                    id=f"{path.stem}-{idx}",
                    text=chunk.strip(),
                    source=path.name,
                    source_type="file",
                    url=None,
                    metadata={"chunk_id": idx},
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
        self._local_pipeline = None

    def _ensure_local(self):
        if self._local_pipeline:
            return
        # Small chat-friendly model for local inference; replace with a better model if GPU available.
        model_name = "Qwen/Qwen1.5-0.5B-Chat"
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForCausalLM.from_pretrained(model_name)
        self._local_pipeline = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=self.settings.generation_max_tokens,
        )

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
            return resp.get("result") or resp.get("body", {}).get("result", "")
        if self.client:
            completion = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            return completion.choices[0].message.content

        # If no API key, fallback to local small model (may trigger download once)
        if not self.settings.allow_local_fallback:
            raise RuntimeError("No API key configured and local fallback disabled")
        self._ensure_local()
        out = self._local_pipeline(prompt, max_new_tokens=max_tokens, do_sample=False)
        return out[0]["generated_text"][-max_tokens:]

    def generate_stream(self, prompt: str, max_tokens: Optional[int] = None) -> Iterable[str]:
        """Yield text chunks. If client supports streaming, stream tokens; otherwise yield one chunk."""
        max_tokens = max_tokens or self.settings.generation_max_tokens
        if self.qianfan_chat:
            stream = self.qianfan_chat.stream(
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
        # fallback: single chunk
        yield self.generate(prompt, max_tokens=max_tokens)


class RAGPipeline:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings: Settings = settings or get_settings()
        self.vectorstore = VectorStore(self.settings)
        self.llm = LLMClient(self.settings)
        self.embedder = EmbeddingProvider(self.settings)
        self.reranker = None
        if self.settings.reranker_model:
            # API-based reranker via embedding similarity (no local HF download)
            self.reranker = _APIReranker(self.embedder, model_name=self.settings.reranker_model)

    def retrieve(self, query: str, top_k: Optional[int] = None) -> List[RetrievedChunk]:
        k = top_k or self.settings.top_k
        hits = self.vectorstore.search(query, max(k, self.settings.rerank_top_n if self.reranker else k))
        if self.reranker:
            reranked = self.reranker.rerank(query, hits, top_k=k)
            return reranked
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
