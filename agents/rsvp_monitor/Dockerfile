FROM python:3.11-slim
WORKDIR /app
RUN addgroup --system charity && adduser --system --group charity
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY agents/rsvp_monitor/ ./agents/rsvp_monitor/
COPY agents/__init__.py   ./agents/__init__.py
RUN mkdir -p auth
USER charity
EXPOSE 8009
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8009/health || exit 1
CMD ["uvicorn", "agents.rsvp_monitor.main:app", "--host", "0.0.0.0", "--port", "8009"]
