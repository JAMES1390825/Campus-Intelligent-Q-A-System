from __future__ import annotations

import io
import mimetypes
from pathlib import Path
import time
import uuid
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, List, Union, Tuple, Any, cast
import json

from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Depends, Form, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .agents import AgentOrchestrator
from .config import get_settings
from .models import (
    HealthStatus,
    QueryRequest,
    QueryResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionHistoryResponse,
    SessionListResponse,
    SessionSummary,
    SessionUpdateRequest,
    AdminUserRegisterRequest,
    DocumentChunk,
)
from .vectorstore import VectorStore
from .rag import ALLOWED_DOC_EXTS, load_documents
from .document_storage import DocumentStorage
from .session_store import SessionStore
from .metrics import Metrics
from .user_store import UserStore
from .redis_coord import RedisCoordinator

logger = logging.getLogger("campusqa.main")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

settings = get_settings()
orchestrator = AgentOrchestrator(settings)
vectorstore = VectorStore(settings)
session_store = SessionStore(settings)
metrics = Metrics()
user_store = UserStore(settings)
doc_storage = DocumentStorage(settings)
redis_coord = RedisCoordinator(settings)
ingest_executor = ThreadPoolExecutor(max_workers=max(1, settings.ingest_workers))
_vectorize_lock = threading.Lock()

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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_placeholder() -> Response:
    """Return an empty response so browsers stop logging missing favicon requests."""
    return Response(status_code=204)

def _guess_media_type(ext: str) -> str:
    normalized = (ext or "").lower()
    overrides = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".txt": "text/plain; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }
    if normalized in overrides:
        return overrides[normalized]
    if normalized and not normalized.startswith("."):
        normalized = f".{normalized}"
    return mimetypes.types_map.get(normalized, "application/octet-stream")


def _parse_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少凭证")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="凭证格式错误")
    return parts[1]


def _validate_admin_credentials(token: Optional[str]) -> Tuple[str, str]:
    if not token:
        raise HTTPException(status_code=401, detail="缺少凭证")
    auth = user_store.validate_token(token)
    if not auth:
        raise HTTPException(status_code=401, detail="凭证无效或已过期")
    username, role, _ = auth
    if role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return username, role


