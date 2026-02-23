FROM python:3.11-slim
WORKDIR /app
RUN addgroup --system charity && adduser --system --group charity
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY agents/prioritizer/ ./agents/prioritizer/
COPY agents/__init__.py  ./agents/__init__.py
USER charity
EXPOSE 8005
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8005/health || exit 1
CMD ["uvicorn", "agents.prioritizer.main:app", "--host", "0.0.0.0", "--port", "8005"]
