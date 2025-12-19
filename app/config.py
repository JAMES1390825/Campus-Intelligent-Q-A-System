from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CAMPUS_RAG_")

    project_name: str = "Campus RAG QA"
    docs_path: Path = Field(default=Path("data/docs"))
    index_path: Path = Field(default=Path("data/index"))
    db_path: Path = Field(default=Path("data/session.db"))
    embedding_model: str = Field(default="BAAI/bge-small-zh-v1.5")
    embedding_batch_size: int = Field(default=32)
    use_openai_embeddings: bool = Field(default=True, description="Use OpenAI-compatible embeddings when api_key provided")
    chunk_size: int = Field(default=700, description="Chunk size in chars/tokens; 512-1024 recommended")
    chunk_overlap: int = Field(default=0, description="Fixed overlap tokens/chars; if <=0, use chunk_overlap_ratio")
    chunk_overlap_ratio: float = Field(default=0.12, description="If chunk_overlap<=0, use ratio of chunk_size for overlap (typ. 0.10~0.15)")
    top_k: int = Field(default=4)
    max_context_chars: int = Field(default=3200)
    openai_api_key: Optional[str] = Field(default=None)
    openai_base_url: Optional[str] = Field(default=None, description="Custom API base URL, e.g., DeepSeek/Sealos")
    openai_model: str = Field(default="gpt-3.5-turbo-0125")
    generation_max_tokens: int = Field(default=512)
    reranker_model: Optional[str] = Field(default=None, description="Set to enable rerank, e.g., BAAI/bge-reranker-base")
    rerank_top_n: int = Field(default=8, description="How many retrieved chunks to rerank before trimming to top_k")
    log_level: str = Field(default="INFO")
    cache_enabled: bool = Field(default=True)
    allow_local_fallback: bool = Field(default=True, description="If False, disable local model fallback to avoid downloads")
    use_langchain: bool = Field(default=False, description="Use LangChain RAG pipeline when OpenAI-compatible key is provided")
    use_pgvector: bool = Field(default=False, description="Enable PgVector backend for embeddings")
    pg_dsn: Optional[str] = Field(default=None, description="PostgreSQL DSN, e.g., postgresql://user:pass@host:5432/db")
    pg_table: str = Field(default="rag_chunks", description="PgVector table name for embeddings")
    admin_password: Optional[str] = Field(default=None, description="Admin password for upload/dashboard access")
    admin_username: str = Field(default="admin", description="Admin username (stored in DB)")
    # MySQL for user accounts
    mysql_host: str = Field(default="localhost")
    mysql_port: int = Field(default=3306)
    mysql_user: str = Field(default="root")
    mysql_password: Optional[str] = Field(default=None)
    mysql_db: str = Field(default="campus_rag")
    # MongoDB for chat/session history
    mongo_uri: str = Field(default="mongodb://localhost:27017")
    mongo_db: str = Field(default="campus_rag")
    mongo_collection: str = Field(default="messages")
    # Qianfan (Baidu) support
    use_qianfan: bool = Field(default=False, description="Use Baidu Qianfan for LLM and/or embeddings")
    qianfan_access_key: Optional[str] = Field(default=None, description="Baidu Qianfan Access Key")
    qianfan_secret_key: Optional[str] = Field(default=None, description="Baidu Qianfan Secret Key")
    qianfan_chat_model: str = Field(default="ERNIE-Speed-128K", description="Qianfan chat model name")
    qianfan_embedding_model: str = Field(default="Embedding-V1", description="Qianfan embedding model name")
    use_qianfan_ocr: bool = Field(default=False, description="Use Baidu OCR via Qianfan/AI Cloud")
    qianfan_ocr_grant_type: str = Field(default="client_credentials", description="OAuth grant type for OCR token")
    # OpenAI-compatible multimodal & OCR model names (optional)
    multimodal_model: Optional[str] = Field(default=None, description="OpenAI-compatible multimodal model name")
    ocr_model: Optional[str] = Field(default=None, description="OpenAI-compatible OCR / image model name")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.index_path.mkdir(parents=True, exist_ok=True)
    settings.docs_path.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
