# ============================================================================
# CharityAI Makefile — Development & Deployment Commands
# ============================================================================

.PHONY: help install test dev up down schema tokens lint docker-build docker-up

VENV      = .venv
PYTHON    = $(VENV)/Scripts/python
PIP       = $(VENV)/Scripts/pip
PYTEST    = $(VENV)/Scripts/pytest

# ── Help ─────────────────────────────────────────────────────────────────────

help:
	@echo "CharityAI — Available Commands:"
	@echo ""
	@echo "  Setup"
	@echo "    make install        Install Python dependencies in .venv"
	@echo "    make tokens         Run OAuth2 flow for Gmail + GCal"
	@echo ""
	@echo "  Development"
	@echo "    make up             Start local Postgres + Redis (Docker)"
	@echo "    make schema         Apply DB schema (requires DB running)"
	@echo "    make schema-local   Apply schema to local Docker Postgres"
	@echo "    make test           Run all unit tests"
	@echo "    make lint           Type-check with pyright"
	@echo "    make dev            Start ALL agents locally (PowerShell)"
	@echo ""
	@echo "  Docker"
	@echo "    make docker-build   Build all agent Docker images"
	@echo "    make docker-up      Start full stack (production compose)"
	@echo "    make docker-down    Stop all containers"

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	python -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "✅  Virtual environment ready"

tokens:
	$(PYTHON) scripts/generate_tokens.py

# ── Database ─────────────────────────────────────────────────────────────────

up:
	docker compose -f infra/docker-compose.yml up -d
	@echo "✅  Local Postgres + Redis running"

down:
	docker compose -f infra/docker-compose.yml down

schema:
	$(PYTHON) scripts/apply_schema.py

schema-local:
	$(PYTHON) scripts/apply_schema.py --local

# ── Testing ───────────────────────────────────────────────────────────────────

test:
	$(PYTEST) tests/unit/ -v --tb=short

test-cov:
	$(PYTEST) tests/unit/ -v --tb=short --cov=agents --cov=shared --cov-report=term-missing

# ── Linting ───────────────────────────────────────────────────────────────────

lint:
	$(PYTHON) -m pyright agents/ shared/

# ── Local Multi-agent Start ───────────────────────────────────────────────────

dev:
	@echo "Starting all 9 agents in separate terminals..."
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.orchestrator.main:app --port 8000 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.email_watcher.main:app --port 8001 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.dedup_guard.main:app --port 8002 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.charity_verifier.main:app --port 8003 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.eligibility_agent.main:app --port 8004 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.prioritizer.main:app --port 8005 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.calendar_agent.main:app --port 8006 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.pa_notification.main:app --port 8007 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.email_composer.main:app --port 8008 --reload'"
	powershell -Command "Start-Process powershell -ArgumentList '-NoExit -Command cd \"$(CURDIR)\"; .venv\\Scripts\\activate; uvicorn agents.rsvp_monitor.main:app --port 8009 --reload'"
	@echo "✅  All agents starting... check individual windows"

# ── Docker Production ─────────────────────────────────────────────────────────

docker-build:
	docker compose -f infra/docker-compose.prod.yml build --parallel

docker-up:
	docker compose -f infra/docker-compose.prod.yml up -d

docker-down:
	docker compose -f infra/docker-compose.prod.yml down
