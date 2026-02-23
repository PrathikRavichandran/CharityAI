"""
Redis client helpers for CharityAI:
- Pub/Sub publish and subscribe
- TTL timer key creation and expiry detection
- Cache get/set helpers
- Dead letter queue

Usage:
    from shared.redis_client import get_redis, publish, set_timer

    r = await get_redis()
    await publish(PubSubChannels.EMAIL_CLASSIFIED, payload)
    await set_timer(TimerKeys.pa_timeout(pipeline_id), TimerKeys.PA_TIMEOUT_TTL)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis
from pydantic_settings import BaseSettings

from infra.redis_channels import CacheKeys, DeadLetterQueue, TimerKeys

logger = logging.getLogger(__name__)


class RedisSettings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379/0"

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = RedisSettings()
_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Return or initialize the shared Redis client."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            _settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
        logger.info("Redis client initialized: %s", _settings.REDIS_URL)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None


# ── Pub/Sub ────────────────────────────────────────────────────────────────────

async def publish(channel: str, payload: dict | str) -> None:
    """Publish a JSON payload to a Redis channel."""
    r = await get_redis()
    message = json.dumps(payload) if isinstance(payload, dict) else payload
    await r.publish(channel, message)
    logger.debug("Published to %s: %s", channel, message[:120])


async def subscribe(channel: str):
    """
    Return a pubsub object subscribed to channel.
    Caller is responsible for reading messages.

    Example:
        pubsub = await subscribe("pipeline.email_classified")
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                data = json.loads(msg["data"])
    """
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)
    return pubsub


async def psubscribe(pattern: str):
    """Return a pubsub subscribed to a glob pattern (e.g. 'pipeline.*')."""
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.psubscribe(pattern)
    return pubsub


# ── TTL Timers ─────────────────────────────────────────────────────────────────

async def set_timer(key: str, ttl_seconds: int, value: str = "pending") -> None:
    """Create a Redis key that auto-expires after ttl_seconds."""
    r = await get_redis()
    await r.setex(key, ttl_seconds, value)
    logger.info("Timer set: %s (TTL=%ds)", key, ttl_seconds)


async def cancel_timer(key: str) -> None:
    """Delete a timer key before it expires."""
    r = await get_redis()
    await r.delete(key)
    logger.info("Timer cancelled: %s", key)


async def timer_exists(key: str) -> bool:
    """Check if a timer key is still active."""
    r = await get_redis()
    return await r.exists(key) == 1


async def timer_ttl(key: str) -> int:
    """Return remaining TTL in seconds, or -2 if key doesn't exist."""
    r = await get_redis()
    return await r.ttl(key)


# ── Cache ──────────────────────────────────────────────────────────────────────

async def cache_set(key: str, data: dict, ttl_seconds: int) -> None:
    """Store a JSON-serializable dict in Redis cache with TTL."""
    r = await get_redis()
    await r.setex(key, ttl_seconds, json.dumps(data))


async def cache_get(key: str) -> Optional[dict]:
    """Retrieve and deserialize a cached dict. Returns None on miss."""
    r = await get_redis()
    raw = await r.get(key)
    if raw is None:
        return None
    return json.loads(raw)


async def org_cache_get(ein: str) -> Optional[dict]:
    return await cache_get(CacheKeys.org_cache(ein))


async def org_cache_set(ein: str, data: dict) -> None:
    await cache_set(CacheKeys.org_cache(ein), data, CacheKeys.ORG_CACHE_TTL)


# ── Dead Letter Queue ──────────────────────────────────────────────────────────

async def dead_letter_push(pipeline_id: str, payload: dict) -> None:
    """Push a failed A2A dispatch to the dead letter queue."""
    r = await get_redis()
    entry = json.dumps({"pipeline_id": pipeline_id, "payload": payload})
    await r.rpush(DeadLetterQueue.KEY, entry)
    logger.error("Dead letter queued for pipeline %s", pipeline_id)


async def dead_letter_pop() -> Optional[dict]:
    """Pop from dead letter queue (FIFO). Returns None if empty."""
    r = await get_redis()
    raw = await r.lpop(DeadLetterQueue.KEY)
    return json.loads(raw) if raw else None


async def dead_letter_length() -> int:
    """Return count of items in the dead letter queue."""
    r = await get_redis()
    return await r.llen(DeadLetterQueue.KEY)
