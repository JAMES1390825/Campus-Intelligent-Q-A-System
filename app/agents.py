from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Tuple, Optional, Iterable

from .config import get_settings, Settings
from .models import AgentTrace, QueryRequest, QueryResponse, ToolResult, SourceAttribution
from .rag import RAGPipeline
from .tooling import ToolRouter
from .langchain_pipeline import LangChainRAG

logger = logging.getLogger("campusqa.agents")


def simple_intent_detection(query: str) -> Tuple[str, str]:
    q = query.lower()
    if any(k in q for k in ["报修", "维修", "修好", "ticket", "repair"]):
        return "action", "检测到报修/工单关键词"
    if any(k in q for k in ["请假", "申请", "提交", "draft", "审批"]):
        return "procedure", "检测到申请/审批相关请求"
    return "qa", "默认归类为知识问答"


def plan_tool(query: str) -> Tuple[Optional[str], Dict[str, Any], str]:
    q = query.lower()
    if any(k in q for k in ["报修", "维修", "坏了", "漏水"]):
        payload = {"type": "repair", "description": query}
        return "create_repair_ticket", payload, "报修意图，生成报修单草稿"
    if any(k in q for k in ["请假", "申请", "补办", "证明"]):
        payload: Dict[str, Any] = {"type": "application", "reason": query, "days": 1}
        return "submit_application", payload, "申请意图，生成申请草稿"
    return None, {}, "无需工具"


class AgentOrchestrator:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings: Settings = settings or get_settings()
        self.rag = RAGPipeline(self.settings)
        self.lc_rag = LangChainRAG(self.settings) if self.settings.use_langchain and self.settings.openai_api_key else None
        self.tools = ToolRouter()
        self.cache: Dict[str, QueryResponse] = {}

    @staticmethod
    def _cache_key(req: QueryRequest) -> str:
        raw = f"{req.query}|{req.top_k}|{req.max_tokens}|{req.need_tool}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def handle(self, req: QueryRequest) -> Tuple[QueryResponse, bool]:
        cache_key = self._cache_key(req)
        if self.settings.cache_enabled and cache_key in self.cache:
            cached = self.cache[cache_key]
            return cached, True

        traces: List[AgentTrace] = []
        intent, reason = simple_intent_detection(req.query)
        _ = reason  # silence unused warning
        traces.append(AgentTrace(name="intent", reasoning=reason, actions=[intent]))

        logger.info("[agent] retrieve start top_k=%s", req.top_k)
        hits = self.rag.retrieve(req.query, top_k=req.top_k)
        logger.info("[agent] retrieve done hits=%s", len(hits))
        traces.append(AgentTrace(name="retrieval", reasoning=f"Top-{req.top_k or self.settings.top_k}", actions=[h.chunk.id for h in hits]))

        used_tools: List[str] = []
        tool_results: List[ToolResult] = []
        if req.need_tool and intent in {"procedure", "action"}:
            tool_name, payload, why = plan_tool(req.query)
            if tool_name:
                logger.info("[agent] tool start=%s", tool_name)
                tool_result = self.tools.call(tool_name, payload)
                logger.info("[agent] tool done=%s", tool_name)
                tool_results.append(tool_result)
                used_tools.append(tool_name)
                traces.append(AgentTrace(name="tool", reasoning=why, actions=[json.dumps(tool_result.model_dump(), ensure_ascii=False)]))
            else:
                traces.append(AgentTrace(name="tool", reasoning="无需工具", actions=[]))

        if self.lc_rag:
            logger.info("[agent] lc_rag answer start")
            answer_text = self.lc_rag.answer(req.query)
            latency_ms = None
            sources = [
                SourceAttribution(source=h.chunk.source, snippet=h.chunk.text[:160], score=h.score)
                for h in hits
            ]
            logger.info("[agent] lc_rag answer done")
        else:
            logger.info("[agent] rag answer start")
            answer_text, sources, latency_ms = self.rag.answer(
                req.query,
                top_k=req.top_k,
                max_tokens=req.max_tokens,
            )
            logger.info("[agent] rag answer done latency_ms=%s", latency_ms)

        if tool_results:
            formatted = "\n\n".join([
                f"[工具:{t.name}] 状态:{t.status}\n{t.message}\n负载:{t.payload}"
                for t in tool_results
            ])
            answer_text += "\n\n[已为你生成自动化处理结果]\n" + formatted

        resp = QueryResponse(
            answer=answer_text,
            sources=sources,
            intent=intent,
            agent_traces=traces,
            latency_ms=latency_ms,
            used_tools=used_tools,
            tool_results=tool_results,
        )

        if self.settings.cache_enabled:
            self.cache[cache_key] = resp
        return resp, False

    def handle_stream(self, req: QueryRequest) -> Iterable[str]:
        """Yield streaming answer chunks with a meta header chunk prefixed by __META__."""

        intent, reason = simple_intent_detection(req.query)
        _ = reason
        hits = self.rag.retrieve(req.query, top_k=req.top_k)

        used_tools: List[str] = []
        tool_result = None
        if req.need_tool and intent in {"procedure", "action"}:
            tool_name, payload, why = plan_tool(req.query)
            _ = why
            if tool_name:
                tool_result = self.tools.call(tool_name, payload)
                used_tools.append(tool_name)

        # Build meta info and send first
        meta: Dict[str, Any] = {
            "sources": [
                {
                    "source": h.chunk.source,
                    "snippet": h.chunk.text[:160],
                    "score": h.score,
                }
                for h in hits
            ],
            "intent": intent,
            "used_tools": used_tools,
        }
        yield "__META__" + json.dumps(meta, ensure_ascii=False)

        stream = self.rag.generate_answer_stream(req.query, hits, max_tokens=req.max_tokens)
        for chunk in stream:
            yield chunk
        if tool_result:
            yield "\n\n[已为你生成自动化处理结果]\n" + str(tool_result)
