"""
Pydantic v2 schemas for all A2A message payloads in CharityAI.

Every inter-agent message is wrapped in an A2ATask envelope.
Agents send POST /tasks with a JSON body conforming to A2ATask.

Payload types:
  - EmailClassifiedPayload  (Email Watcher → Orchestrator)
  - OrgVerifiedPayload      (Charity Verifier → Orchestrator)
  - OrgScoredPayload        (Prioritizer → Orchestrator)
  - PADecisionPayload       (PA Notification → Orchestrator)
  - RSVPOutcomePayload      (RSVP Monitor → Orchestrator)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────────

class VerificationConfidence(str, Enum):
    HIGH    = "high"
    MEDIUM  = "medium"
    LOW     = "low"
    FAILED  = "failed"


class PADecision(str, Enum):
    APPROVED       = "approved"
    REJECTED       = "rejected"
    AUTO_APPROVED  = "auto_approved"


class PADecisionActor(str, Enum):
    PA             = "PA"
    SYSTEM_TIMEOUT = "SYSTEM_TIMEOUT"


class RSVPOutcome(str, Enum):
    CONFIRMED  = "confirmed"
    DECLINED   = "declined"
    TIMEOUT    = "timeout"
    AMBIGUOUS  = "ambiguous"


class RSVPNextAction(str, Enum):
    FINALIZE            = "finalize"
    CANCEL_AND_PULL_NEXT = "cancel_and_pull_next"


class PipelineState(str, Enum):
    EMAIL_RECEIVED          = "EMAIL_RECEIVED"
    CLASSIFYING             = "CLASSIFYING"
    DEDUP_CHECK             = "DEDUP_CHECK"
    VERIFYING               = "VERIFYING"
    ELIGIBILITY_CHECK       = "ELIGIBILITY_CHECK"
    URGENT_ESCALATED_TO_PA  = "URGENT_ESCALATED_TO_PA"
    SCORING                 = "SCORING"
    IN_PRIORITY_QUEUE       = "IN_PRIORITY_QUEUE"
    FINDING_SLOT            = "FINDING_SLOT"
    NO_SLOTS_REQUEUE        = "NO_SLOTS_REQUEUE"
    SLOT_HELD               = "SLOT_HELD"
    PA_PENDING              = "PA_PENDING"
    PA_APPROVED             = "PA_APPROVED"
    AUTO_APPROVED           = "AUTO_APPROVED"
    PA_REJECTED             = "PA_REJECTED"
    CONFIRMATION_SENT       = "CONFIRMATION_SENT"
    RSVP_PENDING            = "RSVP_PENDING"
    RSVP_CONFIRMED          = "RSVP_CONFIRMED"
    RSVP_TIMEOUT_CANCELLED  = "RSVP_TIMEOUT_CANCELLED"
    NEXT_IN_QUEUE_INVITED   = "NEXT_IN_QUEUE_INVITED"
    BOOKED                  = "BOOKED"
    DROPPED_NOT_CHARITY     = "DROPPED_NOT_CHARITY"
    DROPPED_MISSING_INFO    = "DROPPED_MISSING_INFO"
    DROPPED_DUPLICATE       = "DROPPED_DUPLICATE"
    DROPPED_NOT_VERIFIED    = "DROPPED_NOT_VERIFIED"
    DROPPED_WITHIN_90_DAYS  = "DROPPED_WITHIN_90_DAYS"
    MODEL_FAILURE           = "MODEL_FAILURE"
    AGENT_UNREACHABLE       = "AGENT_UNREACHABLE"


# ── Base A2A Envelope ─────────────────────────────────────────────────────────

class A2ATask(BaseModel):
    """Universal A2A task envelope. All inter-agent messages use this wrapper."""
    task_id:    str = Field(default_factory=lambda: str(uuid4()))
    task_type:  str
    sent_at:    datetime = Field(default_factory=datetime.utcnow)
    payload:    Any


class A2AResponse(BaseModel):
    """Standard response from any agent's POST /tasks endpoint."""
    task_id:    str
    status:     Literal["accepted", "rejected", "error"] = "accepted"
    message:    Optional[str] = None


# ── Payload: Email Watcher → Orchestrator ────────────────────────────────────

class EmailClassifiedPayload(BaseModel):
    """
    Emitted when Email Watcher successfully classifies a charity email.
    task_type: "email.classified"
    """
    email_id:              str
    email_thread_id:       str
    org_name:              str
    ein:                   str
    reason:                str
    urgency_signals:       list[str] = Field(default_factory=list)
    contact_email:         str
    received_at:           datetime
    classifier_confidence: float = Field(ge=0.0, le=1.0)
    raw_subject:           Optional[str] = None


class EmailDroppedPayload(BaseModel):
    """
    Emitted when Email Watcher silently drops or logs a drop.
    task_type: "email.dropped"
    """
    email_id:    str
    drop_reason: str   # not_charity | missing_ein | missing_org_name | missing_reason
    raw_subject: Optional[str] = None
    org_name:    Optional[str] = None
    ein:         Optional[str] = None


