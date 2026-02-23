"""
A2A (Agent-to-Agent) HTTP task dispatcher for CharityAI.

All inter-agent communication goes through this module.
Implements retry with exponential backoff, dead letter queuing, and
structured error logging.

Usage:
    from shared.a2a_client import dispatch_task
    from shared.models import A2ATask, TaskType, EmailClassifiedPayload

    response = await dispatch_task(
        target_url="http://localhost:8000/tasks",
        task_type=TaskType.EMAIL_CLASSIFIED,
        payload=my_payload.model_dump(),
        pipeline_id=pipeline_id,
    )
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from shared.models import A2ATask, A2AResponse
from shared.redis_client import dead_letter_push

logger = logging.getLogger(__name__)

# A2A retry configuration (per architecture spec: 3x at 15s intervals)
_MAX_ATTEMPTS = 3
_WAIT_MIN = 15      # seconds
_WAIT_MAX = 15      # fixed interval (not exponential in spec)
_TIMEOUT_SECONDS = 30


class A2ADispatchError(Exception):
    """Raised when all retry attempts to an agent are exhausted."""
    def __init__(self, agent_url: str, pipeline_id: Optional[str] = None):
        self.agent_url = agent_url
        self.pipeline_id = pipeline_id
        super().__init__(f"All A2A retry attempts failed for {agent_url}")


async def dispatch_task(
    target_url: str,
    task_type: str,
    payload: dict[str, Any],
    pipeline_id: Optional[str] = None,
) -> A2AResponse:
    """
    Send an A2A task to a target agent with retry + dead letter fallback.

    Args:
        target_url:   Full URL of the agent's POST /tasks endpoint.
        task_type:    task_type string (from TaskType constants).
        payload:      Dict payload to wrap in the A2ATask envelope.
        pipeline_id:  Pipeline UUID for dead letter logging on failure.

    Returns:
        A2AResponse from the target agent.

    Raises:
        A2ADispatchError: If all retries are exhausted (dead letter also queued).
    """
    task = A2ATask(task_type=task_type, payload=payload)
    task_dict = task.model_dump(mode="json")

    logger.info(
        "A2A dispatch → %s [task_id=%s type=%s]",
        target_url, task.task_id, task_type,
    )

    try:
        response_dict = await _post_with_retry(target_url, task_dict)
        response = A2AResponse(**response_dict)
        logger.info(
            "A2A response ← %s [status=%s]",
            target_url, response.status,
        )
        return response

    except Exception as exc:
        logger.error(
            "A2A all retries exhausted → %s pipeline_id=%s error=%s",
            target_url, pipeline_id, exc,
        )
        # Push to dead letter queue for manual requeue
        if pipeline_id:
            await dead_letter_push(pipeline_id, {"task": task_dict, "target": target_url})
        raise A2ADispatchError(target_url, pipeline_id) from exc


@retry(
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    wait=wait_exponential(min=_WAIT_MIN, max=_WAIT_MAX),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _post_with_retry(url: str, payload: dict) -> dict:
    """Low-level retry-wrapped HTTP POST. Used internally by dispatch_task."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


# ── Health Check ────────────────────────────────────────────────────────────────

async def health_check(agent_url: str) -> bool:
    """
    Ping an agent's /health endpoint.

    Returns:
        True if agent responds with 200, False otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{agent_url}/health")
            return resp.status_code == 200
    except Exception:
        return False


async def check_all_agents(agent_urls: dict[str, str]) -> dict[str, bool]:
    """
    Ping all agents and return a health status dict.

    Args:
        agent_urls: {agent_name: base_url} mapping.

    Returns:
        {agent_name: is_healthy} mapping.
    """
    import asyncio
    results = await asyncio.gather(
        *[health_check(url) for url in agent_urls.values()],
        return_exceptions=True,
    )
    return {
        name: (result is True)
        for name, result in zip(agent_urls.keys(), results)
    }
