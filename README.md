---
title: CharityAI
emoji: 💌
colorFrom: indigo
colorTo: purple
sdk: docker
dockerfile: Dockerfile.demo
app_port: 7860
pinned: false
license: mit
short_description: Autonomous email-to-appointment pipeline (9 agents, state machine)
---

# CharityAI — Autonomous Email-to-Appointment Pipeline

CharityAI is a modular, multi-agent AI system that manages a charity appointment pipeline end-to-end without human triage. It watches an inbox, classifies, verifies (IRS), scores impact, schedules a slot, gates a human-in-the-loop on Slack, and tracks the RSVP — all through a strict state machine with full audit logging.

## Two ways to run this

| Mode | Where | What runs | Infra |
|---|---|---|---|
| **Showcase demo** | `Dockerfile.demo` → HF Spaces | Single FastAPI process, walks the state machine with synthetic data | none |
| **Full pipeline** | `make docker-up` → local | All 9 agents + Postgres + Redis + Ollama (Phi3/Mistral/Llama3) | Docker Compose |

The showcase is what's deployed publicly (free-tier hosts can't run a 9-service distributed system + Postgres + Redis + Ollama). The full pipeline is the real product and lives in `agents/`, `shared/`, and `infra/`.

## Showcase endpoints (deployed)

```
GET  /                  →  landing page (architecture overview, how-to-demo)
GET  /health            →  liveness check
GET  /architecture      →  full state machine + 9-agent map as JSON
POST /demo/walkthrough  →  simulate one pipeline run with stub data
GET  /docs              →  Swagger UI
```

```bash
curl -X POST $URL/demo/walkthrough \
  -H "Content-Type: application/json" \
  -d '{"org_name":"Hope Community Kitchen","ein":"12-3456789","reason":"food bank Q4"}'
```

## Architecture (full pipeline, 9 gates)

1. **Orchestrator** (Gate 1) — central state machine, A2A dispatch, atomic DB writes
2. **Email Watcher** (Gate 2) — Gmail polling, **Phi3** classify, **Mistral** structured extract
3. **Dedup Guard** (Gate 3) — exact-match + semantic (same-org) dedup
4. **Charity Verifier** (Gate 4) — IRS Tax-Exempt API + web-search fallback, **Mistral** synthesis
5. **Eligibility Agent** (Gate 5) — 90-day cooldown + urgency escalation
6. **Prioritizer** (Gate 6) — **Llama3** impact extraction, weighted ranking + weekly bumps
7. **Calendar Agent** (Gate 7) — Google Calendar 30-min slot search, tentative holds
8. **PA Notification** (Gate 8) — Slack Block Kit approve/reject + 24h auto-approve timeout
9. **Email Composer + RSVP Monitor** (Gate 9) — **Llama3** drafts confirmation/rejection, **Phi3** classifies the RSVP reply, finalizes or releases the calendar hold

Switch the LLM provider with the `LLM_PROVIDER` env var:

- `LLM_PROVIDER=ollama` (default) → local Ollama on `:11434`
- `LLM_PROVIDER=anthropic` → Claude Haiku for fast tasks (classify, RSVP intent), Sonnet for everything else (extract, score, draft)

The cloud demo uses `LLM_PROVIDER=anthropic` so it can run without a local Ollama install. See `shared/llm.py`.

## Prerequisites (full local pipeline only)

1. **Docker & Docker Compose** — for Postgres + Redis
2. **Ollama** running on `localhost:11434` with `phi3`, `mistral`, `llama3` pulled (skip if `LLM_PROVIDER=anthropic`)
3. **Google OAuth2** credentials in `auth/gmail_oauth.json` and `auth/gcalendar_oauth.json`; run `make tokens`
4. **Slack App** — create at <https://api.slack.com/apps>, copy Client ID / Secret / Signing Secret / Verification Token / Bot Token into `.env`

## Quick start (full pipeline, local)

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
# source .venv/bin/activate                        # macOS/Linux
pip install -r requirements.txt

cp .env.example .env                                # then fill in real values
make up                                             # Postgres + Redis
make schema                                         # apply infra/schema.sql
make tokens                                         # one-time Google OAuth
make dev                                            # spins up all 9 agents
```

## Quick start (showcase demo, local)

```bash
pip install -r requirements-demo.txt
LLM_PROVIDER=anthropic uvicorn app.demo:app --reload --port 7860
# open http://localhost:7860
```

## Deploy the showcase to Hugging Face Spaces

1. Create a new Space at <https://huggingface.co/new-space> with `SDK = Docker`. Name it e.g. `charityai-demo`.
2. Push this repo to the Space (or link the GitHub repo via the Spaces UI). HF reads the `dockerfile: Dockerfile.demo` field from the README YAML and builds from `Dockerfile.demo`.
3. In **Settings → Variables and secrets**, add:
   - `ANTHROPIC_API_KEY` (secret) — required even though the demo doesn't make LLM calls (it imports `shared.llm` which checks the var when `LLM_PROVIDER=anthropic`)
4. The Space exposes port 7860; visit it for the landing page, or `/docs` for Swagger.

## Testing

```bash
make test                                          # unit + integration + e2e
```

Unit + integration tests have ~100% logic coverage; e2e covers the full happy path through all 9 gates with mocked external APIs.

## Security note

The `.env.example` template uses placeholder values for Slack credentials. **Real values belong only in `.env`** (gitignored), or in your deploy platform's secrets UI. If you ever paste a real key into a tracked file, rotate it immediately.
