# 接口与操作说明（中文）

## 启动
```bash
uvicorn app.main:app --port 8000 --reload
    {
      "query": "图书馆几点关门",
      "top_k": 4,
      "max_tokens": 512,
      "streaming": false,
      "session_id": "可选，会话ID"
    }
  - 返回：`answer`、`sources[]`、`latency_ms`。
  - `CAMPUS_RAG_USE_PGVECTOR=true` 开启 PgVector 后端
  - 返回 text/plain；首块以 `__META__{...}` JSON 包含 `sources` 列表。
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
  - 返回：请求总数、流式数、doc 上传数、cache 命中、错误数、avg/p95 延迟等。

## 会话与持久化
- 提交 `session_id` 字段后，问答对会写入 MongoDB（`CAMPUS_RAG_MONGO_URI` + `CAMPUS_RAG_MONGO_DB`），可用 history 接口查看；若 Mongo 不可用则回退到内存。

## LangChain/大模型切换
- 若需 LangChain，设置：
  - `CAMPUS_RAG_USE_LANGCHAIN=true`
  - `CAMPUS_RAG_OPENAI_API_KEY=...` （可选 `BASE_URL`、`MODEL`）
> 注意：系统默认依赖外部 LLM API（OpenAI 兼容或千帆），不再提供本地 Transformers 回退。


## 快速验证路径
1) `GET /health` 应返回 ok；
2) 上传一个 `.txt` 到 `/api/docs/upload`；
3) `POST /api/query` 带 `session_id`，查看回答与溯源；
4) `GET /api/session/{session_id}/history` 验证持久化；
5) `GET /metrics` 查看指标是否增长；
6) 将 `CAMPUS_RAG_USE_LANGCHAIN=true` + API Key，重复步骤 3 验证云端模型路径。
