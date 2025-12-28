# Campus RAG QA MVP

高性能的校园信息智能问答 MVP，包含 RAG、意图识别、多智能体流程、工具调用模拟、FastAPI 接口与简易前端。

## 功能要点
- **RAG 检索问答**：BGE 中文 Embedding + FAISS，检索 Top-K 片段并附来源引用。
- **多智能体协作**：意图识别 → 检索 Agent → 生成 Agent；支持工具调用分支（报修、申请草稿）。
- **工具调用 Demo**：模拟“报修单”“申请草稿”工具，结果附加到回答中。
- **流式输出**：`/api/query/stream` 支持流式生成，前端勾选“流式输出”可实时看到答案。
- **可选重排**：配置 `CAMPUS_RAG_RERANKER_MODEL` 启用重排，支持 `BAAI/bge-reranker-base` 等嵌入式 reranker，也可直接调用千帆 OpenAI 兼容端点上的 `qwen3-reranker-8b`（同一 `CAMPUS_RAG_OPENAI_API_KEY` 即可）。
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

若同时设置 `CAMPUS_RAG_RERANKER_MODEL=qwen3-reranker-8b`（或其他千帆重排模型），后端会直接调用 `qianfan.Reranker().do()`，无需再走 embedding 兜底或触发 Hugging Face 下载。

如果你只配置了千帆 OpenAI 兼容接口（`CAMPUS_RAG_OPENAI_BASE_URL=https://qianfan.baidubce.com/v2` + API Key），系统会优先走 `/rerank` 端点完成重排；AK/SK 仅在需要访问千帆原生 API 时才是必需项。

OCR：启用 `CAMPUS_RAG_USE_QIANFAN_OCR=true` 后，可用 `app.ocr_client.get_ocr_client()` 调用百度通用文字识别（general_basic）。可将图片转文本再走 ingest / 检索。

## 对接阿里云 OSS（文档上传 & 向量化）
项目支持将原始文档存入 OSS bucket，并在服务端自动同步到本地进行预览/向量化。配置步骤：

1. 安装依赖（requirements 已包含 `oss2`）。
2. 在 `.env` 中添加：
  ```
  CAMPUS_RAG_USE_OSS_STORAGE=true
  CAMPUS_RAG_OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
  CAMPUS_RAG_OSS_INTERNAL_ENDPOINT=oss-cn-hangzhou-internal.aliyuncs.com  # 可选，ECS 内网访问
  CAMPUS_RAG_OSS_BUCKET=your-bucket
  CAMPUS_RAG_OSS_ACCESS_KEY_ID=xxx
  CAMPUS_RAG_OSS_ACCESS_KEY_SECRET=yyy
  CAMPUS_RAG_OSS_PREFIX=docs  # 可选，桶内前缀，相当于“文件夹”
  ```
3. 重启服务后：
  - 管理端上传的文件会写入 OSS 并同步一份到 `data/docs` 作为本地缓存。
  - 启动阶段会尝试 `sync_from_remote()`，确保本地缓存包含 bucket 内所有历史文档。
  - `/api/admin/docs` / 预览 / 下载 接口统一从缓存读取；若本地缺失会自动拉取 OSS 文件。
  - 触发重建索引前再执行一次同步，保证向量化过程覆盖 OSS 中的全部文件。

> 参考 FastGPT 的“上传 -> OSS 持久化 -> 后台异步向量化”模式。若有多实例部署，确保只有一台负责 ingest，或在 `scripts/ingest.py` 中显式调用 `DocumentStorage.sync_from_remote()` 再构建索引。

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
- 重排流程目前串行调用外部 API，后续可根据吞吐需求增加批量/并发能力。
- LoRA 微调管线未在此仓库展开，可在 `peft`/`transformers` 基础上补充训练脚本。

## 版权声明
示例文档为虚构整理内容，仅供演示，不代表任何真实学校规定。
