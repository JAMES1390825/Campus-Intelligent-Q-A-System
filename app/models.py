from typing import List, Optional, Any
from pydantic import BaseModel, Field


class DocumentChunk(BaseModel):
    id: str
    text: str
    source: str
    source_type: str
    url: Optional[str] = None
    created_at: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class RetrievedChunk(BaseModel):
    chunk: DocumentChunk
    score: float


class QueryRequest(BaseModel):
    query: str = Field(..., description="用户问题")
    top_k: Optional[int] = Field(default=None, description="可覆盖默认 Top-K")
    max_tokens: Optional[int] = Field(default=None, description="生成 token 数上限")
    streaming: bool = Field(default=False)
    need_tool: bool = Field(default=True, description="是否允许工具调用/流程化处理")
    session_id: Optional[str] = Field(default=None, description="会话ID，用于持久化对话历史")


class SourceAttribution(BaseModel):
    source: str
    snippet: str
    score: float


class AgentTrace(BaseModel):
    name: str
    reasoning: str
    actions: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    name: str
    status: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    data: Optional[dict[str, Any]] = None


class QueryResponse(BaseModel):
    answer: str
    sources: List[SourceAttribution]
    intent: str
    agent_traces: List[AgentTrace]
    latency_ms: Optional[float] = None
    used_tools: List[str] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)


class SessionMessage(BaseModel):
    role: str
    content: str
    created_at: float


class SessionSummary(BaseModel):
    session_id: str
    title: str
    last_message: Optional[str] = None
    created_at: float
    updated_at: float
    message_count: int = 0


class SessionHistoryResponse(BaseModel):
    session_id: str
    title: Optional[str] = None
    history: List[SessionMessage]


class SessionCreateRequest(BaseModel):
    title: Optional[str] = None


class SessionCreateResponse(BaseModel):
    session_id: str
    title: str
    created_at: float


class SessionListResponse(BaseModel):
    sessions: List[SessionSummary]


class SessionUpdateRequest(BaseModel):
    title: str


class HealthStatus(BaseModel):
    status: str
    embedding_model: str
    docs_indexed: int
