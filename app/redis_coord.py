from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Generator, Optional, List

try:  # pragma: no cover - optional dependency
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class RedisLockHandle:
    client: Any
    key: str
    token: str

    def release(self) -> None:
        if not self.client:
            return
        lua = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        try:
            self.client.eval(lua, 1, self.key, self.token)
        except Exception as exc:  # pragma: no cover - release best effort
            logger.warning("Failed to release redis lock %s: %s", self.key, exc)


class RedisCoordinator:
    """Provides distributed locks, ingestion queues, and status inspection APIs."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.enabled = False
        self.client: Optional[Any] = None
        if redis is None:
            logger.warning("redis package not installed; coordination disabled")
            return
        redis_cls: Any = getattr(redis, "Redis", None)
        if redis_cls is None:
            logger.warning("redis package missing Redis class; coordination disabled")
            return
        url = self.settings.redis_url
        try:
            if url:
                self.client = redis_cls.from_url(url)  # type: ignore[attr-defined]
            else:
                self.client = redis_cls(  # type: ignore[call-arg]
                    host=self.settings.redis_host,
                    port=self.settings.redis_port,
                    db=self.settings.redis_db,
                    password=self.settings.redis_password,
                )
            if self.client:
                self.client.ping()
                self.enabled = True
                logger.info("Redis coordination enabled")
        except Exception as exc:
            logger.warning("Redis unavailable, coordination disabled: %s", exc)
            self.client = None
            self.enabled = False

    def _namespaced(self, key: str) -> str:
        prefix = (self.settings.redis_prefix or "campusqa").strip(":")
        return f"{prefix}:{key}" if prefix else key

    def _history_key(self) -> str:
        return self._namespaced("ingest:history")

    @contextmanager
    def lock(self, name: str, ttl: int = 120, wait_timeout: int = 10) -> Generator[Optional[RedisLockHandle], None, None]:
        if not self.enabled or not self.client:
            yield None
            return
        key = self._namespaced(f"lock:{name}")
        token = f"{time.time()}"  # simple token
        deadline = time.time() + max(wait_timeout, 0)
        acquired = False
        while time.time() < deadline:
            try:
                if self.client.set(key, token, nx=True, ex=ttl):
                    acquired = True
                    break
            except Exception as exc:  # pragma: no cover
                logger.warning("Redis lock set failed %s: %s", key, exc)
                break
            time.sleep(0.2)
        handle = RedisLockHandle(self.client, key, token) if acquired else None
        try:
            yield handle
        finally:
            if acquired and handle:
                handle.release()

    def enqueue(self, queue_name: str, payload: Dict[str, Any]) -> None:
        if not self.enabled or not self.client:
            return
        key = self._namespaced(f"queue:{queue_name}")
        try:
            self.client.rpush(key, json.dumps(payload))
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to push job to redis queue %s: %s", key, exc)

    def set_status(self, doc_name: str, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
        if not self.enabled or not self.client:
            return
        key = self._namespaced(f"doc-status:{doc_name}")
        payload: Dict[str, Any] = {"status": status, "ts": time.time()}
        if extra:
            payload.update(extra)
        try:
            self.client.hset(key, mapping=payload)
            self.client.expire(key, 3600)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to set redis status %s: %s", key, exc)

    def _decode_value(self, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            text = value.decode("utf-8", errors="ignore")
            try:
                return json.loads(text)
            except Exception:
                return text
        return value

    def _decode_map(self, mapping: Dict[Any, Any]) -> Dict[str, Any]:
        return {self._decode_value(k): self._decode_value(v) for k, v in mapping.items()}

    def get_status(self, doc_name: str) -> Optional[Dict[str, Any]]:
        if not self.enabled or not self.client:
            return None
        key = self._namespaced(f"doc-status:{doc_name}")
        try:
            raw = self.client.hgetall(key)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to get redis status %s: %s", key, exc)
            return None
        if not raw:
            return None
        payload = self._decode_map(raw)
        payload.setdefault("doc", doc_name)
        return payload

    def list_statuses(self) -> Dict[str, Dict[str, Any]]:
        if not self.enabled or not self.client:
            return {}
        statuses: Dict[str, Dict[str, Any]] = {}
        base = self._namespaced("doc-status:")
        pattern = f"{base}*"
        try:
            for key in self.client.scan_iter(match=pattern):  # type: ignore[attr-defined]
                key_str = key.decode("utf-8", errors="ignore")
                raw = self.client.hgetall(key)
                if not raw:
                    continue
                doc_name = key_str.replace(base, "", 1)
                statuses[doc_name] = self._decode_map(raw)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to list redis statuses: %s", exc)
        return statuses

    def queue_length(self, queue_name: str) -> int:
        if not self.enabled or not self.client:
            return 0
        key = self._namespaced(f"queue:{queue_name}")
        try:
            return int(self.client.llen(key))
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to read redis queue length %s: %s", key, exc)
            return 0

    def peek_queue(self, queue_name: str, count: int = 10) -> List[Dict[str, Any]]:
        if not self.enabled or not self.client:
            return []
        key = self._namespaced(f"queue:{queue_name}")
        try:
            items = self.client.lrange(key, 0, max(count - 1, 0))
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to peek redis queue %s: %s", key, exc)
            return []
        decoded: List[Dict[str, Any]] = []
        for item in items:
            try:
                decoded.append(json.loads(item))
            except Exception:
                decoded.append({"raw": item.decode("utf-8", errors="ignore")})
        return decoded

    def snapshot(self, queue_name: str = "doc_ingest", preview: int = 5) -> Dict[str, Any]:
        if not self.enabled or not self.client:
            return {"enabled": False, "statuses": {}, "queue_length": 0, "queue_preview": []}
        return {
            "enabled": True,
            "statuses": self.list_statuses(),
            "queue_length": self.queue_length(queue_name),
            "queue_preview": self.peek_queue(queue_name, preview),
            "history": self.recent_events(preview * 4),
        }

    def record_event(self, event: Dict[str, Any]) -> None:
        if not self.enabled or not self.client:
            return
        payload = dict(event)
        payload.setdefault("ts", time.time())
        if "doc" not in payload and payload.get("filename"):
            payload["doc"] = payload.get("filename")
        try:
            self.client.lpush(self._history_key(), json.dumps(payload))
            self.client.ltrim(self._history_key(), 0, 200)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to record redis event: %s", exc)

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.enabled or not self.client:
            return []
        key = self._history_key()
        try:
            entries = self.client.lrange(key, 0, max(limit - 1, 0))
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to read redis event history: %s", exc)
            return []
        events: List[Dict[str, Any]] = []
        for entry in entries:
            try:
                events.append(json.loads(entry))
            except Exception:
                continue
        return events
