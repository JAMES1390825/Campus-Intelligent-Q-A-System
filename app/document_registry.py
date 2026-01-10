from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from .config import Settings, get_settings


logger = logging.getLogger(__name__)

DocRecord = Dict[str, Any]


def _now() -> float:
    return time.time()


class InMemoryDocumentRegistry:
    def __init__(self):
        self._docs: Dict[str, DocRecord] = {}

    def _ensure_doc(self, name: str) -> DocRecord:
        doc = self._docs.get(name)
        if not doc:
            raise FileNotFoundError(f"Document {name} not registered")
        return doc

    def record_upload(
        self,
        *,
        name: str,
        size: int,
        ext: str,
        oss_key: str,
        local_path: str,
        uploaded_by: str,
    ) -> DocRecord:
        ts = _now()
        doc = self._docs.get(name) or {
            "doc_id": uuid.uuid4().hex,
            "name": name,
            "created_at": ts,
        }
        doc.update(
            {
                "size": size,
                "ext": ext,
                "oss_key": oss_key,
                "local_path": local_path,
                "uploaded_by": uploaded_by,
                "status": "uploaded",
                "chunk_count": doc.get("chunk_count", 0),
                "updated_at": ts,
            }
        )
        self._docs[name] = doc
        return doc.copy()

    def update_status(
        self,
        name: str,
        *,
        status: Optional[str] = None,
        chunk_count: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> DocRecord:
        doc = self._ensure_doc(name)
        if status:
            doc["status"] = status
        if chunk_count is not None:
            doc["chunk_count"] = chunk_count
        if extra:
            doc.update(extra)
        doc["updated_at"] = _now()
        return doc.copy()

    def delete(self, name: str) -> bool:
        return self._docs.pop(name, None) is not None

    def list_docs(self) -> List[DocRecord]:
        return sorted((doc.copy() for doc in self._docs.values()), key=lambda d: d.get("updated_at", 0), reverse=True)

    def get(self, name: str) -> Optional[DocRecord]:
        doc = self._docs.get(name)
        return doc.copy() if doc else None


class DocumentRegistry:
    """Persists document metadata/status similar to FastGPT pipeline."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._backend = "memory"
        self._memory = InMemoryDocumentRegistry()
        self.client = None
        self.collection = None
        self._init_db()

    def _init_db(self):
        if not self.settings.mongo_uri:
            logger.warning("Mongo URI missing; document registry uses in-memory fallback")
            return
        try:
            self.client = MongoClient(self.settings.mongo_uri, serverSelectionTimeoutMS=4000)
            self.collection = self.client[self.settings.mongo_db]["documents"]
            self.collection.create_index("name", unique=True)
            self.collection.create_index([("updated_at", DESCENDING)])
            self.collection.create_index([("status", ASCENDING)])
            self.client.admin.command("ping")
            self._backend = "mongo"
            logger.info("Document registry initialized with MongoDB backend")
        except Exception as exc:
            logger.warning("Mongo unavailable for document registry, using memory store: %s", exc)
            self.client = None
            self.collection = None
            self._backend = "memory"

    def _public_doc(self, doc: DocRecord) -> DocRecord:
        payload = {
            "doc_id": doc.get("doc_id"),
            "name": doc.get("name"),
            "size": doc.get("size", 0),
            "ext": doc.get("ext"),
            "oss_key": doc.get("oss_key"),
            "local_path": doc.get("local_path"),
            "status": doc.get("status", "uploaded"),
            "chunk_count": doc.get("chunk_count", 0),
            "uploaded_by": doc.get("uploaded_by"),
            "created_at": float(doc.get("created_at", 0)),
            "updated_at": float(doc.get("updated_at", 0)),
        }
        return payload

    def record_upload(
        self,
        *,
        name: str,
        size: int,
        ext: str,
        oss_key: str,
        local_path: str,
        uploaded_by: str,
    ) -> DocRecord:
        if self._backend == "mongo" and self.collection is not None:
            ts = _now()
            doc_id = uuid.uuid4().hex
            update = {
                "$setOnInsert": {
                    "doc_id": doc_id,
                    "created_at": ts,
                },
                "$set": {
                    "name": name,
                    "size": size,
                    "ext": ext,
                    "oss_key": oss_key,
                    "local_path": local_path,
                    "uploaded_by": uploaded_by,
                    "status": "uploaded",
                    "updated_at": ts,
                },
            }
            result = self.collection.find_one_and_update(
                {"name": name},
                update,
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return self._public_doc(result)
        doc = self._memory.record_upload(
            name=name,
            size=size,
            ext=ext,
            oss_key=oss_key,
            local_path=local_path,
            uploaded_by=uploaded_by,
        )
        return self._public_doc(doc)

    def update_status(
        self,
        name: str,
        *,
        status: Optional[str] = None,
        chunk_count: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> DocRecord:
        if self._backend == "mongo" and self.collection is not None:
            ts = _now()
            update_set: Dict[str, Any] = {"updated_at": ts}
            if status:
                update_set["status"] = status
            if chunk_count is not None:
                update_set["chunk_count"] = chunk_count
            if extra:
                update_set.update(extra)
            result = self.collection.find_one_and_update(
                {"name": name},
                {"$set": update_set},
                return_document=ReturnDocument.AFTER,
            )
            if not result:
                raise FileNotFoundError(f"Document {name} not registered")
            return self._public_doc(result)
        doc = self._memory.update_status(name, status=status, chunk_count=chunk_count, extra=extra)
        return self._public_doc(doc)

    def delete(self, name: str) -> bool:
        if self._backend == "mongo" and self.collection is not None:
            result = self.collection.delete_one({"name": name})
            return bool(result.deleted_count)
        return self._memory.delete(name)

    def get(self, name: str) -> Optional[DocRecord]:
        if self._backend == "mongo" and self.collection is not None:
            doc = self.collection.find_one({"name": name})
            return self._public_doc(doc) if doc else None
        doc = self._memory.get(name)
        return self._public_doc(doc) if doc else None

    def list_docs(self) -> List[DocRecord]:
        if self._backend == "mongo" and self.collection is not None:
            docs = list(self.collection.find({}).sort("updated_at", DESCENDING))
            return [self._public_doc(doc) for doc in docs]
        return [self._public_doc(doc) for doc in self._memory.list_docs()]
