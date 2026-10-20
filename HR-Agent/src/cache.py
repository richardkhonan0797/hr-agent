from __future__ import annotations

from typing import Any
import json
import logging

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from .execution import current_budget


logger = logging.getLogger(__name__)


class ResilientCache:
    def __init__(self, redis_url: str) -> None:
        # TODO(M2): Initialize Redis with a short socket timeout.
        
        # Cache access should not hold up retrieval when Redis is unavailable.
        # Bound both connection and read time, and disable automatic retries.
        self.client = Redis.from_url(
            redis_url,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
            decode_responses=True,
            retry=Retry(NoBackoff(), 0),
            retry_on_error=[],
        )

    def health(self) -> bool:
        try:
            if self.client is None:
                return False
            response = self.client.ping()
            return bool(response)
        except (RedisError, OSError):
            logger.warning("Retrieval cache health check failed")
            return False

    def get(self, key: str) -> Any | None:
        # TODO(M2): Any Redis failure must behave like a cache miss.
        
        if self.client is None:
            return None
        if not self._has_time():
            return None
        # Missing keys and unreadable entries both behave as cache misses.
        # Keep decoding inside the try block so malformed JSON also degrades safely.
        try:
            payload = self.client.get(key)
            if payload is None:
                return None
            return json.loads(payload)
        except (RedisError, OSError, ValueError, TypeError):
            logger.warning("Retrieval cache read failed")
            return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        # TODO(M2): Cache JSON only and swallow infrastructure failure after logging safely.
        
        if self.client is None or ttl_seconds <= 0:
            return
        if not self._has_time():
            return
        # Serialize inside the try block so unsupported values cannot fail retrieval.
        # Reject non-finite numbers and log only a fixed message, never cache contents.
        try:
            serialized_value = json.dumps(value, allow_nan=False)
            self.client.set(key, serialized_value, ex=ttl_seconds)
        except (RedisError, OSError, ValueError, TypeError):
            logger.warning("Retrieval cache write failed")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def _has_time(self) -> bool:
        budget = current_budget.get()
        # Cache is optional. Skip it when its connect + read timeouts would
        # consume the remaining run budget; keep normal cache semantics otherwise.
        return budget is None or budget.remaining() > 0.5


class NullCache(ResilientCache):
    """Disable Redis access while retaining the same cache interface."""

    def __init__(self) -> None:
        self.client = None

