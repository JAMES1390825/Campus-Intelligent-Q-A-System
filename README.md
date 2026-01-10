# Campus RAG QA MVP

高性能的校园信息智能问答 MVP，包含纯粹的 RAG 检索生成、FastAPI 接口、Web 学生端/管理端，以及可直接对接的微信小程序。

## 功能要点
- **RAG 检索问答**：云端 Embedding API + PgVector，检索 Top-K 片段并附来源引用。
- **单链路 RAG**：从检索到生成的轻量链路，专注问答准确性与溯源展示，不再内置多智能体和工具分支。
- **流式输出**：`/api/query/stream` 支持流式生成，前端勾选“流式输出”可实时看到答案。
- **可选重排**：配置 `CAMPUS_RAG_RERANKER_MODEL` 启用重排，支持 `BAAI/bge-reranker-base` 等嵌入式 reranker，也可直接调用千帆 OpenAI 兼容端点上的 `qwen3-reranker-8b`（同一 `CAMPUS_RAG_OPENAI_API_KEY` 即可）。
- **高性能实践**：
  - 归一化向量 + 内积检索，默认 Top-K=4。
  - 上下文裁剪（max_context_chars=3200），可配置。
  - 查询级缓存（可通过 env 关闭）。
- **易扩展**：基于 OpenAI/DeepSeek/通义千问/千帆等外部 API，环境变量即可切换模型、重排与向量数据库，无需本地模型或额外微调。
- **三段式文档管道**：上传文件 → 对象存储保存原始字节 → MySQL `documents` 表记录元数据与向量引用 → PgVector 存放切片向量，满足“对象存储 + 关系型数据库 + 向量库”拓扑。
- **标签式管理员工作台**：文档总览、上传队列、账号注册、调试问答分栏展示，切换 Tab 即可操作不同任务。

## 快速开始
```bash
# 1) 安装依赖（建议 Python 3.10+）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) 通过管理端上传文档后构建向量索引
python scripts/ingest.py  # 从 MySQL documents 表读取所有原始文档并写入 PgVector

# 3) 运行服务
uvicorn app.main:app --reload --port 8000

# 4) 打开前端
open http://localhost:8000
```

> 上传的原始文件会存入对象存储（配置 `CAMPUS_RAG_USE_OSS_STORAGE=true` 后对接阿里云 OSS / S3 兼容服务），MySQL `documents` 表仅保存元数据、OSS Key 及向量引用。`scripts/ingest.py` 与后台自动向量化任务通过 `DocumentStorage.stream_documents()` 从对象存储拉取内容并重建 PgVector（默认表 `rag_chunks`），彻底告别本地 `data/` 目录。

可选：使用 OpenAI/DeepSeek/Sealos 推理，设置环境变量（或写入 `.env`）：
```
CAMPUS_RAG_OPENAI_API_KEY=sk-xxx
CAMPUS_RAG_OPENAI_MODEL=gpt-3.5-turbo-0125
CAMPUS_RAG_OPENAI_BASE_URL=https://your-base-url
# 可选：启用重排
CAMPUS_RAG_RERANKER_MODEL=BAAI/bge-reranker-base
```

可选：使用百度千帆（Qianfan）全托管推理：
```
CAMPUS_RAG_USE_QIANFAN=true
CAMPUS_RAG_QIANFAN_ACCESS_KEY=your-ak
CAMPUS_RAG_QIANFAN_SECRET_KEY=your-sk
CAMPUS_RAG_QIANFAN_CHAT_MODEL=ERNIE-Speed-128K
CAMPUS_RAG_QIANFAN_EMBEDDING_MODEL=Embedding-V1
CAMPUS_RAG_USE_QIANFAN_OCR=true
CAMPUS_RAG_QIANFAN_OCR_GRANT_TYPE=client_credentials
```
开启后，问答走千帆 Chat API，向量化用千帆 Embedding（向 PgVector 写入）。

若同时设置 `CAMPUS_RAG_RERANKER_MODEL=qwen3-reranker-8b`（或其他千帆重排模型），后端会直接调用 `qianfan.Reranker().do()`。

如果你只配置了千帆 OpenAI 兼容接口（`CAMPUS_RAG_OPENAI_BASE_URL=https://qianfan.baidubce.com/v2` + API Key），系统会优先走 `/rerank` 端点完成重排；AK/SK 仅在需要访问千帆原生 API 时才是必需项。

OCR：启用 `CAMPUS_RAG_USE_QIANFAN_OCR=true` 后，可用 `app.ocr_client.get_ocr_client()` 调用百度通用文字识别（general_basic）。可将图片转文本再走 ingest / 检索。

## 目录结构
```
app/
  config.py        # 配置 & 默认参数
  models.py        # Pydantic 模型
  vectorstore.py   # PgVector 向量索引封装
  rag.py           # chunk、加载文档、RAG 生成
  agents.py        # 统一问答编排逻辑（缓存 + RAG）
  document_storage.py # 对象存储 + MySQL 元数据管理
  prompts.py       # Prompt 模板
  main.py          # FastAPI 入口
  static/index.html# 简易前端
scripts/
  ingest.py        # 构建向量索引
  quick_eval.py    # CLI 快速评测
```

## 性能与质量建议
- 根据需求切换 `CAMPUS_RAG_EMBEDDING_MODEL`（如 text-embedding-3-large、Embedding-V1 等），或启用 PgVector 以便共享向量索引。
- 部署时关闭 `--reload`，开启 `cache_enabled`（默认开启）。
- 需要更快生成时，优先选择更高吞吐/更低延迟的云端模型或供应商。
- 根据业务调整 `chunk_size`/`chunk_overlap`，并在 `scripts/ingest.py` 重新构建索引。

## 已知局限与下一步
- 重排流程目前串行调用外部 API，后续可根据吞吐需求增加批量/并发能力。

## 版权声明
示例文档为虚构整理内容，仅供演示，不代表任何真实学校规定。
