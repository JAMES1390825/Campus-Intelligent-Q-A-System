# 接口与操作说明（中文）

## 启动
```bash
uvicorn app.main:app --port 8000 --reload
```
默认监听 8000 端口，跨域已放开。

## 环境变量（见 .env.example）
- 必填：若走云端大模型，填写 `CAMPUS_RAG_OPENAI_API_KEY`，必要时填写 `CAMPUS_RAG_OPENAI_BASE_URL`、`CAMPUS_RAG_OPENAI_MODEL`。
- 可选：`CAMPUS_RAG_USE_LANGCHAIN=true` 时使用 LangChain RAG（需 OpenAI 兼容 key）。
- 可选：`CAMPUS_RAG_RERANKER_MODEL` 启用重排；`CAMPUS_RAG_CACHE_ENABLED` 控制缓存；`CAMPUS_RAG_ALLOW_LOCAL_FALLBACK` 控制本地模型回退。
- 存储：`CAMPUS_RAG_DOCS_PATH`、`CAMPUS_RAG_INDEX_PATH`、`CAMPUS_RAG_DB_PATH` 控制文档/索引/会话存储路径。
- PgVector（可选）：
  - `CAMPUS_RAG_USE_PGVECTOR=true` 开启 PgVector 后端
  - `CAMPUS_RAG_PG_DSN=postgresql://user:pass@host:5432/db`
  - `CAMPUS_RAG_PG_TABLE=rag_chunks`

## 主要接口
- 健康检查：`GET /health`
- 问答：`POST /api/query`
  - 请求体：
    ```json
    {
      "query": "图书馆几点关门",
      "top_k": 4,
      "max_tokens": 512,
      "streaming": false,
      "need_tool": true,
      "session_id": "可选，会话ID"
    }
    ```
  - 返回：`answer`、`sources[]`、`intent`、`agent_traces[]`、`used_tools[]`、`tool_results[]`、`latency_ms`。
- 流式问答：`POST /api/query/stream`
  - 返回 text/plain；首块以 `__META__{...}` JSON 包含来源/意图/工具。
- 文档上传：`POST /api/docs/upload`
  - 表单字段 `file`（仅支持 .txt）；成功后自动重建索引。
- 会话：
  - 创建：`POST /api/session/new` → `{"session_id": "..."}`
  - 查询历史：`GET /api/session/{session_id}/history`
- 指标：`GET /metrics`
  - 返回：请求总数、流式数、doc 上传数、cache 命中、tool 调用数、错误数、avg/p95 延迟等。

## 会话与持久化
- 提交 `session_id` 字段后，问答对会写入 SQLite（默认 `data/session.db`），可用 history 接口查看。

## LangChain/大模型切换
- 若需 LangChain，设置：
  - `CAMPUS_RAG_USE_LANGCHAIN=true`
  - `CAMPUS_RAG_OPENAI_API_KEY=...` （可选 `BASE_URL`、`MODEL`）
- 不填 key 时走本地 Transformers 回退（若允许且模型已下载）。

## 工具调用
- 针对报修/申请等意图自动调用模拟工具，结果出现在 `tool_results`，同时附加在回答末尾。

## 快速验证路径
1) `GET /health` 应返回 ok；
2) 上传一个 `.txt` 到 `/api/docs/upload`；
3) `POST /api/query` 带 `session_id`，查看回答与溯源；
4) `GET /api/session/{session_id}/history` 验证持久化；
5) `GET /metrics` 查看指标是否增长；
6) 将 `CAMPUS_RAG_USE_LANGCHAIN=true` + API Key，重复步骤 3 验证云端模型路径。
