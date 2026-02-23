# ============================================================================
# start_all.ps1 — Start ALL CharityAI agents locally in one command
# 
# Usage (from project root with .venv active):
#   powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
#
# Prerequisites:
#   1. .venv is set up: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
#   2. Postgres + Redis running: docker compose -f infra\docker-compose.yml up -d
#   3. Schema applied: python scripts\apply_schema.py --local
#   4. OAuth tokens generated: python scripts\generate_tokens.py
# ============================================================================

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "$Root\.venv\Scripts\python.exe"
$Uvicorn = "$Root\.venv\Scripts\uvicorn.exe"

Write-Host "🚀 Starting CharityAI — All Agents" -ForegroundColor Cyan
Write-Host "   Root: $Root" -ForegroundColor Gray
Write-Host ""

# Agent definitions: [label, module:app, port]
$agents = @(
    @{ label="Orchestrator";      module="agents.orchestrator.main:app";      port=8000 },
    @{ label="Email Watcher";     module="agents.email_watcher.main:app";     port=8001 },
    @{ label="Dedup Guard";       module="agents.dedup_guard.main:app";       port=8002 },
    @{ label="Charity Verifier";  module="agents.charity_verifier.main:app";  port=8003 },
    @{ label="Eligibility Agent"; module="agents.eligibility_agent.main:app"; port=8004 },
    @{ label="Prioritizer";       module="agents.prioritizer.main:app";       port=8005 },
    @{ label="Calendar Agent";    module="agents.calendar_agent.main:app";    port=8006 },
    @{ label="PA Notification";   module="agents.pa_notification.main:app";   port=8007 },
    @{ label="Email Composer";    module="agents.email_composer.main:app";    port=8008 },
    @{ label="RSVP Monitor";      module="agents.rsvp_monitor.main:app";      port=8009 }
)

foreach ($agent in $agents) {
    $title = "CharityAI | $($agent.label) :$($agent.port)"
    $cmd   = "Set-Location '$Root'; `$Host.UI.RawUI.WindowTitle = '$title'; & '$Uvicorn' $($agent.module) --host 0.0.0.0 --port $($agent.port) --reload"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd
    Write-Host "  ✅ Started $($agent.label) on port $($agent.port)" -ForegroundColor Green
    Start-Sleep -Milliseconds 300   # Small stagger to avoid port bind race
}

Write-Host ""
Write-Host "All 10 agents are starting! Wait ~15 seconds for full boot." -ForegroundColor Cyan
Write-Host ""
Write-Host "Health check URLs:" -ForegroundColor Yellow
foreach ($agent in $agents) {
    Write-Host "  http://localhost:$($agent.port)/health" -ForegroundColor Gray
}

# Also start Audit Sidecar in a separate window
$auditCmd = "Set-Location '$Root'; `$Host.UI.RawUI.WindowTitle = 'CharityAI | Audit Sidecar'; & '$Python' -m agents.orchestrator.audit_sidecar"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $auditCmd
Write-Host "  ✅ Started Audit Sidecar" -ForegroundColor Green

Write-Host ""
Write-Host "💡 Tip: Run ngrok to expose PA Notification for Slack webhooks:" -ForegroundColor DarkYellow
Write-Host "   ngrok http 8007" -ForegroundColor Gray
Write-Host "   Then set SLACK_ACTIONS_URL = https://<ngrok-url>/slack/actions in Slack App settings" -ForegroundColor Gray
