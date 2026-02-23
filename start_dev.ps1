$agents = @(
    "orchestrator", "email_watcher", "dedup_guard", "charity_verifier",
    "eligibility_agent", "prioritizer", "calendar_agent", "pa_notification",
    "email_composer", "rsvp_monitor"
)
$port = 8000
foreach ($agent in $agents) {
    echo "Starting $agent on port $port..."
    Start-Process powershell -ArgumentList "-NoExit -Command `"cd '$pwd'; .venv\Scripts\activate; uvicorn agents.$agent.main:app --port $port --reload`""
    $port++
}
echo "All 10 agents have been started in new windows!"