def _require_admin(authorization: Optional[str] = Header(default=None)) -> Tuple[str, str]:
    """管理员只校验 token & 角色，不强制首次改密。"""

    token = _parse_bearer_token(authorization)
    return _validate_admin_credentials(token)


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
def _load_doc_payload(name: str) -> Dict[str, Any]:
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="非法文件名")
    try:
        doc = doc_storage.get_document(name, include_content=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = str(doc.get("ext") or "").lower()
    if ext not in ALLOWED_DOC_EXTS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    return doc



def _require_user_allow_change(authorization: Optional[str] = Header(default=None)) -> str:
    """仅用于修改密码，允许仍在 must_change 状态的 token。"""

    token = _parse_bearer_token(authorization)
    auth = user_store.validate_token(token)
    if not auth:
        raise HTTPException(status_code=401, detail="凭证无效或已过期")
    username, _, _ = auth
    return username


def _record_ingest_event(doc_name: str, status: str, **meta: Any) -> None:
    payload: Dict[str, Any] = {"doc": doc_name, "status": status}
    if meta:
        payload.update(meta)
    redis_coord.record_event(payload)


def _run_vectorize_job(entries: List[Dict[str, Any]]) -> None:
    filenames = [cast(str, entry.get("filename")) for entry in entries if entry.get("filename")]
    if not filenames:
        return
    job_map = {cast(str, entry.get("filename")): entry.get("job_id") for entry in entries if entry.get("filename")}
    def _fail(doc_name: str, reason: str) -> None:
        job_id = job_map.get(doc_name)
        redis_coord.set_status(doc_name, "failed", {"job_id": job_id, "error": reason})
        _record_ingest_event(doc_name, "failed", job_id=job_id, error=reason)

    def _complete(doc_name: str, chunk_count: int, note: Optional[str] = None) -> None:
        job_id = job_map.get(doc_name)
        payload: Dict[str, Any] = {"chunk_count": chunk_count, "job_id": job_id}
        if note:
            payload["note"] = note
        redis_coord.set_status(doc_name, "completed", payload)
        _record_ingest_event(doc_name, "completed", job_id=job_id, chunk_count=chunk_count, note=note)

    requested_docs: Dict[str, Dict[str, Any]] = {}
    for name in filenames:
        try:
            doc = doc_storage.get_document(name, include_content=True)
        except FileNotFoundError:
            _fail(name, "文件不存在")
            continue
        ext = str(doc.get("ext") or "").lower()
        if ext not in ALLOWED_DOC_EXTS:
            _fail(name, f"不支持的文件类型: {ext or 'unknown'}")
            continue
        requested_docs[name] = doc

    if not requested_docs:
        return

    doc_chunk_map: Dict[str, List[DocumentChunk]] = {name: [] for name in requested_docs.keys()}
    try:
        with _vectorize_lock:
            docs_payload = list(requested_docs.values())
            chunks = load_documents(
                docs_payload,
                settings.chunk_size,
                settings.chunk_overlap,
                settings.chunk_overlap_ratio,
            )
            for chunk in chunks:
                if chunk.source in doc_chunk_map:
                    doc_chunk_map[chunk.source].append(chunk)
            upsert_payload = {name: chunk_list for name, chunk_list in doc_chunk_map.items() if chunk_list}
            if upsert_payload:
                vectorstore.upsert_documents(upsert_payload)
            empty_docs = [name for name, chunk_list in doc_chunk_map.items() if not chunk_list]
            if empty_docs:
                vectorstore.delete_documents(empty_docs)
            for doc_name, chunk_list in doc_chunk_map.items():
                doc_storage.update_vector_refs(doc_name, [chunk.id for chunk in chunk_list])
    except Exception as exc:  # pragma: no cover - background job should not crash server
        logger.exception("后台索引构建失败: %s", exc)
        for name in requested_docs.keys():
            _fail(name, "索引构建失败")
        return

    for name, chunk_list in doc_chunk_map.items():
        chunk_count = len(chunk_list)
        if chunk_count > 0:
            metrics.record_doc_upload()
            _complete(name, chunk_count)
        else:
            _complete(name, 0, note="no_chunks")


def _schedule_vectorize(entries: List[Dict[str, Any]]) -> None:
    if not entries:
        return
    payload = [dict(item) for item in entries]
    try:
        ingest_executor.submit(_run_vectorize_job, payload)
    except Exception as exc:  # pragma: no cover - fallback to inline for reliability
        logger.warning("提交后台索引任务失败，改为同步执行: %s", exc)
        _run_vectorize_job(payload)


def _safe_vectorstore_stats() -> Tuple[int, int]:
    try:
        return vectorstore.stats()
    except Exception as exc:
        logger.warning("无法获取向量索引统计: %s", exc)
        return 0, 0


@app.get("/health", response_model=HealthStatus)
def health() -> HealthStatus:
    docs_indexed, _ = _safe_vectorstore_stats()
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
            session_store.ensure_session_for_user(req.session_id, user, create_if_missing=True)
            session_store.add_message(req.session_id, "user", req.query, user=user)
            session_store.add_message(req.session_id, "assistant", resp.answer, user=user)
        latency_ms = (time.perf_counter() - start) * 1000
        metrics.record_query(latency_ms=latency_ms, cached=from_cache)
        logger.info(
            "[query:%s] done latency_ms=%.1f cached=%s sources=%s",
            req_id,
            latency_ms,
            from_cache,
            len(resp.sources) if hasattr(resp, "sources") else "-",
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
        answer_parts: List[str] = []
        meta_payload: Dict[str, Any] = {}

        def wrapped():
            nonlocal meta_payload
            try:
                for chunk in gen:
                    if chunk.startswith("__META__"):
                        try:
                            meta_payload = json.loads(chunk[len("__META__"):])
                        except Exception:
                            logger.warning("[query-stream:%s] failed to parse meta chunk", req_id)
                        yield chunk
                        continue
                    answer_parts.append(chunk)
                    yield chunk
            finally:
                latency_ms = (time.perf_counter() - start) * 1000
                metrics.record_stream(latency_ms=latency_ms, count=False)
                logger.info("[query-stream:%s] done latency_ms=%.1f", req_id, latency_ms)
                _persist_stream_history(req, user, answer_parts, meta_payload)

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
@app.post("/api/admin/users/register")
def register_user(req: AdminUserRegisterRequest, admin: Tuple[str, str] = Depends(_require_admin)) -> Dict[str, Union[str, bool]]:
    student_id = (req.student_id or "").strip()
    if not student_id:
        raise HTTPException(status_code=400, detail="student_id 不能为空")
    password = (req.password or "").strip()
    if not password:
        password = f"hziee{student_id}"
    created = user_store.create_user(student_id, password, must_change=True, role="student")
    if not created:
        raise HTTPException(status_code=409, detail="该用户已存在")
    return {"username": student_id, "initial_password": password, "must_change_password": True}


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
def upload_docs(files: List[UploadFile] = File(...), admin: Tuple[str, str] = Depends(_require_admin)):
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")

    admin_user, _ = admin
    successes: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    existing_docs = doc_storage.list_documents()
    existing_hashes: Dict[str, Dict[str, Any]] = {
        cast(str, entry["hash"]): entry for entry in existing_docs if entry.get("hash")
    }
    batch_hashes: set[str] = set()
    max_bytes = max(0, settings.max_upload_mb) * 1024 * 1024 if settings.max_upload_mb else 0

    for upload in files:
        filename = (upload.filename or "").strip()
        if not filename:
            failures.append({"filename": "(未命名)", "reason": "文件名为空"})
            continue
        filename = Path(filename).name  # 防止路径穿越
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_DOC_EXTS:
            failures.append({"filename": filename, "reason": f"不支持的文件类型: {ext}"})
            continue

        content = upload.file.read()
        if max_bytes and len(content) > max_bytes:
            failures.append({"filename": filename, "reason": f"文件超过大小限制 {settings.max_upload_mb}MB"})
            continue

        file_hash = doc_storage.compute_hash_from_bytes(content)
        if file_hash in batch_hashes:
            failures.append({"filename": filename, "reason": "与本次上传的其他文件内容相同，已跳过"})
            continue

        existing_entry = existing_hashes.get(file_hash)
        if existing_entry:
            existing_name = existing_entry.get("name") or existing_entry.get("filename") or "已存在文件"
            failures.append({"filename": filename, "reason": f"内容与 {existing_name} 重复"})
            continue

        batch_hashes.add(file_hash)

        job_id = uuid.uuid4().hex
        redis_coord.enqueue(
            "doc_ingest",
            {"job_id": job_id, "filename": filename, "admin": admin_user, "size": len(content), "ts": time.time()},
        )
        redis_coord.set_status(filename, "pending", {"job_id": job_id})
        _record_ingest_event(filename, "pending", job_id=job_id, admin=admin_user, size=len(content), hash=file_hash)

        coord_enabled = redis_coord.enabled
        with redis_coord.lock(f"doc-ingest:{filename}", ttl=600, wait_timeout=5) as lock_handle:
            if coord_enabled and lock_handle is None:
                redis_coord.set_status(filename, "busy", {"job_id": job_id})
                failures.append({"filename": filename, "reason": "文档正在处理中"})
                _record_ingest_event(filename, "busy", job_id=job_id, reason="locked")
                continue
            try:
                redis_coord.set_status(filename, "uploading", {"size": len(content)})
                _record_ingest_event(filename, "uploading", job_id=job_id, size=len(content))
                doc_storage.save(filename, content, uploaded_by=admin_user)

                redis_coord.set_status(filename, "vectorizing", {"job_id": job_id})
                _record_ingest_event(filename, "vectorizing", job_id=job_id)
                successes.append({"filename": filename, "job_id": job_id, "hash": file_hash})
                existing_hashes[file_hash] = {"name": filename}
            except HTTPException as exc:
                redis_coord.set_status(filename, "failed", {"job_id": job_id, "error": exc.detail})
                failures.append({"filename": filename, "reason": exc.detail})
                _record_ingest_event(filename, "failed", job_id=job_id, error=exc.detail)
            except Exception as exc:
                redis_coord.set_status(filename, "failed", {"job_id": job_id, "error": str(exc)})
                failures.append({"filename": filename, "reason": "处理失败"})
                logger.exception("上传文件失败: %s", exc)
                _record_ingest_event(filename, "failed", job_id=job_id, error=str(exc))

    vectorization_info: Dict[str, Any] = {
        "mode": "idle",
        "scheduled": False,
        "pending": [],
        "auto_vectorize": True,
    }
    if successes:
        pending_files = [entry["filename"] for entry in successes]
        vectorization_info.update(
            {
                "mode": "async",
                "scheduled": True,
                "pending": pending_files,
            }
        )
        _schedule_vectorize(successes)

    docs_count, _ = _safe_vectorstore_stats()

    overall_status = "ok"
    if failures and successes:
        overall_status = "partial"
    elif failures and not successes:
        overall_status = "failed"

    return JSONResponse(
        {
            "status": overall_status,
            "processed": [entry["filename"] for entry in successes],
            "failed": failures,
            "docs_count": docs_count,
            "vectorization": vectorization_info,
        }
    )


@app.delete("/api/admin/docs/{name}")
def delete_doc(name: str, admin: Tuple[str, str] = Depends(_require_admin)) -> Dict[str, Any]:
    try:
        doc_storage.delete(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="文件不存在")
    except Exception as exc:
        logger.exception("删除文档失败: %s", exc)
        raise HTTPException(status_code=500, detail="删除失败")
    vectors_removed = 0
    vectorized = False
    try:
        vectors_removed = vectorstore.delete_documents([name])
        vectorized = True
    except Exception as exc:
        logger.warning("清理向量失败: %s", exc)

    docs_count, _ = _safe_vectorstore_stats()
    return {
        "status": "deleted",
        "name": name,
        "docs_count": docs_count,
        "vectors_removed": vectors_removed,
        "vectorized": vectorized,
    }


@app.post("/api/admin/docs/{name}/reindex")
def reindex_doc(name: str, admin: Tuple[str, str] = Depends(_require_admin)) -> Dict[str, Any]:
    admin_user, _ = admin
    doc = _load_doc_payload(name)
    job_id = uuid.uuid4().hex
    filename = str(doc.get("name") or name)
    entry: Dict[str, Any] = {"filename": filename, "job_id": job_id, "admin": admin_user, "manual": True}
    redis_coord.set_status(filename, "vectorizing", {"job_id": job_id, "note": "手动重建"})
    _record_ingest_event(filename, "vectorizing", job_id=job_id, admin=admin_user, note="manual_reindex")
    _schedule_vectorize([entry])
    return {"status": "scheduled", "job_id": job_id}


@app.get("/api/admin/docs")
def list_docs(admin: Tuple[str, str] = Depends(_require_admin)) -> Dict[str, List[Dict[str, Any]]]:
    items: List[Dict[str, Any]] = []
    statuses = redis_coord.list_statuses() if redis_coord.enabled else {}
    for entry in doc_storage.list_documents():
        ext = str(entry.get("ext") or "").lower()
        if ext and ext not in ALLOWED_DOC_EXTS:
            continue
        name = str(entry.get("name") or "")
        status_meta = statuses.get(name, {})
        entry_payload: Dict[str, Any] = dict(entry)
        entry_payload.update({"status": status_meta.get("status", "unknown"), "status_meta": status_meta})
        items.append(entry_payload)
    return {"docs": items}


@app.get("/api/admin/docs/{name}/download")
def download_doc_admin(name: str, admin: Tuple[str, str] = Depends(_require_admin)):
    doc = _load_doc_payload(name)
    content = doc.get("content") or b""
    filename = doc.get("name") or name
    ext = str(doc.get("ext") or Path(filename).suffix).lower()
    media_type = _guess_media_type(ext)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(io.BytesIO(bytes(content)), media_type=media_type, headers=headers)


@app.get("/api/admin/overview")
def admin_overview(admin: Tuple[str, str] = Depends(_require_admin)) -> Dict[str, Any]:
    metrics_snapshot = metrics.snapshot()
    docs_indexed, vectors_indexed = _safe_vectorstore_stats()

    statuses = redis_coord.list_statuses() if redis_coord.enabled else {}
    docs: List[Dict[str, Any]] = []
    for entry in doc_storage.list_documents():
        ext = str(entry.get("ext") or "").lower()
        if ext and ext not in ALLOWED_DOC_EXTS:
            continue
        name = str(entry.get("name") or "")
        status_meta = statuses.get(name, {})
        entry_payload: Dict[str, Any] = dict(entry)
        entry_payload.update({"status": status_meta.get("status", "unknown"), "status_meta": status_meta})
        docs.append(entry_payload)

    redis_snapshot = redis_coord.snapshot()
    recent_statuses: List[Dict[str, Any]] = []
    raw_statuses = redis_snapshot.get("statuses")
    typed_statuses: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_statuses, dict):
        for doc_name, payload in cast(Dict[Any, Any], raw_statuses).items():
            if not isinstance(payload, dict):
                continue
            payload_dict = dict(cast(Dict[str, Any], payload))
            typed_statuses[str(doc_name)] = payload_dict
    for doc_name, payload in typed_statuses.items():
        payload.setdefault("doc", doc_name)
        recent_statuses.append(payload)
    recent_statuses.sort(key=lambda item: float(item.get("ts", 0)), reverse=True)
    history = redis_snapshot.get("history")
    recent_history: List[Dict[str, Any]] = []
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                recent_history.append(cast(Dict[str, Any], item))

    return {
        "metrics": metrics_snapshot,
        "vectorstore": {
            "docs_indexed": docs_indexed,
            "vectors_indexed": vectors_indexed,
            "auto_vectorize": True,
        },
        "docs": docs,
        "docs_count": len(docs),
        "redis": redis_snapshot,
        "recent_statuses": recent_statuses[:20],
        "recent_history": recent_history[:40],
        "upload_limits": {
            "max_mb": settings.max_upload_mb,
            "allowed_exts": sorted(ALLOWED_DOC_EXTS),
        },
    }


@app.post("/api/admin/test_query", response_model=QueryResponse)
def admin_test_query(req: QueryRequest, admin: Tuple[str, str] = Depends(_require_admin)) -> QueryResponse:
    admin_user, _ = admin
    start = time.perf_counter()
    req_id = uuid.uuid4().hex[:8]
    logger.info("[admin-test:%s] start admin=%s top_k=%s", req_id, admin_user, req.top_k)
    try:
        resp, from_cache = orchestrator.handle(req)
        latency_ms = (time.perf_counter() - start) * 1000
        metrics.record_query(latency_ms=latency_ms, cached=from_cache)
        logger.info("[admin-test:%s] done latency_ms=%.1f cached=%s", req_id, latency_ms, from_cache)
        return resp
    except FileNotFoundError as exc:
        metrics.record_error()
        logger.exception("[admin-test:%s] failed: %s", req_id, exc)
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/session/new", response_model=SessionCreateResponse)
def new_session(
    payload: Optional[SessionCreateRequest] = None,
    user: str = Depends(_require_user_active),
) -> SessionCreateResponse:
    title = (payload.title.strip() if payload and payload.title else None)
    summary = session_store.create_session(user, title=title)
    return SessionCreateResponse(
        session_id=summary.session_id,
        title=summary.title,
        created_at=summary.created_at,
    )


@app.get("/api/session", response_model=SessionListResponse)
def list_sessions(user: str = Depends(_require_user_active)) -> SessionListResponse:
    sessions = session_store.list_sessions(user, limit=50)
    return SessionListResponse(sessions=sessions)


@app.patch("/api/session/{session_id}", response_model=SessionSummary)
def rename_session_endpoint(
    session_id: str,
    payload: SessionUpdateRequest,
    user: str = Depends(_require_user_active),
) -> SessionSummary:
    summary = session_store.rename_session(session_id, user, payload.title)
    if not summary:
        raise HTTPException(status_code=404, detail="会话不存在或无权操作")
    return summary


@app.delete("/api/session/{session_id}")
def delete_session_endpoint(session_id: str, user: str = Depends(_require_user_active)) -> Dict[str, str]:
    ok = session_store.delete_session(session_id, user)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或无权删除")
    return {"status": "deleted"}


@app.get("/api/session/{session_id}/history", response_model=SessionHistoryResponse)
def session_history(session_id: str, user: str = Depends(_require_user_active)) -> SessionHistoryResponse:
    summary = session_store.ensure_session_for_user(session_id, user, create_if_missing=True)
    history = session_store.get_history(session_id)
    return SessionHistoryResponse(
        session_id=session_id,
        title=summary.title if summary else None,
        history=history,
    )


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


@app.get("/change-password")
def change_password_page() -> FileResponse:
    page_path = Path(__file__).parent / "static" / "change_password.html"
    if not page_path.exists():
        raise HTTPException(status_code=404, detail="Change password UI not found")
    return FileResponse(page_path)


def _persist_stream_history(req: QueryRequest, user: str, answer_parts: List[str], meta_payload: Dict[str, Any]):
    if not req.session_id:
        return
    _ = meta_payload  # reserved for future enrichment (e.g., sources)
    final_answer = "".join(answer_parts).strip()
    user_text = req.query or ""
    if not user_text and not final_answer:
        return
    try:
        session_store.ensure_session_for_user(req.session_id, user, create_if_missing=True)
        if user_text:
            session_store.add_message(req.session_id, "user", user_text, user=user)
        if final_answer:
            session_store.add_message(req.session_id, "assistant", final_answer, user=user)
    except Exception as exc:  # pragma: no cover - persistence failure shouldn't break stream
        logger.warning("[query-stream] failed to persist history for session=%s: %s", req.session_id, exc)
