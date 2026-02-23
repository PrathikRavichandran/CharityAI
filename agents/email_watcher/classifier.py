"""
Email Charity Classifier — Gate 1, Step 1
Model: Phi3 (primary) → Mistral (fallback)

Determines whether an email is:
  1. Charity-related (passes to extraction)
  2. Not charity-related (silent drop)

Returns a dict with: {"is_charity": bool, "confidence": float, "reasoning": str}
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from shared.ollama_client import OllamaRouter, ModelTask, ModelFailureError

log = logging.getLogger(__name__)

_router = OllamaRouter()

SYSTEM_PROMPT = """You are an email classifier for a charity appointment scheduling system.
Your ONLY job is to determine if an incoming email is from a charitable organization
requesting a meeting or appointment with an executive.

Charity-related emails typically:
- Come from nonprofits, foundations, NGOs, or charitable organizations
- Request a meeting, appointment, or call with an executive
- Mention funding, grants, donations, partnerships, or community programs
- Reference an EIN (Employer Identification Number) or 501(c)(3) status
- Discuss social causes: food security, housing, healthcare, education, etc.

NOT charity-related:
- Sales pitches for commercial products or SaaS tools
- Spam, newsletters, or marketing emails  
- Internal company emails
- Invoices, receipts, notifications
- Personal emails with no organizational purpose

Respond ONLY with valid JSON. No other text."""

USER_TEMPLATE = """Classify this email:

Subject: {subject}
From: {from_addr}
Body (first 1000 chars):
{body_snippet}

Respond with JSON only:
{{
  "is_charity": true or false,
  "confidence": 0.0 to 1.0,
  "reasoning": "one sentence explanation"
}}"""


async def classify_email(subject: str, body: str, from_addr: str) -> dict[str, Any]:
    """
    Classify an email as charity-related or not.

    Returns:
        {
            "is_charity": bool,
            "confidence": float,
            "reasoning": str
        }
    """
    prompt = USER_TEMPLATE.format(
        subject=subject[:200],
        from_addr=from_addr[:100],
        body_snippet=body[:1000],
    )

    try:
        raw = await _router.complete(
            task=ModelTask.EMAIL_CLASSIFY,
            prompt=prompt,
            system=SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=256,
        )
        result = _parse_json(raw)
        log.info(
            "Classification: is_charity=%s confidence=%.2f",
            result.get("is_charity"), result.get("confidence", 0),
        )
        return result

    except ModelFailureError as e:
        log.error("Model failure during classification: %s", e)
        # Drop email on model failure to prevent infinite loops through extractor
        return {
            "is_charity": False,
            "confidence": 0.0,
            "reasoning": "Model failure — dropped",
        }
    except Exception as e:
        log.error("Classification error: %s", e)
        return {"is_charity": False, "confidence": 0.0, "reasoning": f"Error: {e}"}


def _parse_json(raw: str) -> dict:
    """Extract JSON from model response, handling markdown fences."""
    # Strip markdown code fences
    clean = re.sub(r"```(?:json)?", "", raw).strip().strip("`")

    # Find first {...} block
    match = re.search(r"\{.*\}", clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Try whole string as JSON
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.warning("Could not parse classifier JSON: %s", raw[:200])
        return {"is_charity": False, "confidence": 0.0, "reasoning": "Parse error"}
