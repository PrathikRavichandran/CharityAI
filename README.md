# CharityAI: Autonomous Email-to-Appointment Pipeline

CharityAI is a highly modular, multi-agent AI system designed to manage a charity appointment pipeline entirely autonomously. It watches an inbox for charity requests, classifies, verifies, scores, schedules, notifies a human-in-the-loop (HITL), and manages the confirmation/RSVP process.

## 🌟 Architecture Overview

The system runs 9 isolated containerized agents communicating via an HTTP Agent-to-Agent (A2A) protocol, orchestrated by a central State Machine.

1.  **Orchestrator (Gate 1):** The central brain. Manages the state machine (20+ states), atomic DB writes, and dispatches tasks to other agents.
2.  **Email Watcher (Gate 2):** Polls Gmail, uses **Phi3** to classify if an email is from a charity, uses **Mistral** to extract structured data (EIN, org name, reason).
3.  **Dedup Guard (Gate 3):** Deterministic exact-match and semantic (same org) deduplication logic.
4.  **Charity Verifier (Gate 4):** Uses IRS Tax-Exempt API (or web search fallback) to verify EINs. Uses **Mistral** to synthesize a confidence score.
5.  **Eligibility Agent (Gate 5):** SQL-based check ensuring the org hasn't had an appointment in 90 days, with an urgency escalation path.
6.  **Prioritizer (Gate 6):** Uses **Llama3** to extract impact scale, ranks orgs based on a robust rubric (people scale, wait time, urgency, confidence), and manages a priority queue with weekly bumps.
7.  **Calendar Agent (Gate 7):** Finds 30-min slots 14–28 days out in Google Calendar and places tentative holds.
8.  **PA Notification (Gate 8):** Drafts a summary via **Llama3** and sends a Slack Block Kit message to a Personal Assistant (PA) for Approve/Reject. Handles auto-approvals on a 24h timeout.
9.  **Email Composer & RSVP Monitor:** Drafts and sends confirmation/rejection emails via **Llama3**. Watches the reply thread, uses **Phi3** to classify the RSVP intent (confirm/decline), and finalizes or cancels the Calendar hold appropriately.

## 🛠️ Prerequisites

1.  **Docker & Docker Compose:** Required to run PostgreSQL and Redis.
2.  **Ollama:** Must be running locally on `localhost:11434` with the following models pulled:
    ```bash
    ollama run phi3
    ollama run mistral
    ollama run llama3
    ```
3.  **Google OAuth2 Credentials:** You need `auth/gmail_token.json` and `auth/gcalendar_token.json`.
    *   Create a Google Cloud Project → APIs & Services → Credentials.
    *   Create an **OAuth 2.0 Client ID (Desktop app)**.
    *   Download JSON to `auth/gmail_oauth.json`.
    *   Run `make tokens` to generate the tokens.
4.  **Slack App:** Create an app, get the Bot Token (`xoxb-...`), and set up Interactive Components (Webhook URL).

## 🚀 Quick Start (Local Development)

1.  **Setup Environment:**
    ```bash
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    ```

2.  **Configure `.env`:**
    Copy `.env.local` to `.env`. Ensure your Slack and Google credentials are correct.
    ```bash
    cp .env.local .env
    ```

3.  **Start Infrastructure:**
    Starts PostgreSQL and Redis via Docker.
    ```bash
    make up
    ```

4.  **Apply Database Schema:**
    Applies the schema to the local Postgres DB.
    ```bash
    make schema
    ```

5.  **Run All Agents:**
    This command opens 10 separate PowerShell windows (9 agents + 1 audit sidecar).
    ```bash
    make dev
    ```

## 🐳 Docker Deployment (Production)

To run the entire suite of 9 agents + PostgreSQL + Redis in Docker:

```bash
# Build all images
make docker-build

# Start the cluster
make docker-up
```

## 🧪 Testing

The codebase includes comprehensive unit tests across all phases, achieving ~100% logic coverage, plus a full end-to-end integration test.

```bash
# Run all tests
make test
```
