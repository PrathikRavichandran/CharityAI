"""
Audit Logger Sidecar — CharityAI
Runs alongside the Orchestrator on the same EC2 instance.

Subscribes to all 'pipeline.*' Redis events via pattern sub.
Writes every event to the immutable audit_log table.

Run as a background process:
    python -m agents.orchestrator.audit_sidecar
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys

import structlog
from pydantic_settings import BaseSettings

from shared.db import get_pool, close_pool, execute
from shared.redis_client import get_redis, close_redis, psubscribe
from infra.redis_channels import PubSubChannels

log = structlog.get_logger()


class AuditSettings(BaseSettings):
    LOG_LEVEL: str = "INFO"
    REDIS_URL: str = "redis://localhost:6379/0"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = AuditSettings()
logging.basicConfig(level=settings.LOG_LEVEL)

_running = True


def _shutdown(sig, frame):
    global _running
    log.info("Audit sidecar shutting down")
    _running = False


async def run() -> None:
    """Main audit sidecar loop."""
    await get_pool()
    await get_redis()

    pubsub = await psubscribe(PubSubChannels.AUDIT_PATTERN)
    log.info("Audit sidecar listening on pattern: %s", PubSubChannels.AUDIT_PATTERN)

    try:
        async for message in pubsub.listen():
            if not _running:
                break

            if message.get("type") not in ("pmessage", "message"):
                continue

            channel = message.get("channel", "")
            raw_data = message.get("data", "{}")

            try:
                data = json.loads(raw_data) if isinstance(raw_data, str) else {}
            except json.JSONDecodeError:
                data = {"raw": str(raw_data)}

            pipeline_id = data.pop("pipeline_id", None)
            from_state  = data.pop("from_state", None)
            to_state    = data.pop("to_state", None)
            actor       = data.pop("actor", "SYSTEM")
            event_type  = channel.replace("pipeline.", "pipeline.event.")

            try:
                await execute(
                    """
                    INSERT INTO audit_log
                        (pipeline_id, event_type, actor, from_state, to_state, details)
                    VALUES
                        ($1, $2, $3, $4::pipeline_state, $5::pipeline_state, $6::jsonb)
                    """,
                    pipeline_id,
                    event_type,
                    actor,
                    from_state,
                    to_state,
                    json.dumps(data),
                )
                log.debug(
                    "Audit written",
                    channel=channel,
                    pipeline_id=pipeline_id,
                    to_state=to_state,
                )
            except Exception as db_err:
                log.error("Audit DB write failed", error=str(db_err), channel=channel)

    finally:
        await pubsub.close()
        await close_pool()
        await close_redis()
        log.info("Audit sidecar stopped cleanly")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    asyncio.run(run())
