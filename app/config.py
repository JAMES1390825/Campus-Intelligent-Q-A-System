from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CAMPUS_RAG_")

    project_name: str = "Campus RAG QA"
    embedding_model: str = Field(default="text-embedding-3-large")
    embedding_batch_size: int = Field(default=32, description="Max chunks per embedding API call")
    embedding_max_retries: int = Field(default=4, description="Retries for embedding calls when rate-limited")
    embedding_retry_base_delay: float = Field(default=0.8, description="Initial backoff delay (seconds) for embeddings")
    embedding_retry_max_delay: float = Field(default=6.0, description="Max backoff delay (seconds) for embeddings")
    use_openai_embeddings: bool = Field(default=True, description="Use OpenAI-compatible embeddings when api_key provided")
    chunk_size: int = Field(default=700, description="Chunk size in chars/tokens; 512-1024 recommended")
    chunk_overlap: int = Field(default=0, description="Fixed overlap tokens/chars; if <=0, use chunk_overlap_ratio")
    chunk_overlap_ratio: float = Field(default=0.12, description="If chunk_overlap<=0, use ratio of chunk_size for overlap (typ. 0.10~0.15)")
    top_k: int = Field(default=4)
    min_relevance: float = Field(default=0.35, description="Similarity threshold (0-1). Below this we fall back to generic reply")
    max_context_chars: int = Field(default=3200)
    openai_api_key: Optional[str] = Field(default=None)
    openai_base_url: Optional[str] = Field(default=None, description="Custom API base URL, e.g., DeepSeek/Sealos")
    openai_model: str = Field(default="gpt-3.5-turbo-0125")
    generation_max_tokens: int = Field(default=512)
    reranker_model: Optional[str] = Field(default=None, description="Set to enable rerank, e.g., BAAI/bge-reranker-base")
    rerank_top_n: int = Field(default=8, description="How many retrieved chunks to rerank before trimming to top_k")
    log_level: str = Field(default="INFO")
    cache_enabled: bool = Field(default=True)
    use_langchain: bool = Field(default=False, description="Use LangChain RAG pipeline when OpenAI-compatible key is provided")
    use_pgvector: bool = Field(default=True, description="Enable PgVector backend for embeddings (required)")
    pg_dsn: Optional[str] = Field(default=None, description="PostgreSQL DSN, e.g., postgresql://user:pass@host:5432/db")
    pg_table: str = Field(default="rag_chunks", description="PgVector table name for embeddings")
    admin_password: Optional[str] = Field(default=None, description="Admin password for upload/dashboard access")
    admin_username: str = Field(default="admin", description="Admin username (stored in DB)")
    max_upload_mb: int = Field(default=20, description="Maximum size (MB) allowed per uploaded document")
    # Object storage for raw documents
    use_oss_storage: bool = Field(default=False, description="Persist uploaded files to object storage (OSS/S3)")
    oss_endpoint: Optional[str] = Field(default=None, description="OSS endpoint, e.g., oss-cn-hangzhou.aliyuncs.com")
    oss_internal_endpoint: Optional[str] = Field(default=None, description="Internal endpoint for same-region access")
    oss_bucket: Optional[str] = Field(default=None, description="OSS bucket name")
    oss_access_key_id: Optional[str] = Field(default=None, description="OSS AccessKey ID")
    oss_access_key_secret: Optional[str] = Field(default=None, description="OSS AccessKey Secret")
    oss_prefix: str = Field(default="docs", description="Object storage prefix/folder for uploaded documents")
    # MySQL for user accounts
    mysql_host: str = Field(default="localhost")
    mysql_port: int = Field(default=3306)
    mysql_user: str = Field(default="root")
    mysql_password: Optional[str] = Field(default=None)
    mysql_db: str = Field(default="campus_rag")
    mysql_docs_table: str = Field(default="documents", description="MySQL table that stores raw uploaded documents")
    # MongoDB for chat/session history
    mongo_uri: str = Field(default="mongodb://localhost:27017")
    mongo_db: str = Field(default="campus_rag")
    mongo_collection: str = Field(default="messages")
    # Redis for task coordination / locking
    redis_url: Optional[str] = Field(default=None, description="Redis URL, e.g., redis://localhost:6379/0")
    redis_host: str = Field(default="localhost")
    redis_port: int = Field(default=6379)
    redis_db: int = Field(default=0)
    redis_password: Optional[str] = Field(default=None)
    redis_prefix: str = Field(default="campusqa")
    ingest_workers: int = Field(default=1, description="后台向量化线程数")
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
    return settings
