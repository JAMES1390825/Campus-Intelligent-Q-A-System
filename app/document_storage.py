from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple

from .config import Settings

try:
    import oss2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    oss2 = None  # type: ignore


logger = logging.getLogger("campusqa.docstore")


class DocumentStorage:
    """Unified abstraction for storing and retrieving raw documents.

    - When OSS is enabled, files are saved to bucket + mirrored locally for preview/vectorization.
    - When disabled, falls back to local filesystem only.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.local_dir: Path = settings.docs_path
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._hash_cache: Dict[str, Tuple[str, float, int]] = {}
        self.use_oss = bool(
            settings.use_oss_storage
            and settings.oss_bucket
            and settings.oss_endpoint
            and settings.oss_access_key_id
            and settings.oss_access_key_secret
        )
        self.prefix = (settings.oss_prefix or "").strip("/")
        self._bucket: Optional[Any] = None
        self._oss: Optional[Any] = oss2
        if self.use_oss:
            if self._oss is None:
                raise RuntimeError("oss2 dependency missing, please `pip install oss2`")
            endpoint = settings.oss_internal_endpoint or settings.oss_endpoint
            auth = self._oss.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)  # type: ignore[attr-defined]
            self._bucket = self._oss.Bucket(auth, endpoint, settings.oss_bucket)  # type: ignore[attr-defined]
            logger.info("OSS storage enabled: bucket=%s prefix=%s", settings.oss_bucket, self.prefix or "/")
        else:
            logger.info("OSS storage disabled, using local docs directory")

    # ------------------------------------------------------------------
    # Helpers
    def _remote_key(self, name: str) -> str:
        if not self.prefix:
            return name
        return f"{self.prefix}/{name}"

    def remote_key(self, name: str) -> str:
        """Public helper used by other components to derive OSS object keys."""

        return self._remote_key(name)

    def _ensure_safe_name(self, name: str) -> str:
        if "/" in name or ".." in name:
            raise ValueError("Illegal document name")
        return name

    # ------------------------------------------------------------------
    # CRUD operations
    def _write_local(self, safe_name: str, data: bytes) -> Path:
        local_path = self.local_dir / safe_name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return local_path

    def save(self, name: str, data: bytes) -> Path:
        safe_name = self._ensure_safe_name(name)
        local_path = self.local_dir / safe_name

        if self.use_oss and self._bucket:
            # 上传顺序改为先写入 OSS，再同步到本地，防止 OSS 状态不同步
            self._bucket.put_object(self._remote_key(safe_name), data)
            local_path = self._write_local(safe_name, data)
        else:
            local_path = self._write_local(safe_name, data)
        return local_path

    def ensure_local(self, name: str) -> Path:
        safe_name = self._ensure_safe_name(name)
        local_path = self.local_dir / safe_name
        if local_path.exists():
            return local_path
        if not self.use_oss or not self._bucket:
            raise FileNotFoundError(f"Document {safe_name} not found")
        self.download(name)
        return local_path

    def download(self, name: str, target_path: Optional[Path] = None) -> Path:
        safe_name = self._ensure_safe_name(name)
        if not self.use_oss or not self._bucket:
            raise FileNotFoundError(f"Remote object {safe_name} not available")
        target_path = target_path or (self.local_dir / safe_name)
        result = self._bucket.get_object(self._remote_key(safe_name))
        data = result.read()  # type: ignore[attr-defined]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(data)
        return target_path

    def read_bytes(self, name: str) -> bytes:
        path = self.ensure_local(name)
        return path.read_bytes()

    def list_documents(self) -> List[Dict[str, Any]]:
        """Return metadata for documents (name/size/mtime).

        Always falls back to local directory contents so uploads remain visible
        even if OSS listing fails or is temporarily empty.
        """

        items: Dict[str, Dict[str, Any]] = {}
        iterator_prefix = f"{self.prefix}/" if self.prefix else ""

        if self.use_oss and self._bucket and self._oss:
            try:
                for obj in self._oss.ObjectIterator(self._bucket, prefix=iterator_prefix):  # type: ignore[attr-defined]
                    if getattr(obj, "is_prefix", False):
                        continue
                    key = obj.key
                    if self.prefix:
                        if not key.startswith(f"{self.prefix}/"):
                            continue
                        name = key[len(self.prefix) + 1 :]
                    else:
                        name = key
                    if not name:
                        continue
                    items[name] = {
                        "name": name,
                        "size": getattr(obj, "size", 0),
                        "mtime": getattr(obj, "last_modified", 0),
                        "ext": Path(name).suffix.lower(),
                    }
            except Exception as exc:  # pragma: no cover - network/OSS issues
                logger.warning("Failed to list OSS documents, falling back to local only: %s", exc)

        for path in sorted(self.local_dir.glob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            entry = items.get(path.name)
            if entry:
                # Ensure local metadata fills any missing info from OSS response
                entry.setdefault("size", stat.st_size)
                entry.setdefault("mtime", stat.st_mtime)
                entry.setdefault("ext", path.suffix.lower())
                if "hash" not in entry:
                    entry["hash"] = self._get_or_compute_hash(path.name, path, stat.st_size, stat.st_mtime)
            else:
                items[path.name] = {
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "ext": path.suffix.lower(),
                    "hash": self._get_or_compute_hash(path.name, path, stat.st_size, stat.st_mtime),
                }

        # Return sorted list for deterministic ordering
        return [items[name] for name in sorted(items.keys())]

    def _get_or_compute_hash(self, name: str, path: Path, size: int, mtime: float) -> Optional[str]:
        try:
            cached = self._hash_cache.get(name)
            if cached and cached[1] == float(mtime) and cached[2] == size:
                return cached[0]
            digest = self._compute_file_hash(path)
            self._hash_cache[name] = (digest, float(mtime), size)
            return digest
        except FileNotFoundError:
            return None

    def _compute_file_hash(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    def compute_hash_from_bytes(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def sync_from_remote(self) -> None:
        if not self.use_oss or not self._bucket or not self._oss:
            return
        iterator_prefix = f"{self.prefix}/" if self.prefix else ""
        for obj in self._oss.ObjectIterator(self._bucket, prefix=iterator_prefix):  # type: ignore[attr-defined]
            if getattr(obj, "is_prefix", False):
                continue
            key = obj.key
            if self.prefix:
                if not key.startswith(f"{self.prefix}/"):
                    continue
                name = key[len(self.prefix) + 1 :]
            else:
                name = key
            if not name:
                continue
            local_path = self.local_dir / name
            remote_mtime = getattr(obj, "last_modified", None)
            needs_download = not local_path.exists()
            if not needs_download and remote_mtime:
                local_mtime = int(local_path.stat().st_mtime)
                if remote_mtime > local_mtime + 1:
                    needs_download = True
            if needs_download:
                logger.info("Syncing %s from OSS", name)
                self.download(name, local_path)

    def exists(self, name: str) -> bool:
        safe_name = self._ensure_safe_name(name)
        local_path = self.local_dir / safe_name
        if local_path.exists():
            return True
        if not self.use_oss or not self._bucket:
            return False
        try:
            self._bucket.get_object_meta(self._remote_key(safe_name))
            return True
        except Exception:  # pragma: no cover
            return False

    def delete(self, name: str) -> None:
        safe_name = self._ensure_safe_name(name)
        if not self.exists(name):
            raise FileNotFoundError(f"Document {safe_name} not found")

        local_path = self.local_dir / safe_name
        if local_path.exists():
            local_path.unlink()

        if self.use_oss and self._bucket:
            try:
                self._bucket.delete_object(self._remote_key(safe_name))
            except Exception as exc:  # pragma: no cover - remote delete failure should be visible to caller
                logger.error("Failed to delete %s from OSS: %s", safe_name, exc)
                raise