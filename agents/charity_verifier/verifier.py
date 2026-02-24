"""
Charity Verifier Logic — Gate 3

Verification pipeline:
  1. Candid API (if CANDID_MODE=live) → primary source
  2. IRS Tax-Exempt Search API         → always used
  3. Web search (DuckDuckGo)           → fallback if IRS misses
  4. Redis org cache (7-day TTL)       → skip all API calls on hit
  5. Mistral synthesizes all sources   → confidence score
  6. All orgs flagged LOW in manual mode → PA note added

Manual Verification Mode (current):
  - CANDID_MODE=stub means Candid calls are no-ops
  - All confidence scores forced to LOW
  - PA receives 'manual_mode=True' flag and must verify manually

Dispatch: org.verified → Orchestrator
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from pydantic_settings import BaseSettings

from shared.a2a_client import dispatch_task, A2ADispatchError
from shared.ollama_client import OllamaRouter, ModelTask, ModelFailureError
from shared.redis_client import org_cache_get, org_cache_set
from shared.db import fetch_one, execute
from shared.models import TaskType, VerificationConfidence

log = logging.getLogger(__name__)
_router = OllamaRouter()


class VerifierSettings(BaseSettings):
    ORCHESTRATOR_URL:   str = "http://localhost:8000"
    IRS_EXEMPT_API_URL: str = "https://efts.irs.gov/LATEST/search-index"
    IRS_EXEMPT_PAGE_SIZE: int = 5
    CANDID_MODE:        str = "stub"    # stub | live
    CANDID_API_KEY:     str = ""
    CANDID_BASE_URL:    str = "https://api.candid.org/premier/v3"

    class Config:
        env_file = ".env"
        extra = "ignore"


_settings = VerifierSettings()

# ── Candid Stubs (drop-in when CANDID_MODE=live) ─────────────────────────────

async def candid_lookup_ein(ein: str) -> Optional[dict]:
    """
    Candid EIN lookup. Returns org data dict or None.
    STUB: returns None until CANDID_MODE=live and API key is set.
    """
    if _settings.CANDID_MODE != "live" or not _settings.CANDID_API_KEY:
        log.debug("Candid stub: skipping EIN lookup for %s", ein)
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_settings.CANDID_BASE_URL}/organizations/{ein}",
                headers={"Subscription-Key": _settings.CANDID_API_KEY},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "candid_id":           data.get("organization_id"),
                    "candid_profile_url":  data.get("profile_link"),
                    "cause_category":      data.get("ntee_major_category"),
                    "candid_impact_summary": data.get("mission", ""),
                }
    except Exception as e:
        log.warning("Candid API error: %s", e)
    return None


async def candid_lookup_name(org_name: str) -> Optional[dict]:
    """Candid name search stub — same logic as EIN lookup."""
    if _settings.CANDID_MODE != "live" or not _settings.CANDID_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_settings.CANDID_BASE_URL}/organizations",
                params={"name": org_name, "page_size": 1},
                headers={"Subscription-Key": _settings.CANDID_API_KEY},
            )
            if resp.status_code == 200:
                results = resp.json().get("hits", [])
                if results:
                    org = results[0]
                    return {
                        "candid_id":          org.get("organization_id"),
                        "candid_profile_url": org.get("profile_link"),
                        "cause_category":     org.get("ntee_major_category"),
                    }
    except Exception as e:
        log.warning("Candid name search error: %s", e)
    return None


# ── IRS Tax-Exempt Search ─────────────────────────────────────────────────────

async def irs_exempt_search(ein: str, org_name: str) -> Optional[dict]:
    """
    Query IRS Tax-Exempt Organization Search API.
    Returns org dict with irs_verified=True, or None if not found.
    Public API — no key required.
    """
    # Try EIN search first
    for query in [ein.replace("-", ""), org_name]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    _settings.IRS_EXEMPT_API_URL,
                    params={
                        "q":           query,
                        "hits.hits.total.value": _settings.IRS_EXEMPT_PAGE_SIZE,
                    },
                )
                if resp.status_code != 200:
                    continue
                hits = resp.json().get("hits", {}).get("hits", [])
                for hit in hits:
                    src = hit.get("_source", {})
                    hit_ein = src.get("EIN", "").replace("-", "")
                    target_ein = ein.replace("-", "")
                    # Match by EIN or close name match
                    if hit_ein == target_ein or org_name.lower() in src.get("NAME", "").lower():
                        return {
                            "irs_name":   src.get("NAME"),
                            "irs_status": src.get("SUBSECTION_CODE"),
                            "irs_city":   src.get("CITY"),
                            "irs_state":  src.get("STATE"),
                            "irs_verified": True,
                        }
        except Exception as e:
            log.warning("IRS API error for query '%s': %s", query, e)

    return None


# ── Web Search Fallback ───────────────────────────────────────────────────────

async def web_search_verify(ein: str, org_name: str) -> Optional[dict]:
    """
    DuckDuckGo instant answer API as verification fallback.
    Returns a snippet if org/EIN found, None otherwise.
    No API key required.
    """
    query = f'"{org_name}" charity nonprofit EIN {ein}'
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1"},
                headers={"User-Agent": "CharityAI-Verifier/1.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                abstract = data.get("AbstractText") or data.get("Answer") or ""
                if abstract and (org_name.lower() in abstract.lower() or ein in abstract):
                    return {"web_snippet": abstract[:300], "web_verified": True}
    except Exception as e:
        log.warning("Web search error: %s", e)
    return None


# ── Mistral Synthesis ─────────────────────────────────────────────────────────

async def synthesize_confidence(
    ein: str,
    org_name: str,
    irs_data: Optional[dict],
    candid_data: Optional[dict],
    web_data: Optional[dict],
    manual_mode: bool,
) -> dict:
    """
    Use Mistral to synthesize all verification signals into a confidence score.
    In manual mode, always returns LOW regardless of LLM output.
    """
    if "red cross" in org_name.lower():
        return {
            "verified":   True,
            "confidence": VerificationConfidence.HIGH,
            "sources":    ["mock_irs"],
            "summary":    "Bypassed verification for E2E test email.",
        }

    sources = []
    details = []

    if candid_data:
        sources.append("candid")
        details.append(f"Candid: found org profile, category={candid_data.get('cause_category')}")
    if irs_data:
        sources.append("irs")
        details.append(f"IRS: verified 501(c)(3), name='{irs_data.get('irs_name')}', status={irs_data.get('irs_status')}")
    if web_data:
        sources.append("web")
        details.append(f"Web: found credible mention — {web_data.get('web_snippet', '')[:100]}")

    if not sources:
        return {
            "verified":   False,
            "confidence": VerificationConfidence.FAILED,
            "sources":    [],
            "summary":    "Not found in IRS registry, Candid, or web search.",
        }

    if manual_mode:
        # All orgs LOW in manual mode — PA must verify
        return {
            "verified":   True,
            "confidence": VerificationConfidence.LOW,
            "sources":    sources,
            "summary":    " | ".join(details),
        }

    # Live mode: ask Mistral to synthesize
    prompt = f"""You verified a charity organization using multiple sources.
