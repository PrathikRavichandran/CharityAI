FROM python:3.11-slim
WORKDIR /app
RUN addgroup --system charity && adduser --system --group charity
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY agents/email_composer/ ./agents/email_composer/
COPY agents/__init__.py     ./agents/__init__.py
RUN mkdir -p auth
USER charity
EXPOSE 8008
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8008/health || exit 1
CMD ["uvicorn", "agents.email_composer.main:app", "--host", "0.0.0.0", "--port", "8008"]
