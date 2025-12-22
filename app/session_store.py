from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List

from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from .config import Settings, get_settings
from .models import SessionMessage


class InMemorySessionStore:
    """简易内存实现，用于无 Mongo 时本地开发。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._messages: Dict[str, List[SessionMessage]] = {}

    def add_message(self, session_id: str, role: str, content: str):
        ts = time.time()
        with self._lock:
            self._messages.setdefault(session_id, []).append(
                SessionMessage(role=role, content=content, created_at=ts)
            )

    def get_history(self, session_id: str, limit: int = 50) -> List[SessionMessage]:
        with self._lock:
            msgs = self._messages.get(session_id, [])
            return msgs[-limit:]


class SessionStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._backend = "memory"
        self._memory_fallback = InMemorySessionStore()
        self.client = None
        self.db = None
        self.col = None
        self._init_db()

    def _init_db(self):
        # 若未配置 mongo_uri，直接使用内存
        if not self.settings.mongo_uri:
            logging.warning("Mongo URI 为空，使用内存会话存储")
            return

        try:
            self.client = MongoClient(self.settings.mongo_uri, serverSelectionTimeoutMS=5000)
            self.db = self.client[self.settings.mongo_db]
            self.col = self.db[self.settings.mongo_collection]
            self.col.create_index([("session_id", ASCENDING), ("created_at", ASCENDING)])
            # 触发一次连接检查
            self.client.admin.command("ping")
            self._backend = "mongo"
            logging.info("MongoDB 会话存储已初始化")
        except Exception as exc:
            logging.warning("Mongo 不可用，回退到内存会话存储: %s", exc)
            self.client = None
            self.db = None
            self.col = None
            self._backend = "memory"

    def add_message(self, session_id: str, role: str, content: str):
        ts = time.time()
        if self._backend == "mongo" and self.col is not None:
            self.col.insert_one({
                "session_id": session_id,
                "role": role,
                "content": content,
                "created_at": ts,
            })
        else:
            self._memory_fallback.add_message(session_id, role, content)

    def get_history(self, session_id: str, limit: int = 50) -> List[SessionMessage]:
        if self._backend == "mongo" and self.col is not None:
            docs: List[dict] = list(
                self.col.find({"session_id": session_id}).sort("created_at", ASCENDING).limit(limit)
            )
            return [SessionMessage(role=d.get("role", "user"), content=d.get("content", ""), created_at=d.get("created_at", 0.0)) for d in docs]
        return self._memory_fallback.get_history(session_id, limit)
