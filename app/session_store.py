from __future__ import annotations

import time
from typing import List

from pymongo import MongoClient, ASCENDING

from .models import SessionMessage

from .config import get_settings, Settings


class SessionStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = MongoClient(self.settings.mongo_uri)
        self.db = self.client[self.settings.mongo_db]
        self.col = self.db[self.settings.mongo_collection]
        self._init_db()

    def _init_db(self):
        self.col.create_index([("session_id", ASCENDING), ("created_at", ASCENDING)])

    def add_message(self, session_id: str, role: str, content: str):
        ts = time.time()
        self.col.insert_one({
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": ts,
        })

    def get_history(self, session_id: str, limit: int = 50) -> List[SessionMessage]:
        docs = list(
            self.col.find({"session_id": session_id}).sort("created_at", ASCENDING).limit(limit)
        )
        return [SessionMessage(role=d.get("role", "user"), content=d.get("content", ""), created_at=d.get("created_at", 0.0)) for d in docs]
