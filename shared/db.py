"""
Shared PostgreSQL async connection pool for all CharityAI agents.

Usage:
    from shared.db import get_db, execute, fetch_one, fetch_all

    async with get_db() as conn:
        row = await conn.fetchrow("SELECT * FROM pipeline WHERE id = $1", pid)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import asyncpg
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class DBSettings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:pg-charityai@localhost:5432/charityai"

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = DBSettings()
_pool: Optional[asyncpg.Pool] = None
_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    """Return or initialize the shared connection pool."""
    global _pool
    if _pool is None:
        async with _lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    dsn=_settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"),
                    min_size=2,
                    max_size=10,
                    command_timeout=30,
                    statement_cache_size=100,
                )
                logger.info("PostgreSQL pool initialized")
    return _pool


async def close_pool() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL pool closed")


@asynccontextmanager
async def get_db() -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a connection from the pool as a context manager."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def execute(query: str, *args: Any) -> str:
    """Execute a write query. Returns the command tag (e.g. 'INSERT 1')."""
    async with get_db() as conn:
        return await conn.execute(query, *args)


async def fetch_one(query: str, *args: Any) -> Optional[asyncpg.Record]:
    """Fetch a single row or None."""
    async with get_db() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_all(query: str, *args: Any) -> list[asyncpg.Record]:
    """Fetch all matching rows."""
    async with get_db() as conn:
        return await conn.fetch(query, *args)


async def fetch_val(query: str, *args: Any) -> Any:
    """Fetch a single scalar value."""
    async with get_db() as conn:
        return await conn.fetchval(query, *args)


async def execute_in_transaction(queries: list[tuple[str, list]]) -> None:
    """Execute multiple queries atomically within a single transaction."""
    async with get_db() as conn:
        async with conn.transaction():
            for query, args in queries:
                await conn.execute(query, *args)


# ── Audit Logging Helper ───────────────────────────────────────────────────────

async def write_audit_log(
    pipeline_id: str,
    event_type: str,
    actor: str,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Append a row to the immutable audit_log table."""
    import json
    await execute(
        """
        INSERT INTO audit_log
            (pipeline_id, event_type, actor, from_state, to_state, details)
        VALUES ($1, $2, $3, $4::pipeline_state, $5::pipeline_state, $6::jsonb)
        """,
        pipeline_id,
        event_type,
        actor,
        from_state,
        to_state,
        json.dumps(details or {}),
    )


# ── Pipeline State Transition ──────────────────────────────────────────────────

async def transition_state(
    pipeline_id: str,
    new_state: str,
    actor: str,
    extra_updates: Optional[dict] = None,
    details: Optional[dict] = None,
) -> Optional[asyncpg.Record]:
    """
    Atomically transition a pipeline row to a new state and write audit log.

    Args:
        pipeline_id: UUID of the pipeline row.
        new_state: Target pipeline_state ENUM value.
        actor: Agent name or 'PA' or 'SYSTEM_TIMEOUT'.
        extra_updates: Additional column values to SET on the pipeline row.
        details: JSONB payload for the audit log.

    Returns:
        The updated pipeline row.
    """
    set_clauses = ["current_state = $2::pipeline_state", "updated_at = NOW()"]
    params: list[Any] = [pipeline_id, new_state]

    if extra_updates:
        for col, val in extra_updates.items():
            params.append(val)
            set_clauses.append(f"{col} = ${len(params)}")

    query = f"""
        UPDATE pipeline
        SET {', '.join(set_clauses)}
        WHERE id = $1
        RETURNING *
    """

    async with get_db() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(query, *params)
            if row:
                old_state = dict(row).get("current_state")
                await conn.execute(
                    """
                    INSERT INTO audit_log
                        (pipeline_id, event_type, actor, from_state, to_state, details)
                    VALUES ($1, $2, $3, $4::pipeline_state, $5::pipeline_state, $6::jsonb)
                    """,
                    pipeline_id,
                    f"state.transition.{new_state.lower()}",
                    actor,
                    str(old_state) if old_state else None,
                    new_state,
                    __import__("json").dumps(details or {}),
                )
            return row
