"""
Redis Channel Names and Key Templates for CharityAI Pipeline.

Usage:
    from infra.redis_channels import PubSubChannels, TimerKeys, CacheKeys

    # Publish to a channel
    await redis.publish(PubSubChannels.EMAIL_CLASSIFIED, payload_json)

    # Set a TTL timer key
    key = TimerKeys.pa_timeout(pipeline_id)
    await redis.setex(key, TimerKeys.PA_TIMEOUT_TTL, "pending")
"""

from __future__ import annotations


class PubSubChannels:
    """Redis pub/sub channel names. One channel per pipeline gate."""

    EMAIL_CLASSIFIED    = "pipeline.email_classified"
    DEDUP_CLEARED       = "pipeline.dedup_cleared"
    VERIFIED            = "pipeline.verified"
    ELIGIBLE            = "pipeline.eligible"
    URGENT_ESCALATED    = "pipeline.urgent_escalated"
    SCORED              = "pipeline.scored"
    SLOT_HELD           = "pipeline.slot_held"
    NO_SLOTS            = "pipeline.no_slots"
    PA_APPROVED         = "pipeline.pa_approved"
    PA_REJECTED         = "pipeline.pa_rejected"
    AUTO_APPROVED       = "pipeline.auto_approved"
    CONFIRMATION_SENT   = "pipeline.confirmation_sent"
    RSVP_CONFIRMED      = "pipeline.rsvp_confirmed"
    RSVP_DECLINED       = "pipeline.rsvp_declined"
    RSVP_TIMEOUT        = "pipeline.rsvp_timeout"
    MODEL_FAILURE       = "pipeline.model_failure"
    AGENT_UNREACHABLE   = "pipeline.agent_unreachable"

    # Audit logger subscribes to ALL events via pattern
    AUDIT_PATTERN       = "pipeline.*"


class TimerKeys:
    """Redis TTL key templates for timeout timers."""

    # PA approval window: 24 hours
    PA_TIMEOUT_TTL = 86_400  # seconds

    # RSVP reply window: 24 hours
    RSVP_TIMEOUT_TTL = 86_400  # seconds

    # Weekly queue bump: 7 days
    QUEUE_BUMP_TTL = 604_800  # seconds

    @staticmethod
    def pa_timeout(pipeline_id: str) -> str:
        """Key expires → orchestrator auto-approves PA decision."""
        return f"pa_timeout:{pipeline_id}"

    @staticmethod
    def rsvp_timeout(pipeline_id: str) -> str:
        """Key expires → orchestrator cancels hold + pulls next from queue."""
        return f"rsvp_timeout:{pipeline_id}"

    @staticmethod
    def queue_bump_cron() -> str:
        """Singleton key. Expiry triggers weekly priority_score +5 bump."""
        return "queue_bump_cron"

    @staticmethod
    def dead_letter(pipeline_id: str) -> str:
        """Dead letter entry for unreachable agents."""
        return f"dead_letter:{pipeline_id}"


class CacheKeys:
    """Redis cache key templates with TTLs."""

    # Candid/IRS org verification cache: 7 days
    ORG_CACHE_TTL = 604_800  # seconds

    # GCal free slots cache: 5 minutes
    GCAL_SLOTS_TTL = 300  # seconds

    @staticmethod
    def org_cache(ein: str) -> str:
        """Cached org verification result. Hit = skip IRS/Candid call."""
        return f"candid_cache:{ein}"

    @staticmethod
    def gcal_slots_cache() -> str:
        """Cached list of free calendar slots."""
        return "gcal_slots_cache"


class DeadLetterQueue:
    """Redis list key for failed A2A dispatch retries."""

    KEY = "dead_letter_queue"
