import httpx
import json

payload = {
  "task_id": "test-123",
  "task_type": "email.classified",
  "payload": {
    "email_id": "msg123",
    "email_thread_id": "thread123",
    "org_name": "Test Charity",
    "ein": "12-3456789",
    "reason": "Need help",
    "urgency_signals": ["urgent"],
    "contact_email": "test@example.com",
    "received_at": "2023-10-10T10:00:00Z",
    "classifier_confidence": 0.95
  }
}

try:
    response = httpx.post("http://localhost:8000/tasks", json=payload, timeout=5.0)
    print(response.status_code)
    print(response.text)
except Exception as e:
    print(e)
