# Campus RAG QA MVP

高性能的校园信息智能问答 MVP，包含 RAG、意图识别、多智能体流程、工具调用模拟、FastAPI 接口与简易前端。

## 功能要点
- **RAG 检索问答**：BGE 中文 Embedding + FAISS，检索 Top-K 片段并附来源引用。
- **多智能体协作**：意图识别 → 检索 Agent → 生成 Agent；支持工具调用分支（报修、申请草稿）。
- **工具调用 Demo**：模拟“报修单”“申请草稿”工具，结果附加到回答中。
- **流式输出**：`/api/query/stream` 支持流式生成，前端勾选“流式输出”可实时看到答案。
- **可选重排**：配置 `CAMPUS_RAG_RERANKER_MODEL` 启用 reranker（如 `BAAI/bge-reranker-base`），提升命中质量。
- **高性能实践**：
  - 归一化向量 + 内积检索，默认 Top-K=4。
  - 上下文裁剪（max_context_chars=3200），可配置。
  - 查询级缓存（可通过 env 关闭）。
- **易扩展**：OpenAI API 或本地 HF 模型；LoRA 微调依赖已准备（peft/accelerate/transformers）。

## 快速开始
```bash
# 1) 安装依赖（建议 Python 3.10+）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) 准备向量索引
python scripts/ingest.py  # 默认读取 data/docs 下示例文档

# 3) 运行服务
uvicorn app.main:app --reload --port 8000

# 4) 打开前端
open http://localhost:8000
```

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
开启后，问答走千帆 Chat API，向量化用千帆 Embedding（FAISS/PgVector 均可）。

OCR：启用 `CAMPUS_RAG_USE_QIANFAN_OCR=true` 后，可用 `app.ocr_client.get_ocr_client()` 调用百度通用文字识别（general_basic）。可将图片转文本再走 ingest / 检索。

## 目录结构
```
app/
  config.py        # 配置 & 默认参数
  models.py        # Pydantic 模型
  vectorstore.py   # 向量索引封装（FAISS + metadata）
  rag.py           # chunk、加载文档、RAG 生成
  agents.py        # 意图、检索、生成、工具路由
  tooling.py       # 模拟工具（报修/申请草稿）
  prompts.py       # Prompt 模板
  main.py          # FastAPI 入口
  static/index.html# 简易前端
scripts/
  ingest.py        # 构建向量索引
  quick_eval.py    # CLI 快速评测
data/docs/         # 示例文档
```

## 性能与质量建议
- 有 GPU 时，将 `settings.embedding_model` 替换为更大的中文 Embedding（如 bge-large-zh）。
- 部署时关闭 `--reload`，开启 `cache_enabled`（默认开启）。
- 需要更快生成时，优先用云端 API（OpenAI / 腾讯 / 阿里），或在本地切换更小的 HF 模型。
- 根据业务调整 `chunk_size`/`chunk_overlap`，并在 `scripts/ingest.py` 重新构建索引。

## 已知局限与下一步
- 本地默认模型 `Qwen1.5-0.5B-Chat` 仅作演示，生产建议接入更强模型或部署 TGI/vLLM。
- 未实现重排序/rerank，可在检索后加入模型（如 bge-reranker）。
- LoRA 微调管线未在此仓库展开，可在 `peft`/`transformers` 基础上补充训练脚本。

## 版权声明
示例文档为虚构整理内容，仅供演示，不代表任何真实学校规定。
