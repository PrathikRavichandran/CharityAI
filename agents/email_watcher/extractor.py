"""
Structured Field Extractor — Gate 1, Step 2
Model: Mistral (primary) → Llama3 (fallback)

For emails that pass charity classification, extracts:
  - org_name:       Official organization name
  - ein:            EIN in XX-XXXXXXX format
  - reason:         Why they want a meeting / funding reason
  - urgency_signals: List of urgency keywords found
  - contact_email:  Sender's reply-to email

Returns a dict with all extracted fields (None if not found).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from shared.ollama_client import OllamaRouter, ModelTask, ModelFailureError

log = logging.getLogger(__name__)

_router = OllamaRouter()

# Keywords that signal urgency (used in eligibility escalation)
URGENCY_KEYWORDS = [
    "urgent", "emergency", "crisis", "critical", "immediate",
    "disaster", "flood", "hurricane", "fire", "life-threatening",
    "deadline", "expiring", "at risk", "facing closure", "running out",
]

SYSTEM_PROMPT = """You are a data extraction assistant for a charity scheduling system.
Extract specific structured information from charity emails.
Be precise. If a field is not present in the email, return null.
For EIN, look for patterns like XX-XXXXXXX or XXXXXXXXX.
Respond ONLY with valid JSON. No other text."""

USER_TEMPLATE = """Extract structured data from this charity email:

Subject: {subject}
From: {from_addr}
Body:
{body}

Respond with JSON only — extract exactly these fields:
{{
  "org_name": "Official organization name or null",
  "ein": "EIN in format XX-XXXXXXX or null if not found",
  "reason": "Why they want a meeting / what they need (1-2 sentences) or null",
  "contact_email": "Reply-to or sender email address",
  "estimated_people_impacted": "Number of people impacted as integer or null"
}}"""


async def extract_fields(subject: str, body: str, from_addr: str) -> dict[str, Any]:
    """
    Extract structured fields from a charity-classified email.

    Returns:
        {
            "org_name":                str | None,
            "ein":                     str | None,
            "reason":                  str | None,
            "contact_email":           str | None,
            "urgency_signals":         list[str],
            "estimated_people_impacted": int | None,
        }
    """
    prompt = USER_TEMPLATE.format(
        subject=subject[:200],
        from_addr=from_addr[:100],
        body=body[:3000],
    )

    extracted: dict[str, Any] = {
        "org_name": None,
        "ein": None,
        "reason": None,
        "contact_email": None,
        "urgency_signals": [],
        "estimated_people_impacted": None,
    }

    try:
        raw = await _router.complete(
            task=ModelTask.STRUCTURED_EXTRACT,
            prompt=prompt,
            system=SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=512,
        )
        parsed = _parse_json(raw)
        extracted.update({k: v for k, v in parsed.items() if v is not None})

    except ModelFailureError as e:
        log.error("Model failure during extraction: %s", e)
        extracted["model_failure"] = True

    except Exception as e:
        log.error("Extraction error: %s", e)

    # Normalize EIN format
    if extracted.get("ein"):
        extracted["ein"] = _normalize_ein(extracted["ein"])

    # Extract urgency signals from body (deterministic — no LLM needed)
    body_lower = (subject + " " + body).lower()
    extracted["urgency_signals"] = [
        kw for kw in URGENCY_KEYWORDS if kw in body_lower
    ]

    # Fallback contact_email: parse from From header
    if not extracted.get("contact_email") and from_addr:
        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_addr)
        if match:
            extracted["contact_email"] = match.group()

    log.info(
        "Extracted: org='%s' ein=%s urgency=%s",
        extracted.get("org_name"),
        extracted.get("ein"),
        extracted.get("urgency_signals"),
    )
    return extracted


def _normalize_ein(ein_raw: str) -> str:
    """
    Normalize EIN to XX-XXXXXXX format.
    Accepts: 123456789, 12-3456789, 12 3456789, etc.
    """
    digits = re.sub(r"\D", "", str(ein_raw))
    if len(digits) == 9:
        return f"{digits[:2]}-{digits[2:]}"
    return ein_raw.strip()


def _parse_json(raw: str) -> dict:
    """Extract JSON dict from model response, handling markdown fences."""
    clean = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.warning("Could not parse extraction JSON: %s", raw[:200])
        return {}
