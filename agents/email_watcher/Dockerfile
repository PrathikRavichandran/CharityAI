FROM python:3.11-slim
WORKDIR /app
RUN addgroup --system charity && adduser --system --group charity
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY infra/   ./infra/
COPY agents/email_watcher/ ./agents/email_watcher/
COPY agents/__init__.py    ./agents/__init__.py
# Gmail OAuth token (mounted at runtime via Docker secret / volume)
RUN mkdir -p auth
USER charity
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1
CMD ["uvicorn", "agents.email_watcher.main:app", "--host", "0.0.0.0", "--port", "8001"]
