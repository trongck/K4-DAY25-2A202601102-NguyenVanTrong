from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, object]


class ResponseCache:
    """In-memory semantic cache with TTL, privacy, and false-hit guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []
        self._lock = threading.RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity."""
        value, score, _ = self.get_with_metadata(query)
        return value, score

    def get_with_metadata(self, query: str) -> tuple[str | None, float, dict[str, object]]:
        """Look up a response and return the token/cost metadata stored with it."""
        if _is_uncacheable(query):
            return None, 0.0, {}

        with self._lock:
            current_time = time.time()
            self._entries = [
                e for e in self._entries if current_time - e.created_at <= self.ttl_seconds
            ]

            best_score = 0.0
            best_entry = None

            for entry in self._entries:
                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_score = score
                    best_entry = entry

            if best_entry and best_score >= self.similarity_threshold:
                if _looks_like_false_hit(query, best_entry.key):
                    self.false_hit_log.append(
                        {
                            "query": query,
                            "cached_key": best_entry.key,
                            "reason": "date_or_number_mismatch",
                        }
                    )
                    return None, best_score, {}
                return best_entry.value, best_score, dict(best_entry.metadata)

            return None, best_score, {}

    def set(self, query: str, value: str, metadata: dict[str, object] | None = None) -> None:
        """Store a response in cache."""
        if _is_uncacheable(query):
            return

        with self._lock:
            self._entries.append(
                CacheEntry(key=query, value=value, created_at=time.time(), metadata=metadata or {})
            )

    def entry_count(self) -> int:
        """Return the number of non-expired entries currently held in memory."""
        with self._lock:
            current_time = time.time()
            self._entries = [
                e for e in self._entries if current_time - e.created_at <= self.ttl_seconds
            ]
            return len(self._entries)

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings."""
        if a == b:
            return 1.0

        def get_tokens(s: str) -> list[str]:
            words = s.split()
            tokens = list(words)
            for word in words:
                if len(word) >= 3:
                    for i in range(len(word) - 2):
                        tokens.append(word[i : i + 3])
            return tokens

        tokens_a = get_tokens(a)
        tokens_b = get_tokens(b)

        count_a = Counter(tokens_a)
        count_b = Counter(tokens_b)

        dot_product = sum(count_a[token] * count_b[token] for token in count_a if token in count_b)
        mag_a = math.sqrt(sum(v * v for v in count_a.values()))
        mag_b = math.sqrt(sum(v * v for v in count_b.values()))

        if mag_a == 0 or mag_b == 0:
            return 0.0

        return dot_product / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed semantic cache shared by multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""
        value, score, _ = self.get_with_metadata(query)
        return value, score

    def get_with_metadata(self, query: str) -> tuple[str | None, float, dict[str, object]]:
        """Look up a Redis response together with token/cost metadata."""
        if _is_uncacheable(query):
            return None, 0.0, {}

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        exact_entry = self._redis.hgetall(exact_key)
        if exact_entry.get("response"):
            return exact_entry["response"], 1.0, self._decode_metadata(exact_entry)

        best_score = 0.0
        best_entry: dict[str, str] | None = None

        for key in self._redis.scan_iter(f"{self.prefix}*"):
            entry = self._redis.hgetall(key)
            cached_query = entry.get("query")
            if not cached_query:
                continue

            score = ResponseCache.similarity(query, cached_query)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry and best_entry.get("response") and best_score >= self.similarity_threshold:
            best_cached_query = best_entry.get("query", "")
            if _looks_like_false_hit(query, best_cached_query):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_cached_query,
                        "reason": "date_or_number_mismatch",
                    }
                )
                return None, best_score, {}
            return best_entry["response"], best_score, self._decode_metadata(best_entry)

        return None, best_score, {}

    def set(self, query: str, value: str, metadata: dict[str, object] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query):
            return

        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(
            key,
            mapping={
                "query": query,
                "response": value,
                "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            },
        )
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def entry_count(self) -> int:
        """Return the number of cache entries in this Redis namespace."""
        return sum(1 for _ in self._redis.scan_iter(f"{self.prefix}*"))

    def keys(self) -> list[str]:
        """Return sorted keys in this namespace for inspectable report evidence."""
        return sorted(str(key) for key in self._redis.scan_iter(f"{self.prefix}*"))

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]

    @staticmethod
    def _decode_metadata(entry: dict[str, str]) -> dict[str, object]:
        raw = entry.get("metadata", "")
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
