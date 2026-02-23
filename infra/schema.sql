-- ============================================================
-- CharityAI — PostgreSQL Schema DDL
-- Apply with: psql $DATABASE_URL -f infra/schema.sql
--             OR python scripts/apply_schema.py
-- ============================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- ENUMS
-- ============================================================

CREATE TYPE verification_confidence AS ENUM ('high', 'medium', 'low', 'failed');

CREATE TYPE meeting_status AS ENUM ('booked', 'completed', 'cancelled', 'no-rsvp');

CREATE TYPE pipeline_state AS ENUM (
    'EMAIL_RECEIVED',
    'CLASSIFYING',
    'DEDUP_CHECK',
    'VERIFYING',
    'ELIGIBILITY_CHECK',
    'URGENT_ESCALATED_TO_PA',
    'SCORING',
    'IN_PRIORITY_QUEUE',
    'FINDING_SLOT',
    'NO_SLOTS_REQUEUE',
    'SLOT_HELD',
    'PA_PENDING',
    'PA_APPROVED',
    'AUTO_APPROVED',
    'PA_REJECTED',
    'CONFIRMATION_SENT',
    'RSVP_PENDING',
    'RSVP_CONFIRMED',
    'RSVP_TIMEOUT_CANCELLED',
    'NEXT_IN_QUEUE_INVITED',
    'BOOKED',
    'DROPPED_NOT_CHARITY',
    'DROPPED_MISSING_INFO',
    'DROPPED_DUPLICATE',
    'DROPPED_NOT_VERIFIED',
    'DROPPED_WITHIN_90_DAYS',
    'MODEL_FAILURE',
    'AGENT_UNREACHABLE'
);

CREATE TYPE pa_response_type AS ENUM ('approved', 'rejected', 'auto_approved');

CREATE TYPE drop_reason AS ENUM (
    'not_charity',
    'missing_ein',
    'missing_org_name',
    'missing_reason',
    'exact_duplicate',
    'not_in_irs_or_web',
    'within_90_days'
);

CREATE TYPE queue_status AS ENUM ('waiting', 'invited', 'expired');

-- ============================================================
-- TABLE 1: organizations
-- Cached org data from Candid / IRS (refreshed every 7 days)
-- ============================================================

