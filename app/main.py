from __future__ import annotations

from pathlib import Path
import time
from typing import Optional, Dict, List, Union, Tuple

from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Depends, Form, Body
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
from .session_store import SessionStore
from .metrics import Metrics
from .user_store import UserStore

settings = get_settings()
orchestrator = AgentOrchestrator(settings)
vectorstore = VectorStore(settings)
session_store = SessionStore(settings)
metrics = Metrics()
user_store = UserStore(settings)

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


def _parse_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少凭证")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="凭证格式错误")
    return parts[1]


def _require_admin(authorization: Optional[str] = Header(default=None)) -> Tuple[str, str]:
    token = _parse_bearer_token(authorization)
    auth = user_store.validate_token(token)
    if not auth:
        raise HTTPException(status_code=401, detail="凭证无效或已过期")
    username, role = auth
    if role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return username, role


def _require_user(authorization: Optional[str] = Header(default=None)) -> str:
    token = _parse_bearer_token(authorization)
    auth = user_store.validate_token(token)
    if not auth:
        raise HTTPException(status_code=401, detail="凭证无效或已过期")
    username, _ = auth
    return username


@app.get("/health", response_model=HealthStatus)
def health() -> HealthStatus:
    try:
        docs_indexed, _ = vectorstore.stats()
    except FileNotFoundError:
        docs_indexed = 0
    return HealthStatus(status="ok", embedding_model=settings.embedding_model, docs_indexed=docs_indexed)


@app.post("/api/query", response_model=QueryResponse)
def query(req: QueryRequest, user: str = Depends(_require_user)) -> QueryResponse:
    start = time.perf_counter()
    from_cache = False
    try:
        resp, from_cache = orchestrator.handle(req)
        # persist conversation if session_id provided
        if req.session_id:
            session_store.add_message(req.session_id, "user", req.query)
            session_store.add_message(req.session_id, "assistant", resp.answer)
        latency_ms = (time.perf_counter() - start) * 1000
        metrics.record_query(latency_ms=latency_ms, cached=from_cache, used_tools=len(resp.used_tools))
        return resp
    except FileNotFoundError as e:
        metrics.record_error()
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/query/stream")
def query_stream(req: QueryRequest, user: str = Depends(_require_user)):
    start = time.perf_counter()
    try:
        gen = orchestrator.handle_stream(req)
        def wrapped():
            for chunk in gen:
                yield chunk
            latency_ms = (time.perf_counter() - start) * 1000
            metrics.record_stream(latency_ms=latency_ms, count=False)

        # record start for streaming count even if client drops
        metrics.record_stream()
        return StreamingResponse(wrapped(), media_type="text/plain")
    except FileNotFoundError as e:
        metrics.record_error()
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
def change_password(new_password: str = Form(...), user: str = Depends(_require_user)):
    if not new_password or len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少6位")
    user_store.set_password(user, new_password)
    return {"status": "ok"}


@app.post("/api/docs/upload")
def upload_doc(file: UploadFile = File(...), admin: Tuple[str, str] = Depends(_require_admin)):
    filename = file.filename or ""
    if not filename.endswith(".txt"):
        raise HTTPException(status_code=400, detail="仅支持 .txt 文本上传")
    target_path = settings.docs_path / filename
    target_path.parent.mkdir(parents=True, exist_ok=True)
    content = file.file.read()
    target_path.write_bytes(content)

    # Rebuild index incrementally (simple full rebuild for MVP)
    chunks = load_documents(settings.docs_path, settings.chunk_size, settings.chunk_overlap, settings.chunk_overlap_ratio)
    vectorstore.build(chunks)
    metrics.record_doc_upload()

    return JSONResponse({"status": "ok", "saved": filename, "docs_count": len(chunks)})


@app.post("/api/session/new", response_model=SessionCreateResponse)
def new_session(user: str = Depends(_require_user)) -> SessionCreateResponse:
    import uuid

    session_id = str(uuid.uuid4())
    return SessionCreateResponse(session_id=session_id)


@app.get("/api/session/{session_id}/history", response_model=SessionHistoryResponse)
def session_history(session_id: str, user: str = Depends(_require_user)) -> SessionHistoryResponse:
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
def admin_ui() -> FileResponse:
    admin_path = Path(__file__).parent / "static" / "admin.html"
    if not admin_path.exists():
        raise HTTPException(status_code=404, detail="Admin UI not found")
    return FileResponse(admin_path)
