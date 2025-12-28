from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from .config import Settings, get_settings
from .models import SessionMessage, SessionSummary

MetaDict = Dict[str, Any]


class InMemorySessionStore:
    """简易内存实现，用于无 Mongo 时本地开发。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._messages: Dict[str, List[SessionMessage]] = {}
        self._sessions: Dict[str, MetaDict] = {}

    @staticmethod
    def _sanitize_title(title: Optional[str]) -> str:
        base = (title or "新的对话").strip()
        base = base or "新的对话"
        return base[:60]

    def create_session(self, user: str, title: Optional[str] = None, session_id: Optional[str] = None) -> MetaDict:
        ts = time.time()
        sid = session_id or uuid.uuid4().hex
        meta: MetaDict = {
            "session_id": sid,
            "user": user,
            "title": self._sanitize_title(title),
            "created_at": ts,
            "updated_at": ts,
            "message_count": 0,
            "last_message": "",
        }
        with self._lock:
            self._sessions[sid] = meta
            self._messages.setdefault(sid, [])
        return meta.copy()

    def list_sessions(self, user: str, limit: int = 50) -> List[MetaDict]:
        with self._lock:
            items = [meta.copy() for meta in self._sessions.values() if meta["user"] == user]
        items.sort(key=lambda m: m.get("updated_at", 0.0), reverse=True)
        return items[:limit]

    def rename_session(self, session_id: str, user: str, title: str) -> Optional[MetaDict]:
        title = self._sanitize_title(title)
        with self._lock:
            meta = self._sessions.get(session_id)
            if not meta or meta.get("user") != user:
                return None
            meta["title"] = title
            meta["updated_at"] = time.time()
            return meta.copy()

    def delete_session(self, session_id: str, user: str) -> bool:
        with self._lock:
            meta = self._sessions.get(session_id)
            if not meta or meta.get("user") != user:
                return False
            self._sessions.pop(session_id, None)
            self._messages.pop(session_id, None)
            return True

    def get_session(self, session_id: str) -> Optional[MetaDict]:
        with self._lock:
            meta = self._sessions.get(session_id)
            return meta.copy() if meta else None

    def add_message(self, session_id: str, role: str, content: str, user: Optional[str] = None):
        ts = time.time()
        with self._lock:
            self._messages.setdefault(session_id, []).append(
                SessionMessage(role=role, content=content, created_at=ts)
            )
            if session_id not in self._sessions and user:
                self.bootstrap_session(session_id, user)
            meta = self._sessions.get(session_id)
            if meta:
                meta["updated_at"] = ts
                meta["last_message"] = content
                meta["message_count"] = meta.get("message_count", 0) + 1

    def bootstrap_session(self, session_id: str, user: str) -> MetaDict:
        history = self._messages.get(session_id, [])
        ts = time.time()
        title_source = history[0].content if history else "历史会话"
        meta: MetaDict = {
            "session_id": session_id,
            "user": user,
            "title": self._sanitize_title(title_source[:20]),
            "created_at": history[0].created_at if history else ts,
            "updated_at": history[-1].created_at if history else ts,
            "message_count": len(history),
            "last_message": history[-1].content if history else "",
        }
        self._sessions[session_id] = meta
        return meta.copy()

    def get_history(self, session_id: str, limit: int = 50) -> List[SessionMessage]:
        with self._lock:
            msgs = self._messages.get(session_id, [])
            return msgs[-limit:]

    def save_meta(self, meta: MetaDict):
        with self._lock:
            self._sessions[meta["session_id"]] = meta.copy()
            self._messages.setdefault(meta["session_id"], [])


class SessionStore:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._backend = "memory"
        self._memory_fallback = InMemorySessionStore()
        self.client: Any = None
        self.db: Any = None
        self.col: Any = None
        self.meta_col: Any = None
        self._init_db()

    def _init_db(self):
        if not self.settings.mongo_uri:
            logging.warning("Mongo URI 为空，使用内存会话存储")
            return

        try:
            self.client = MongoClient(self.settings.mongo_uri, serverSelectionTimeoutMS=5000)
            self.db = self.client[self.settings.mongo_db]
            self.col = self.db[self.settings.mongo_collection]
            self.meta_col = self.db[f"{self.settings.mongo_collection}_meta"]
            self.col.create_index([("session_id", ASCENDING), ("created_at", ASCENDING)])
            self.meta_col.create_index("session_id", unique=True)
            self.meta_col.create_index([("user", ASCENDING), ("updated_at", DESCENDING)])
            self.client.admin.command("ping")
            self._backend = "mongo"
            logging.info("MongoDB 会话存储已初始化")
        except Exception as exc:
            logging.warning("Mongo 不可用，回退到内存会话存储: %s", exc)
            self.client = None
            self.db = None
            self.col = None
            self.meta_col = None
            self._backend = "memory"

    @staticmethod
    def _title_or_default(title: Optional[str]) -> str:
        base = (title or "新的对话").strip()
        base = base or "新的对话"
        return base[:60]

    def _meta_to_summary(self, meta: MetaDict) -> SessionSummary:
        return SessionSummary(
            session_id=meta["session_id"],
            title=meta.get("title", "新的对话"),
            last_message=meta.get("last_message"),
            created_at=float(meta.get("created_at", time.time())),
            updated_at=float(meta.get("updated_at", time.time())),
            message_count=int(meta.get("message_count", 0)),
        )

    def _build_meta_doc(
        self,
        user: str,
        title: Optional[str] = None,
        session_id: Optional[str] = None,
        last_message: str = "",
    ) -> MetaDict:
        ts = time.time()
        return {
            "session_id": session_id or uuid.uuid4().hex,
            "user": user,
            "title": self._title_or_default(title),
            "created_at": ts,
            "updated_at": ts,
            "message_count": 0,
            "last_message": last_message,
        }

    def create_session(self, user: str, title: Optional[str] = None) -> SessionSummary:
        if self._backend == "mongo" and self.meta_col is not None:
            doc = self._build_meta_doc(user, title)
            self.meta_col.insert_one(doc)
            return self._meta_to_summary(doc)
        meta = self._memory_fallback.create_session(user, title)
        return self._meta_to_summary(meta)

    def list_sessions(self, user: str, limit: int = 50) -> List[SessionSummary]:
        if self._backend == "mongo" and self.meta_col is not None:
            docs = list(
                self.meta_col.find({"user": user}).sort("updated_at", DESCENDING).limit(limit)
            )
            return [self._meta_to_summary(doc) for doc in docs]
        return [self._meta_to_summary(meta) for meta in self._memory_fallback.list_sessions(user, limit)]

    def rename_session(self, session_id: str, user: str, title: str) -> Optional[SessionSummary]:
        title = self._title_or_default(title)
        if self._backend == "mongo" and self.meta_col is not None:
            doc = self.meta_col.find_one_and_update(
                {"session_id": session_id, "user": user},
                {"$set": {"title": title, "updated_at": time.time()}},
                return_document=ReturnDocument.AFTER,
            )
            return self._meta_to_summary(doc) if doc else None
        meta = self._memory_fallback.rename_session(session_id, user, title)
        return self._meta_to_summary(meta) if meta else None

    def delete_session(self, session_id: str, user: str) -> bool:
        if self._backend == "mongo" and self.meta_col is not None and self.col is not None:
            meta_result = self.meta_col.delete_one({"session_id": session_id, "user": user})
            if meta_result.deleted_count:
                self.col.delete_many({"session_id": session_id})
                return True
            return False
        return self._memory_fallback.delete_session(session_id, user)

    def get_session(self, session_id: str) -> Optional[MetaDict]:
        if self._backend == "mongo" and self.meta_col is not None:
            doc = self.meta_col.find_one({"session_id": session_id})
            return doc
        return self._memory_fallback.get_session(session_id)

    def ensure_session_for_user(
        self,
        session_id: str,
        user: str,
        create_if_missing: bool = False,
    ) -> Optional[SessionSummary]:
        meta = self.get_session(session_id)
        if meta:
            owner = meta.get("user")
            if owner and owner != user:
                raise PermissionError("会话归属不匹配")
            if not owner:
                meta["user"] = user
                self._persist_meta(meta)
            return self._meta_to_summary(meta)
        if create_if_missing:
            boot = self._bootstrap_meta(session_id, user)
            return self._meta_to_summary(boot) if boot else None
        return None

    def add_message(self, session_id: str, role: str, content: str, user: Optional[str] = None):
        ts = time.time()
        if self._backend == "mongo" and self.col is not None:
            self.col.insert_one({
                "session_id": session_id,
                "role": role,
                "content": content,
                "created_at": ts,
            })
            self._update_session_meta(session_id, role, content, user, ts)
        else:
            self._memory_fallback.add_message(session_id, role, content, user=user)

    def get_history(self, session_id: str, limit: int = 50) -> List[SessionMessage]:
        if self._backend == "mongo" and self.col is not None:
            docs: List[Dict[str, Any]] = list(
                self.col.find({"session_id": session_id}).sort("created_at", ASCENDING).limit(limit)
            )
            return [
                SessionMessage(
                    role=d.get("role", "user"),
                    content=d.get("content", ""),
                    created_at=d.get("created_at", 0.0),
                )
                for d in docs
            ]
        return self._memory_fallback.get_history(session_id, limit)

    def _persist_meta(self, meta: MetaDict):
        if self._backend == "mongo" and self.meta_col is not None:
            self.meta_col.update_one(
                {"session_id": meta["session_id"]},
                {"$set": meta},
                upsert=True,
            )
        else:
            self._memory_fallback.save_meta(meta)

    def _update_session_meta(self, session_id: str, role: str, content: str, user: Optional[str], ts: float):
        if self.meta_col is None:
            return
        result = self.meta_col.update_one(
            {"session_id": session_id},
            {
                "$set": {"updated_at": ts, "last_message": content},
                "$inc": {"message_count": 1},
            },
        )
        if result.matched_count == 0 and user:
            self._bootstrap_meta(session_id, user, last_message=content, ts=ts)

    def _bootstrap_meta(
        self,
        session_id: str,
        user: str,
        last_message: str = "",
        ts: Optional[float] = None,
    ) -> Optional[MetaDict]:
        ts = ts or time.time()
        if self._backend == "mongo" and self.meta_col is not None and self.col is not None:
            count = self.col.count_documents({"session_id": session_id})
            first = self.col.find({"session_id": session_id}).sort("created_at", ASCENDING).limit(1)
            first_doc = next(iter(first), None)
            title_source = first_doc.get("content", "历史会话") if first_doc else "历史会话"
            created_at = first_doc.get("created_at", ts) if first_doc else ts
            last_doc = self.col.find({"session_id": session_id}).sort("created_at", DESCENDING).limit(1)
            last_doc_val = next(iter(last_doc), None)
            last_msg = last_doc_val.get("content", last_message) if last_doc_val else last_message
            meta: MetaDict = {
                "session_id": session_id,
                "user": user,
                "title": self._title_or_default(title_source[:20]),
                "created_at": created_at,
                "updated_at": ts,
                "message_count": count,
                "last_message": last_msg,
            }
            self.meta_col.update_one({"session_id": session_id}, {"$set": meta}, upsert=True)
            return meta
        return self._memory_fallback.bootstrap_session(session_id, user)
