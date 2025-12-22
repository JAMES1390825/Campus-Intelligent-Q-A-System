from __future__ import annotations

from pathlib import Path
import time
import uuid
import logging
from typing import Optional, Dict, List, Union, Tuple, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Depends, Form, Body
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse

from .agents import AgentOrchestrator
from .config import get_settings
from .models import (
    HealthStatus,
    QueryRequest,
    QueryResponse,
    SessionCreateResponse,
    SessionHistoryResponse,
)
from .vectorstore import VectorStore
from .rag import load_documents
import pypdf
import docx
import openpyxl
from .session_store import SessionStore
from .metrics import Metrics
from .user_store import UserStore

settings = get_settings()
orchestrator = AgentOrchestrator(settings)
vectorstore = VectorStore(settings)
session_store = SessionStore(settings)
metrics = Metrics()
user_store = UserStore(settings)

logger = logging.getLogger("campusqa.main")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.project_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

ALLOWED_DOC_EXTS = {".txt", ".md", ".pdf", ".docx", ".xlsx"}


def _extract_text_for_preview(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if ext == ".pdf":
        with path.open("rb") as f:
            reader = pypdf.PdfReader(f)
            texts: List[str] = []
            for page in reader.pages:
                try:
                    texts.append(page.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(texts)
    if ext == ".docx":
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if ext == ".xlsx":
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts: List[str] = []
        for sheet in wb:
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" \t ".join(cells))
        wb.close()
        return "\n".join(parts)
    return ""


def _parse_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少凭证")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="凭证格式错误")
    return parts[1]


def _require_admin(authorization: Optional[str] = Header(default=None)) -> Tuple[str, str]:
    """管理员只校验 token & 角色，不强制首次改密。"""

    token = _parse_bearer_token(authorization)
    auth = user_store.validate_token(token)
    if not auth:
        raise HTTPException(status_code=401, detail="凭证无效或已过期")
    username, role, _ = auth
    if role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return username, role


def _require_user_active(authorization: Optional[str] = Header(default=None)) -> str:
    """普通用户访问受保护资源，需要已修改初始密码。"""

    token = _parse_bearer_token(authorization)
    auth = user_store.validate_token(token)
    if not auth:
        raise HTTPException(status_code=401, detail="凭证无效或已过期")
    username, _, must_change = auth
    if must_change:
        raise HTTPException(status_code=403, detail="请先修改初始密码")
    return username
def _safe_doc_path(name: str) -> Path:
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = settings.docs_path / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if path.suffix.lower() not in ALLOWED_DOC_EXTS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    return path



def _require_user_allow_change(authorization: Optional[str] = Header(default=None)) -> str:
    """仅用于修改密码，允许仍在 must_change 状态的 token。"""

    token = _parse_bearer_token(authorization)
    auth = user_store.validate_token(token)
    if not auth:
        raise HTTPException(status_code=401, detail="凭证无效或已过期")
    username, _, _ = auth
    return username


@app.get("/health", response_model=HealthStatus)
def health() -> HealthStatus:
    try:
        docs_indexed, _ = vectorstore.stats()
    except FileNotFoundError:
        docs_indexed = 0
    return HealthStatus(status="ok", embedding_model=settings.embedding_model, docs_indexed=docs_indexed)


@app.post("/api/query", response_model=QueryResponse)
def query(req: QueryRequest, user: str = Depends(_require_user_active)) -> QueryResponse:
    start = time.perf_counter()
    req_id = uuid.uuid4().hex[:8]
    from_cache = False
    logger.info("[query:%s] start user=%s session=%s top_k=%s", req_id, user, req.session_id, req.top_k)
    try:
        resp, from_cache = orchestrator.handle(req)
        # persist conversation if session_id provided
        if req.session_id:
            session_store.add_message(req.session_id, "user", req.query)
            session_store.add_message(req.session_id, "assistant", resp.answer)
        latency_ms = (time.perf_counter() - start) * 1000
        metrics.record_query(latency_ms=latency_ms, cached=from_cache, used_tools=len(resp.used_tools))
        logger.info(
            "[query:%s] done latency_ms=%.1f cached=%s sources=%s tools=%s",
            req_id,
            latency_ms,
            from_cache,
            len(resp.sources) if hasattr(resp, "sources") else "-",
            len(resp.used_tools) if hasattr(resp, "used_tools") else "-",
        )
        return resp
    except FileNotFoundError as e:
        metrics.record_error()
        logger.exception("[query:%s] failed: %s", req_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/query/stream")
def query_stream(req: QueryRequest, user: str = Depends(_require_user_active)):
    start = time.perf_counter()
    req_id = uuid.uuid4().hex[:8]
    logger.info("[query-stream:%s] start user=%s session=%s top_k=%s", req_id, user, req.session_id, req.top_k)
    try:
        gen = orchestrator.handle_stream(req)
        def wrapped():
            for chunk in gen:
                yield chunk
            latency_ms = (time.perf_counter() - start) * 1000
            metrics.record_stream(latency_ms=latency_ms, count=False)
            logger.info("[query-stream:%s] done latency_ms=%.1f", req_id, latency_ms)

        # record start for streaming count even if client drops
        metrics.record_stream()
        return StreamingResponse(wrapped(), media_type="text/plain")
    except FileNotFoundError as e:
        metrics.record_error()
        logger.exception("[query-stream:%s] failed: %s", req_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/login")
def admin_login(username: str = Form(...), password: str = Form(...)) -> Dict[str, Union[str, bool]]:
    ok, token, must_change, role = user_store.authenticate(username, password)
    if not ok or not token or role != "admin":
        raise HTTPException(status_code=401, detail="用户名或密码错误，或无管理员权限")
    return {"token": token, "must_change_password": must_change, "role": role}


@app.post("/api/admin/users/batch_register")
def batch_register(student_ids: List[str] = Body(...), admin: Tuple[str, str] = Depends(_require_admin)) -> Dict[str, Union[List[str], str]]:
    if not student_ids:
        raise HTTPException(status_code=400, detail="student_ids 不能为空")
    created, skipped = user_store.batch_create(student_ids)
    return {"created": created, "skipped": skipped, "initial_password_rule": "hziee+学号"}


@app.post("/api/auth/login")
def user_login(username: str = Form(...), password: str = Form(...)) -> Dict[str, Union[str, bool]]:
    ok, token, must_change, role = user_store.authenticate(username, password)
    if not ok or not token:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": token, "must_change_password": must_change, "role": role or "student"}


@app.post("/api/auth/change_password")
def change_password(new_password: str = Form(...), user: str = Depends(_require_user_allow_change)):
    if not new_password or len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少6位")
    user_store.set_password(user, new_password)
    return {"status": "ok"}


@app.post("/api/docs/upload")
def upload_doc(file: UploadFile = File(...), admin: Tuple[str, str] = Depends(_require_admin)):
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_DOC_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")
    target_path = settings.docs_path / filename
    target_path.parent.mkdir(parents=True, exist_ok=True)
    content = file.file.read()
    target_path.write_bytes(content)

    # Rebuild index incrementally (simple full rebuild for MVP)
    chunks = load_documents(settings.docs_path, settings.chunk_size, settings.chunk_overlap, settings.chunk_overlap_ratio)
    vectorstore.build(chunks)
    metrics.record_doc_upload()

    return JSONResponse({"status": "ok", "saved": filename, "docs_count": len(chunks)})


@app.get("/api/admin/docs")
def list_docs(admin: Tuple[str, str] = Depends(_require_admin)) -> Dict[str, List[Dict[str, Any]]]:
    docs_dir = settings.docs_path
    docs_dir.mkdir(parents=True, exist_ok=True)
    items: List[Dict[str, Any]] = []
    for p in sorted(docs_dir.glob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in ALLOWED_DOC_EXTS:
            continue
        stat = p.stat()
        items.append({
            "name": p.name,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "ext": p.suffix.lower(),
        })
    return {"docs": items}


@app.get("/api/admin/docs/{name}", response_class=PlainTextResponse)
def get_doc_content(name: str, admin: Tuple[str, str] = Depends(_require_admin)):
    path = _safe_doc_path(name)
    content = _extract_text_for_preview(path)
    if not content:
        raise HTTPException(status_code=400, detail="无法读取文件内容")
    return content


@app.get("/api/docs/{name}", response_class=PlainTextResponse)
def get_doc_content_user(name: str, user: str = Depends(_require_user_active)):
    path = _safe_doc_path(name)
    content = _extract_text_for_preview(path)
    if not content:
        raise HTTPException(status_code=400, detail="无法读取文件内容")
    return content


@app.get("/api/docs/{name}/download")
def download_doc(name: str, user: str = Depends(_require_user_active)):
    path = _safe_doc_path(name)
    return FileResponse(path, filename=path.name)


@app.post("/api/session/new", response_model=SessionCreateResponse)
def new_session(user: str = Depends(_require_user_active)) -> SessionCreateResponse:
    import uuid

    session_id = str(uuid.uuid4())
    return SessionCreateResponse(session_id=session_id)


@app.get("/api/session/{session_id}/history", response_model=SessionHistoryResponse)
def session_history(session_id: str, user: str = Depends(_require_user_active)) -> SessionHistoryResponse:
    history = session_store.get_history(session_id)
    return SessionHistoryResponse(session_id=session_id, history=history)


@app.get("/metrics")
def metrics_snapshot():
    return metrics.snapshot()


@app.get("/")
def root() -> FileResponse:
    index_path = Path(__file__).parent / "static" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(index_path)


@app.get("/chat")
def chat_page() -> FileResponse:
    chat_path = Path(__file__).parent / "static" / "chat.html"
    if not chat_path.exists():
        raise HTTPException(status_code=404, detail="Chat UI not found")
    return FileResponse(chat_path)


@app.get("/admin")
def admin_login_page() -> FileResponse:
    login_path = Path(__file__).parent / "static" / "admin_login.html"
    if not login_path.exists():
        raise HTTPException(status_code=404, detail="Admin login UI not found")
    return FileResponse(login_path)


@app.get("/admin/dashboard")
def admin_ui() -> FileResponse:
    admin_path = Path(__file__).parent / "static" / "admin.html"
    if not admin_path.exists():
        raise HTTPException(status_code=404, detail="Admin UI not found")
    return FileResponse(admin_path)
