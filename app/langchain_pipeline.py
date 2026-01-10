from __future__ import annotations

from typing import List, Any, Optional

from langchain.schema import Document
from langchain_openai import ChatOpenAI
from langchain_community.chat_models import QianfanChatEndpoint
from pydantic.v1 import SecretStr

from .config import Settings, get_settings
from .vectorstore_pgvector import PGVectorStore


class LangChainRAG:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        if not (self.settings.openai_api_key or self.settings.use_qianfan):
            raise RuntimeError("LangChain pipeline requires OpenAI-compatible key or Qianfan credentials")
        if not self.settings.pg_dsn:
            raise RuntimeError("LangChain pipeline requires PgVector (CAMPUS_RAG_PG_DSN)")
        self._pgvector = PGVectorStore(self.settings)
        if self.settings.use_qianfan:
            if not (self.settings.qianfan_access_key and self.settings.qianfan_secret_key):
                raise RuntimeError("Qianfan enabled but credentials missing")
            self._llm = QianfanChatEndpoint(
                endpoint=self.settings.qianfan_chat_model,
                api_key=self.settings.qianfan_access_key,
                secret_key=self.settings.qianfan_secret_key,
                temperature=0.2,
            )
        else:
            api_key = SecretStr(self.settings.openai_api_key) if self.settings.openai_api_key else None
            self._llm = ChatOpenAI(
                api_key=api_key,
                base_url=self.settings.openai_base_url,
                model=self.settings.openai_model,
                temperature=0.2,
            )

    def retrieve(self, query: str):
        hits = self._pgvector.search(query, self.settings.top_k)
        docs: List[Document] = []
        for hit in hits:
            meta = {"source": hit.chunk.source}
            if hit.chunk.metadata:
                meta.update(hit.chunk.metadata)
            docs.append(Document(page_content=hit.chunk.text, metadata=meta))
        return docs

    def answer(self, query: str) -> str:
        docs = self.retrieve(query)
        context = "\n".join([f"[来源:{d.metadata.get('source','')}] {d.page_content}" for d in docs])
        prompt = (
            "你是高校校园信息问答助手。根据提供的检索片段回答用户问题，给出简洁答案并标注引用来源。\n"\
            f"用户问题：{query}\n检索片段：\n{context}"
        )
        resp = self._llm.invoke(prompt)
        return resp.content if isinstance(resp.content, str) else str(resp.content)  # type: ignore[return-value]
