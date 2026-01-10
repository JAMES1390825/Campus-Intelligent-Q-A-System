from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Iterable, Tuple

from .config import get_settings, Settings
from .models import QueryRequest, QueryResponse, SourceAttribution, RetrievedChunk
from .rag import RAGPipeline
from .langchain_pipeline import LangChainRAG
from .prompts import FALLBACK_NO_CONTEXT, FALLBACK_GREETING

logger = logging.getLogger("campusqa.agents")


GREETING_PATTERN = re.compile(r"^(你好|您好|嗨|哈喽|hello|hi|hey)([啊呀呀～!！。,.…·]*)$", re.IGNORECASE)


class AgentOrchestrator:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings: Settings = settings or get_settings()
        self.rag = RAGPipeline(self.settings)
        self.lc_rag = LangChainRAG(self.settings) if self.settings.use_langchain and self.settings.openai_api_key else None
        self.cache: Dict[str, QueryResponse] = {}

    @staticmethod
    def _cache_key(req: QueryRequest) -> str:
        raw = f"{req.query}|{req.top_k}|{req.max_tokens}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def handle(self, req: QueryRequest) -> Tuple[QueryResponse, bool]:
        cache_key = self._cache_key(req)
        if self.settings.cache_enabled and cache_key in self.cache:
            cached = self.cache[cache_key]
            return cached, True

        logger.info("[agent] retrieve start top_k=%s", req.top_k)
        start = time.time()
        hits = self.rag.retrieve(req.query, top_k=req.top_k)
        logger.info("[agent] retrieve done hits=%s", len(hits))
        filtered_hits, best_score = self._apply_relevance_threshold(hits)
        if not filtered_hits:
            logger.info(
                "[agent] low relevance fallback best=%.3f threshold=%.2f query=%s",
                best_score,
                getattr(self.settings, "min_relevance", 0.0) or 0.0,
                req.query,
            )
            resp = self._build_low_relevance_response(req.query)
            if self.settings.cache_enabled:
                self.cache[cache_key] = resp
            return resp, False

        if self.lc_rag:
            logger.info("[agent] lc_rag answer start")
            answer_text = self.lc_rag.answer(req.query)
            logger.info("[agent] lc_rag answer done")
            latency_ms = (time.time() - start) * 1000
        else:
            logger.info("[agent] rag answer start")
            answer_text = self.rag.generate_answer(req.query, filtered_hits, max_tokens=req.max_tokens)
            latency_ms = (time.time() - start) * 1000
            logger.info("[agent] rag answer done latency_ms=%s", latency_ms)

        sources = [
            SourceAttribution(source=hit.chunk.source, snippet=hit.chunk.text[:160], score=hit.score)
            for hit in filtered_hits
        ]

        resp = QueryResponse(
            answer=answer_text,
            sources=sources,
            latency_ms=latency_ms,
        )

        if self.settings.cache_enabled:
            self.cache[cache_key] = resp
        return resp, False

    def handle_stream(self, req: QueryRequest) -> Iterable[str]:
        """Yield streaming answer chunks with a meta header chunk prefixed by __META__."""

        hits = self.rag.retrieve(req.query, top_k=req.top_k)
        filtered_hits, best_score = self._apply_relevance_threshold(hits)
        if not filtered_hits:
            meta = {"sources": [], "low_relevance": True, "best_score": best_score}
            yield "__META__" + json.dumps(meta, ensure_ascii=False)
            yield self._fallback_text(req.query)
            return

        meta: Dict[str, Any] = {
            "sources": [
                {
                    "source": h.chunk.source,
                    "snippet": h.chunk.text[:160],
                    "score": h.score,
                }
                for h in filtered_hits
            ],
            "best_score": best_score,
        }
        yield "__META__" + json.dumps(meta, ensure_ascii=False)

        stream = self.rag.generate_answer_stream(req.query, filtered_hits, max_tokens=req.max_tokens)
        for chunk in stream:
            yield chunk

    def _apply_relevance_threshold(self, hits: List[RetrievedChunk]) -> Tuple[List[RetrievedChunk], float]:
        threshold = float(getattr(self.settings, "min_relevance", 0.0) or 0.0)
        threshold = max(0.0, min(1.0, threshold))
        best_score = max((h.score for h in hits), default=0.0)
        if threshold <= 0:
            return hits, best_score
        filtered = [h for h in hits if h.score >= threshold]
        return filtered, best_score

    def _fallback_text(self, query: str) -> str:
        clean = (query or "").strip()
        if self._is_greeting(clean):
            return FALLBACK_GREETING
        clean = clean or "该问题"
        return FALLBACK_NO_CONTEXT.format(query=clean)

    def _build_low_relevance_response(self, query: str) -> QueryResponse:
        return QueryResponse(answer=self._fallback_text(query), sources=[], latency_ms=0.0)

    @staticmethod
    def _is_greeting(text: Optional[str]) -> bool:
        if not text:
            return False
        normalized = text.strip()
        if not normalized:
            return False
        if GREETING_PATTERN.match(normalized):
            return True
        compact = normalized.replace(" ", "")
        if len(compact) <= 6 and compact.lower() in {"hello", "hi"}:
            return True
        return False
