from __future__ import annotations

from pathlib import Path
from typing import List, Any

import faiss
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain.schema import Document
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.chat_models import QianfanChatEndpoint
from langchain_community.embeddings import QianfanEmbeddingsEndpoint
from pydantic.v1 import SecretStr
from langchain_community.vectorstores import FAISS

from .config import Settings, get_settings
from .models import DocumentChunk


class LangChainRAG:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not (self.settings.openai_api_key or self.settings.use_qianfan):
            raise RuntimeError("LangChain pipeline requires OpenAI-compatible key or Qianfan credentials")
        self._vectorstore: Any = None
        self._retriever: Any = None
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

    def _load_chunks(self) -> List[DocumentChunk]:
        meta_path = Path(self.settings.index_path) / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError("Vector metadata not found, please ingest docs first")
        import json

        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return [DocumentChunk(**m) for m in meta]

    def _build_vectorstore(self):
        if self._vectorstore is not None:
            return
        index_path = Path(self.settings.index_path) / "faiss.index"
        if not index_path.exists():
            raise FileNotFoundError("Vector index not found, please ingest docs first")

        index = faiss.read_index(str(index_path))
        chunks = self._load_chunks()

        # Build docstore and mapping aligned with FAISS ids
        docs = {}
        index_to_docstore_id = {}
        for i, ch in enumerate(chunks):
            doc_id = ch.id
            docs[doc_id] = Document(page_content=ch.text, metadata={"source": ch.source, **ch.metadata})
            index_to_docstore_id[i] = doc_id

        if self.settings.use_qianfan:
            embeddings = QianfanEmbeddingsEndpoint(
                endpoint=self.settings.qianfan_embedding_model,
                api_key=self.settings.qianfan_access_key,
                secret_key=self.settings.qianfan_secret_key,
            )
        else:
            api_key = SecretStr(self.settings.openai_api_key) if self.settings.openai_api_key else None
            embeddings = OpenAIEmbeddings(
                api_key=api_key,
                base_url=self.settings.openai_base_url,
                model=self.settings.embedding_model,
            )
        self._vectorstore = FAISS(  # type: ignore[arg-type]
            embedding_function=embeddings,
            index=index,
            docstore=InMemoryDocstore(docs),
            index_to_docstore_id=index_to_docstore_id,
        )
        self._retriever = self._vectorstore.as_retriever(search_kwargs={"k": self.settings.top_k})

    def retrieve(self, query: str):
        self._build_vectorstore()
        return self._retriever.get_relevant_documents(query)

    def answer(self, query: str) -> str:
        self._build_vectorstore()
        docs = self._retriever.get_relevant_documents(query)
        context = "\n".join([f"[来源:{d.metadata.get('source','')}] {d.page_content}" for d in docs])
        prompt = (
            "你是高校校园信息问答助手。根据提供的检索片段回答用户问题，给出简洁答案并标注引用来源。\n"\
            f"用户问题：{query}\n检索片段：\n{context}"
        )
        resp = self._llm.invoke(prompt)
        return resp.content if isinstance(resp.content, str) else str(resp.content)  # type: ignore[return-value]
