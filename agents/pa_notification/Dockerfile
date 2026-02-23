FROM python:3.11-slim
WORKDIR /app
RUN addgroup --system charity && adduser --system --group charity
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY agents/pa_notification/ ./agents/pa_notification/
COPY agents/__init__.py      ./agents/__init__.py
USER charity
EXPOSE 8007
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8007/health || exit 1
# Expose /slack/actions over ngrok or public URL (configure SLACK_ACTIONS_URL in .env)
CMD ["uvicorn", "agents.pa_notification.main:app", "--host", "0.0.0.0", "--port", "8007"]