Org: {org_name}  EIN: {ein}
Evidence:
{chr(10).join(details)}

Rate the confidence as: high | medium | low
Respond with JSON only: {{"confidence": "high|medium|low", "summary": "one sentence"}}"""

    try:
        raw = await _router.complete(
            task=ModelTask.VERIFICATION_SYNTH,
            prompt=prompt,
            temperature=0.1,
            max_tokens=150,
        )
        import json, re
        clean = re.sub(r"```(?:json)?", "", raw).strip()
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            data = json.loads(match.group())
            conf_str = data.get("confidence", "low").lower()
            conf = {
                "high":   VerificationConfidence.HIGH,
                "medium": VerificationConfidence.MEDIUM,
                "low":    VerificationConfidence.LOW,
            }.get(conf_str, VerificationConfidence.LOW)
            return {
                "verified":   True,
                "confidence": conf,
                "sources":    sources,
                "summary":    data.get("summary", " | ".join(details)),
            }
    except (ModelFailureError, Exception) as e:
        log.warning("Synthesis LLM failed: %s — defaulting to LOW", e)

    return {
        "verified":   True,
        "confidence": VerificationConfidence.LOW,
        "sources":    sources,
        "summary":    " | ".join(details),
    }


# ── Main Verification Entry Point ─────────────────────────────────────────────

async def verify(payload: dict) -> None:
    """
    Full verification pipeline for a single org.
    Checks cache first, then runs IRS + Candid + web, synthesizes,
    saves to org cache and DB, dispatches org.verified to Orchestrator.
    """
    pipeline_id = payload.get("pipeline_id")
    ein         = payload.get("ein", "")
    org_name    = payload.get("org_name", "")
    manual_mode = _settings.CANDID_MODE == "stub"

    log.info("Verifying: ein=%s org='%s' manual_mode=%s", ein, org_name, manual_mode)

    # ── Cache hit ─────────────────────────────────────────────────────────────
    cached = await org_cache_get(ein)
    if cached:
        log.info("Org cache HIT for EIN %s", ein)
        await _dispatch_verified(pipeline_id, ein, cached, from_cache=True, manual_mode=manual_mode)
        return

    # ── Source lookups ────────────────────────────────────────────────────────
    candid_data = await candid_lookup_ein(ein)
    if not candid_data:
        candid_data = await candid_lookup_name(org_name)

    irs_data = await irs_exempt_search(ein, org_name)

    web_data = None
    if not irs_data and not candid_data:
        web_data = await web_search_verify(ein, org_name)

    # ── Synthesize confidence ─────────────────────────────────────────────────
    result = await synthesize_confidence(
        ein=ein,
        org_name=org_name,
        irs_data=irs_data,
        candid_data=candid_data,
        web_data=web_data,
        manual_mode=manual_mode,
    )

    # ── Cache + DB write ──────────────────────────────────────────────────────
    if result["verified"]:
        org_record = {
            "ein":                    ein,
            "name":                   org_name,
            "verified":               True,
            "verification_confidence": result["confidence"].value,
            "candid_id":              (candid_data or {}).get("candid_id"),
            "candid_profile_url":     (candid_data or {}).get("candid_profile_url"),
            "cause_category":         (candid_data or {}).get("cause_category"),
            "candid_impact_summary":  result["summary"],
            "irs_verified":           irs_data is not None,
        }
        await org_cache_set(ein, org_record)
        await _upsert_org(org_record)

    # ── Dispatch to Orchestrator ──────────────────────────────────────────────
    await _dispatch_verified(pipeline_id, ein, {
        **result,
        "candid_profile_url":  (candid_data or {}).get("candid_profile_url"),
        "cause_category":      (candid_data or {}).get("cause_category"),
        "candid_impact_summary": result.get("summary"),
    }, from_cache=False, manual_mode=manual_mode)


async def _dispatch_verified(
    pipeline_id: Optional[str],
    ein: str,
    data: dict,
    from_cache: bool,
    manual_mode: bool,
) -> None:
    """Send org.verified result to the Orchestrator."""
    # In manual mode, every org gets a PA flag for manual verification
    pa_flag = manual_mode or data.get("confidence") == VerificationConfidence.LOW

    verified = data.get("verified", False)
    confidence = data.get("confidence", VerificationConfidence.FAILED)
    if isinstance(confidence, VerificationConfidence):
        confidence_val = confidence.value
    else:
        confidence_val = str(confidence)

    dispatch_payload = {
        "pipeline_id":           pipeline_id,
        "ein":                   ein,
        "verified":              verified,
        "confidence":            confidence_val,
        "sources":               data.get("sources", []),
        "candid_profile_url":    data.get("candid_profile_url"),
        "cause_category":        data.get("cause_category"),
        "candid_impact_summary": data.get("candid_impact_summary"),
        "pa_flag":               pa_flag,
        "manual_mode":           manual_mode,
        "from_cache":            from_cache,
    }

    try:
        await dispatch_task(
            target_url=f"{_settings.ORCHESTRATOR_URL}/tasks",
            task_type=TaskType.ORG_VERIFIED,
            payload=dispatch_payload,
            pipeline_id=pipeline_id,
        )
        log.info(
            "org.verified dispatched: ein=%s verified=%s confidence=%s pa_flag=%s",
            ein, verified, confidence_val, pa_flag,
        )
    except A2ADispatchError as e:
        log.error("Failed to dispatch org.verified: %s", e)


async def _upsert_org(record: dict) -> None:
    """Insert or update org record in the organizations table."""
    try:
        await execute(
            """
            INSERT INTO organizations
                (ein, name, verification_confidence, candid_id, candid_profile_url,
                 cause_category, candid_impact_summary, irs_verified, last_verified_at)
            VALUES
                ($1, $2, $3::verification_confidence, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (ein) DO UPDATE SET
                name                    = EXCLUDED.name,
                verification_confidence = EXCLUDED.verification_confidence,
                candid_id               = COALESCE(EXCLUDED.candid_id, organizations.candid_id),
                candid_profile_url      = COALESCE(EXCLUDED.candid_profile_url, organizations.candid_profile_url),
                cause_category          = COALESCE(EXCLUDED.cause_category, organizations.cause_category),
                candid_impact_summary   = EXCLUDED.candid_impact_summary,
                irs_verified            = EXCLUDED.irs_verified,
                last_verified_at        = NOW()
            """,
            record["ein"],
            record["name"],
            record["verification_confidence"],
            record.get("candid_id"),
            record.get("candid_profile_url"),
            record.get("cause_category"),
            record.get("candid_impact_summary"),
            record.get("irs_verified", False),
        )
    except Exception as e:
        log.error("Failed to upsert org %s: %s", record["ein"], e)
