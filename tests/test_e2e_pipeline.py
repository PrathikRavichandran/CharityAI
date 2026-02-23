"""
End-to-End integration test for CharityAI.

This test simulates an email arriving and progressing through the entire pipeline.
Since it requires all services (Postgres, Redis, Ollama, Orchestrator) to be running,
it is marked with @pytest.mark.e2e and runs separately from unit tests.

Run with: pytest tests/test_e2e_pipeline.py -v -m e2e
"""

import pytest
import asyncio
import httpx
from datetime import datetime, timezone
from shared.db import fetch_one, execute

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_pipeline_happy_path():
    """
    Simulates a full run: Email Watcher detects charity email -> 
    Orchestrator tracks state -> Dedup Guard passes -> Verification passes ...
    """
    # 0. Insert the organization first so the pipeline row doesn't hit an FK violation
    await execute(
        "INSERT INTO organizations (ein, name, irs_verified, candid_impact_summary) VALUES ($1, $2, $3, $4) ON CONFLICT (ein) DO NOTHING",
        "99-9999999", "E2E Test Foundation", False, "{}"
    )

    import uuid
    run_id = str(uuid.uuid4())[:8]
    
    # 1. Simulate Email Watcher dispatching a classified email
    payload = {
        "email_id": f"test_e2e_{run_id}",
        "email_thread_id": f"thread_e2e_{run_id}",
        "org_name": "E2E Test Foundation",
        "ein": "99-9999999",
        "reason": "Need food distribution help",
        "urgency_signals": ["URGENT: running out of supplies"],
        "contact_email": "test@e2efoundation.org",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "classifier_confidence": 0.95,
        "raw_subject": "Urgent Request for Food"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post("http://localhost:8000/tasks", json={
                "task_type": "email.classified",
                "payload": payload
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "accepted"
        except httpx.ConnectError:
            pytest.skip("Orchestrator not running. Start services with `make dev` first.")

    # 2. Poll DB to see state changes
    # This requires reaching out to the DB directly.
    # In a real E2E test, we would poll until state reaches PA_PENDING, 
    # invoke the Slack webhook manually, then poll until RSVP_PENDING, etc.
    
    # Note: Full E2E requires all 9 agents running and mocked external APIs 
    # (or a dedicated staging environment).
    
    # For now, we just assert the pipeline was created.
    await asyncio.sleep(2)
    record = await fetch_one("SELECT * FROM pipeline WHERE email_id = $1", "test_e2e_001")
    assert record is not None
    assert record["current_state"] in [
        "CLASSIFYING", "DEDUP_CHECK", "VERIFYING", "ELIGIBILITY_CHECK", 
        "SCORING", "IN_PRIORITY_QUEUE", "FINDING_SLOT", "PA_PENDING",
        "DROPPED_NOT_VERIFIED"
    ]