# ── Payload: Dedup Guard → Orchestrator ──────────────────────────────────────

class DedupResultPayload(BaseModel):
    """
    task_type: "dedup.result"
    """
    email_id:    str
    pipeline_id: Optional[str] = None   # Existing pipeline ID if merged
    is_duplicate: bool
    is_merged:    bool
    message:      Optional[str] = None


# ── Payload: Charity Verifier → Orchestrator ─────────────────────────────────

class OrgVerifiedPayload(BaseModel):
    """
    Emitted when verification is complete (pass or fail).
    task_type: "org.verified"
    """
    pipeline_id:          str
    ein:                  str
    verified:             bool
    confidence:           VerificationConfidence
    sources:              list[str] = Field(default_factory=list)  # e.g. ["irs", "web"]
    candid_profile_url:   Optional[str] = None
    cause_category:       Optional[str] = None
    candid_impact_summary: Optional[str] = None
    pa_flag:              bool = False   # True = PA gets a manual verification note
    manual_mode:          bool = True    # True while Candid API is pending


# ── Payload: Eligibility Agent → Orchestrator ────────────────────────────────

class EligibilityResultPayload(BaseModel):
    """
    task_type: "eligibility.result"
    """
    pipeline_id:           str
    ein:                   str
    is_eligible:           bool
    appointment_count_90d: int
    has_urgency_signals:   bool
    escalate_to_pa:        bool   # True if ineligible but urgency detected


# ── Payload: Prioritizer → Orchestrator ──────────────────────────────────────

class ScoreBreakdown(BaseModel):
    people_scale:       float = Field(ge=0, le=40)
    wait_time_bump:     float = Field(ge=0, le=25)
    urgency_category:   float = Field(ge=0, le=20)
    confidence_bonus:   float = Field(ge=0, le=15)


class OrgScoredPayload(BaseModel):
    """
    task_type: "org.scored"
    """
    pipeline_id:                str
    ein:                        str
    priority_score:             float = Field(ge=0, le=100)
    score_breakdown:            ScoreBreakdown
    estimated_people_impacted:  Optional[int] = None
    justification:              str
    queue_position:             int


# ── Payload: Calendar Agent → Orchestrator ───────────────────────────────────

class CalendarSlot(BaseModel):
    start:        datetime
    end:          datetime
    gcal_hold_id: str   # Tentative hold event ID


class SlotFoundPayload(BaseModel):
    """
    task_type: "calendar.slot_found"
    """
    pipeline_id:    str
    proposed_slot:  CalendarSlot
    alternatives:   list[CalendarSlot] = Field(default_factory=list)


class SlotNotFoundPayload(BaseModel):
    """
    task_type: "calendar.no_slots"
    """
    pipeline_id:    str
    requeue:        bool = True


# ── Payload: PA Notification → Orchestrator ──────────────────────────────────

class PADecisionPayload(BaseModel):
    """
    Emitted after PA approves/rejects via Slack, or 24hr timeout auto-approves.
    task_type: "pa.decision"
    """
    pipeline_id:        str
    decision:           PADecision
    decided_by:         PADecisionActor
    decided_at:         datetime = Field(default_factory=datetime.utcnow)
    pa_notes:           Optional[str] = None
    rejection_reason:   Optional[str] = None


# ── Payload: Email Composer → Orchestrator ───────────────────────────────────

class EmailSentPayload(BaseModel):
    """
    task_type: "email.sent"
    """
    pipeline_id:    str
    template:       str   # confirmation_rsvp | rsvp_timeout_cancel | rsvp_decline_cancel
    gmail_message_id: str
    sent_at:        datetime = Field(default_factory=datetime.utcnow)


# ── Payload: RSVP Monitor → Orchestrator ─────────────────────────────────────

class RSVPOutcomePayload(BaseModel):
    """
    Emitted after the charity responds to the confirmation email (or times out).
    task_type: "rsvp.outcome"
    """
    pipeline_id:        str
    outcome:            RSVPOutcome
    gcal_event_id:      Optional[str] = None
    rsvp_received_at:   Optional[datetime] = None
    next_action:        RSVPNextAction


# ── Task Type Constants ───────────────────────────────────────────────────────

class TaskType:
    EMAIL_CLASSIFIED    = "email.classified"
    EMAIL_DROPPED       = "email.dropped"
    DEDUP_RESULT        = "dedup.result"
    ORG_VERIFIED        = "org.verified"
    ELIGIBILITY_RESULT  = "eligibility.result"
    ORG_SCORED          = "org.scored"
    SLOT_FOUND          = "calendar.slot_found"
    NO_SLOTS            = "calendar.no_slots"
    PA_DECISION         = "pa.decision"
    EMAIL_SENT          = "email.sent"
    RSVP_OUTCOME        = "rsvp.outcome"
