from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

import pymysql

try:
    import oss2  # type: ignore
except Exception:  # pragma: no cover - optional dependency for dev environments without oss2
    oss2 = None  # type: ignore

from .config import Settings, get_settings

logger = logging.getLogger("campusqa.docstore")


class DocumentStorage:
    """Store raw files in object storage and persist metadata/vector refs in MySQL."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.table = self.settings.mysql_docs_table
        self._backend = "mysql"
        self._memory_meta: Dict[str, Dict[str, Any]] = {}
        self._memory_objects: Dict[str, bytes] = {}
        self._use_oss = False
        self._oss = None
        self._bucket = None
        self._object_backend = "memory"
        self.prefix = (self.settings.oss_prefix or "docs").strip("/")
        try:
            self._init_db()
        except Exception as exc:  # pragma: no cover - fallback for local dev
            logger.warning("MySQL 文档元数据不可用，回退到内存: %s", exc)
            self._backend = "memory"
        self._init_object_store()

    # ------------------------------------------------------------------
    def _init_object_store(self):
        required = [
            self.settings.oss_bucket,
            self.settings.oss_endpoint,
            self.settings.oss_access_key_id,
            self.settings.oss_access_key_secret,
        ]
        if all(required) and oss2 is not None and self.settings.use_oss_storage:
            endpoint = self.settings.oss_internal_endpoint or self.settings.oss_endpoint
            auth = oss2.Auth(self.settings.oss_access_key_id, self.settings.oss_access_key_secret)  # type: ignore[attr-defined]
            self._bucket = oss2.Bucket(auth, endpoint, self.settings.oss_bucket)  # type: ignore[attr-defined]
            self._oss = oss2
            self._use_oss = True
            self._object_backend = "oss"
            logger.info(
                "OSS storage enabled bucket=%s prefix=%s", self.settings.oss_bucket, self.prefix or "(root)"
            )
        else:
            self._object_backend = "memory"
            logger.warning("OSS 未配置或依赖缺失，使用内存对象存储（仅供开发/测试）")

    # ------------------------------------------------------------------
    def _connect(self):
        if self._backend != "mysql":
            raise RuntimeError("MySQL backend unavailable")
        return pymysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password or "",
            database=self.settings.mysql_db,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=3,
            read_timeout=3,
            write_timeout=3,
        )

    def _init_db(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                name VARCHAR(255) PRIMARY KEY,
                size BIGINT,
                ext VARCHAR(32),
                oss_key VARCHAR(512),
                hash CHAR(64),
                uploaded_by VARCHAR(128),
                vector_refs TEXT,
                created_at DOUBLE,
                updated_at DOUBLE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        conn.commit()
        try:
            cur.execute(f"CREATE INDEX idx_{self.table}_updated_at ON {self.table}(updated_at DESC)")
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def _ensure_metadata(self, name: str) -> Dict[str, Any]:
        if self._backend == "memory":
            doc = self._memory_meta.get(name)
            if not doc:
                raise FileNotFoundError(f"Document {name} not found")
            return doc
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"SELECT name,size,ext,oss_key,hash,uploaded_by,vector_refs,created_at,updated_at FROM {self.table} WHERE name=%s",
            (name,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            raise FileNotFoundError(f"Document {name} not found")
        name, size, ext, oss_key, digest, uploaded_by, vector_refs, created_at, updated_at = row
        meta = {
            "name": name,
            "size": int(size or 0),
            "ext": (ext or "").lower(),
            "oss_key": oss_key,
            "hash": digest,
            "uploaded_by": uploaded_by,
            "vector_refs": self._deserialize_vector_refs(vector_refs),
            "created_at": float(created_at or 0),
            "updated_at": float(updated_at or 0),
        }
        return meta

    def _object_key(self, safe_name: str) -> str:
        uid = uuid.uuid4().hex[:8]
        key = f"{safe_name}-{uid}"
        if self.prefix:
            return f"{self.prefix}/{key}"
        return key

    def _store_object(self, key: str, data: bytes) -> None:
        if self._object_backend == "oss" and self._bucket is not None:
            self._bucket.put_object(key, data)
            return
        self._memory_objects[key] = data

    def _fetch_object(self, key: str) -> bytes:
        if self._object_backend == "oss" and self._bucket is not None:
            result = self._bucket.get_object(key)
            return result.read()  # type: ignore[attr-defined]
        data = self._memory_objects.get(key)
        if data is None:
            raise FileNotFoundError(f"Object {key} not found in fallback store")
        return data

    def _delete_object(self, key: str) -> None:
        if self._object_backend == "oss" and self._bucket is not None:
            try:
                self._bucket.delete_object(key)
            except Exception:  # pragma: no cover
                logger.warning("删除 OSS 对象失败: %s", key)
            return
        self._memory_objects.pop(key, None)

    def save(self, name: str, data: bytes, uploaded_by: Optional[str] = None) -> Dict[str, Any]:
        safe_name = self._sanitize(name)
        ext = safe_name[safe_name.rfind(".") :].lower() if "." in safe_name else ""
        digest = self.compute_hash_from_bytes(data)
        size = len(data)
        now = time.time()
        key = self._object_key(safe_name)
        self._store_object(key, data)
        payload = {
            "name": safe_name,
            "size": size,
            "ext": ext,
            "oss_key": key,
            "hash": digest,
            "uploaded_by": uploaded_by,
            "vector_refs": [],
            "created_at": now,
            "updated_at": now,
        }
        if self._backend == "memory":
            payload_copy = dict(payload)
            payload_copy["content"] = data
            self._memory_meta[safe_name] = payload_copy
            return payload

        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO {self.table} (name, size, ext, oss_key, hash, uploaded_by, vector_refs, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                size=VALUES(size),
                ext=VALUES(ext),
                oss_key=VALUES(oss_key),
                hash=VALUES(hash),
                uploaded_by=VALUES(uploaded_by),
                vector_refs=VALUES(vector_refs),
                updated_at=VALUES(updated_at)
            """,
            (
                safe_name,
                size,
                ext,
                key,
                digest,
                uploaded_by,
                json.dumps([], ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
        conn.close()
        return payload

    def list_documents(self) -> List[Dict[str, Any]]:
        if self._backend == "memory":
            items: List[Dict[str, Any]] = []
            for doc in self._memory_meta.values():
                meta = {k: v for k, v in doc.items() if k != "content"}
                meta.setdefault("vector_refs", meta.get("vector_refs", []))
                meta["vector_count"] = len(meta.get("vector_refs", []) or [])
                items.append(meta)
            return sorted(items, key=lambda item: float(item.get("updated_at", 0)), reverse=True)

        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"SELECT name,size,ext,oss_key,hash,uploaded_by,vector_refs,created_at,updated_at FROM {self.table} ORDER BY updated_at DESC"
        )
        rows = cur.fetchall()
        conn.close()
        items: List[Dict[str, Any]] = []
        for row in rows:
            name, size, ext, oss_key, digest, uploaded_by, vector_refs, created_at, updated_at = row
            refs = self._deserialize_vector_refs(vector_refs)
            items.append(
                {
                    "name": name,
                    "size": int(size or 0),
                    "ext": (ext or "").lower(),
                    "oss_key": oss_key,
                    "hash": digest,
                    "uploaded_by": uploaded_by,
                    "vector_refs": refs,
                    "vector_count": len(refs),
                    "created_at": float(created_at or 0),
                    "updated_at": float(updated_at or 0),
                }
            )
        return items

    def stream_documents(self) -> Iterable[Dict[str, Any]]:
        docs = self.list_documents()
        for meta in docs:
            try:
                content = self.read_bytes(meta["name"])
            except FileNotFoundError:
                logger.warning("无法读取对象，跳过：%s", meta["name"])
                continue
            payload = dict(meta)
            payload["content"] = content
            yield payload

    def get_document(self, name: str, include_content: bool = False) -> Dict[str, Any]:
        safe_name = self._sanitize(name)
        if self._backend == "memory":
            meta = self._memory_meta.get(safe_name)
            if not meta:
                raise FileNotFoundError(f"Document {safe_name} not found")
            return dict(meta)

        meta = self._ensure_metadata(safe_name)
        if include_content:
            meta["content"] = self._fetch_object(meta["oss_key"])
        return meta

    def read_bytes(self, name: str) -> bytes:
        meta = self.get_document(name, include_content=False)
        if self._backend == "memory":
            data = meta.get("content")
            return bytes(data or b"")
        key = meta.get("oss_key")
        if not key:
            raise FileNotFoundError(f"Document {name} missing oss_key")
        return self._fetch_object(key)

    def exists(self, name: str) -> bool:
        safe_name = self._sanitize(name)
        if self._backend == "memory":
            return safe_name in self._memory_meta
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM {self.table} WHERE name=%s", (safe_name,))
        row = cur.fetchone()
        conn.close()
        return bool(row)

    def delete(self, name: str) -> None:
        safe_name = self._sanitize(name)
        if self._backend == "memory":
            meta = self._memory_meta.pop(safe_name, None)
            if not meta:
                raise FileNotFoundError(f"Document {safe_name} not found")
            key = meta.get("oss_key")
            if key:
                self._delete_object(key)
            return

        meta = self._ensure_metadata(safe_name)
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {self.table} WHERE name=%s", (safe_name,))
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            raise FileNotFoundError(f"Document {safe_name} not found")
        key = meta.get("oss_key")
        if key:
            self._delete_object(key)

    def update_vector_refs(self, name: str, chunk_ids: List[str]) -> None:
        safe_name = self._sanitize(name)
        payload = json.dumps(chunk_ids, ensure_ascii=False)
        now = time.time()
        if self._backend == "memory":
            meta = self._memory_meta.get(safe_name)
            if meta:
                meta["vector_refs"] = chunk_ids
                meta["updated_at"] = now
            return
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {self.table} SET vector_refs=%s, updated_at=%s WHERE name=%s",
            (payload, now, safe_name),
        )
        conn.commit()
        conn.close()

    def _sanitize(self, name: str) -> str:
        if "/" in name or ".." in name:
            raise ValueError("Illegal document name")
        base = name.strip()
        if not base:
            raise ValueError("Empty document name")
        return base

    @staticmethod
    def compute_hash_from_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _deserialize_vector_refs(raw: Optional[str]) -> List[str]:
        if not raw:
            return []
        try:
            values = json.loads(raw)
            if isinstance(values, list):
                return [str(v) for v in values]
        except Exception:
            return []
        return []