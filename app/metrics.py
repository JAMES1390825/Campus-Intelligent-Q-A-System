from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any, Deque, Dict, List, Optional


class Metrics:
    """In-memory metrics aggregator with a sliding latency window."""

    def __init__(self, latency_window: int = 200):
        self.latencies_ms: Deque[float] = deque(maxlen=latency_window)
        self.total_queries = 0
        self.total_stream_queries = 0
        self.total_doc_uploads = 0
        self.cache_hits = 0
        self.total_errors = 0
        self.last_reset_ts = time.time()
        self._lock = Lock()

    def record_query(self, latency_ms: float, cached: bool):
        with self._lock:
            self.total_queries += 1
            self.latencies_ms.append(latency_ms)
            if cached:
                self.cache_hits += 1

    def record_stream(self, latency_ms: Optional[float] = None, count: bool = True):
        with self._lock:
            if count:
                self.total_stream_queries += 1
            if latency_ms is not None:
                self.latencies_ms.append(latency_ms)

    def record_doc_upload(self):
        with self._lock:
            self.total_doc_uploads += 1

    def record_error(self):
        with self._lock:
            self.total_errors += 1

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        values = sorted(values)
        k = (len(values) - 1) * (percentile / 100)
        f = int(k)
        c = min(f + 1, len(values) - 1)
        if f == c:
            return values[int(k)]
        return values[f] + (values[c] - values[f]) * (k - f)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            latencies = list(self.latencies_ms)
            avg_latency = sum(latencies) / len(latencies) if latencies else None
            p95_latency = self._percentile(latencies, 95) if latencies else None
            uptime_seconds = time.time() - self.last_reset_ts
            return {
                "uptime_seconds": uptime_seconds,
                "total_queries": self.total_queries,
                "total_stream_queries": self.total_stream_queries,
                "total_doc_uploads": self.total_doc_uploads,
                "cache_hits": self.cache_hits,
                "total_errors": self.total_errors,
                "avg_latency_ms": avg_latency,
                "p95_latency_ms": p95_latency,
                "latency_samples": len(latencies),
            }