CREATE TABLE IF NOT EXISTS organizations (
    ein                     VARCHAR(12)             PRIMARY KEY,
    name                    TEXT                    NOT NULL,
    candid_id               TEXT,
    verification_confidence verification_confidence NOT NULL DEFAULT 'low',
    candid_profile_url      TEXT,
    cause_category          TEXT,
    candid_impact_summary   TEXT,
    irs_verified            BOOLEAN                 NOT NULL DEFAULT FALSE,
    last_verified_at        TIMESTAMPTZ             NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ             NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE organizations IS
    'Cached verified org data. TTL managed by Redis; re-verified when cache misses.';

-- ============================================================
-- TABLE 2: appointment_history
-- Source of truth for 90-day rolling window eligibility rule.
-- Single SQL COUNT query: WHERE ein=$1 AND scheduled_at > NOW() - INTERVAL '90 days'
-- ============================================================

CREATE TABLE IF NOT EXISTS appointment_history (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ein             VARCHAR(12)     NOT NULL REFERENCES organizations(ein),
    scheduled_at    TIMESTAMPTZ     NOT NULL,
    meeting_status  meeting_status  NOT NULL DEFAULT 'booked',
    outcome_notes   TEXT,
    gcal_event_id   TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_appt_history_ein_scheduled
    ON appointment_history (ein, scheduled_at DESC);

COMMENT ON TABLE appointment_history IS
    '90-day eligibility source of truth. COUNT > 0 in last 90 days = ineligible.';

-- ============================================================
-- TABLE 3: pipeline
-- One row per active email. Owns the canonical pipeline state.
-- ============================================================

CREATE TABLE IF NOT EXISTS pipeline (
    id                  UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id            TEXT                NOT NULL UNIQUE,   -- Gmail message ID
    email_thread_id     TEXT,
    ein                 VARCHAR(12)         REFERENCES organizations(ein),
    org_name            TEXT,
    contact_email       TEXT,
    reason              TEXT,
    urgency_signals     TEXT[],
    current_state       pipeline_state      NOT NULL DEFAULT 'EMAIL_RECEIVED',
    priority_score      NUMERIC(5,2),
    proposed_slot       TIMESTAMPTZ,
    gcal_event_id       TEXT,
    gcal_hold_event_id  TEXT,               -- Tentative hold ID (released on reject/timeout)
    pa_notified_at      TIMESTAMPTZ,
    pa_response         pa_response_type,
    pa_notes            TEXT,
    rsvp_sent_at        TIMESTAMPTZ,
    rsvp_received_at    TIMESTAMPTZ,
    received_at         TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_state ON pipeline (current_state);
CREATE INDEX IF NOT EXISTS idx_pipeline_ein   ON pipeline (ein);

COMMENT ON TABLE pipeline IS
    'Canonical per-email pipeline state row. One row per Gmail message.';

-- ============================================================
-- TABLE 4: priority_queue
-- Ranked waiting list. Orgs enter here when eligible and scored.
-- ============================================================

CREATE TABLE IF NOT EXISTS priority_queue (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ein             VARCHAR(12)     NOT NULL REFERENCES organizations(ein),
    pipeline_id     UUID            NOT NULL REFERENCES pipeline(id),
    priority_score  NUMERIC(5,2)    NOT NULL DEFAULT 0,
    wait_bump_total NUMERIC(5,2)    NOT NULL DEFAULT 0,    -- Cumulative wait bump applied
    entered_queue_at TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    last_bumped_at  TIMESTAMPTZ,
    status          queue_status    NOT NULL DEFAULT 'waiting',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_queue_status_score
    ON priority_queue (status, priority_score DESC);

COMMENT ON TABLE priority_queue IS
    'Ranked waiting list. Top org by priority_score is invited to calendar scheduling.';

-- ============================================================
-- TABLE 5: dropped_emails
-- Silent and logged drop records. Never deleted.
-- ============================================================

CREATE TABLE IF NOT EXISTS dropped_emails (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id            TEXT        NOT NULL,
    drop_reason         drop_reason NOT NULL,
    raw_subject         TEXT,
    org_name_extracted  TEXT,
    ein_extracted       TEXT,
    raw_snippet         TEXT,       -- First 500 chars of email body
    pipeline_id         UUID        REFERENCES pipeline(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dropped_reason ON dropped_emails (drop_reason, created_at DESC);

COMMENT ON TABLE dropped_emails IS
    'All dropped emails with reason codes. Immutable — never update or delete rows.';

-- ============================================================
-- TABLE 6: audit_log
-- Immutable event log. Append-only. One row per state transition.
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID        REFERENCES pipeline(id),
    event_type  TEXT        NOT NULL,
    actor       TEXT        NOT NULL,   -- agent name, 'PA', or 'SYSTEM_TIMEOUT'
    from_state  pipeline_state,
    to_state    pipeline_state,
    details     JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_pipeline ON audit_log (pipeline_id, created_at ASC);

COMMENT ON TABLE audit_log IS
    'Immutable event trail. No UPDATE or DELETE ever. Append only.';

-- ============================================================
-- Updated_at trigger (for pipeline + priority_queue)
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_pipeline_updated_at
    BEFORE UPDATE ON pipeline
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER set_queue_updated_at
    BEFORE UPDATE ON priority_queue
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================
-- Verify
-- ============================================================
DO $$
BEGIN
    RAISE NOTICE 'CharityAI schema applied successfully. Tables: organizations, appointment_history, pipeline, priority_queue, dropped_emails, audit_log';
END $$;
