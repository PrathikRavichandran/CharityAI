# ── Base image (shared by all agents) ───────────────────────────────────────
FROM python:3.11-slim AS base

WORKDIR /app

# Security: non-root user
RUN addgroup --system charity && adduser --system --group charity

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps (layer-cached until requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy shared libraries ────────────────────────────────────────────────────
COPY shared/ ./shared/
COPY infra/   ./infra/

# ── Orchestrator source ──────────────────────────────────────────────────────
COPY agents/orchestrator/ ./agents/orchestrator/
COPY agents/__init__.py   ./agents/__init__.py

USER charity

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "agents.orchestrator.main:app", \
    "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
