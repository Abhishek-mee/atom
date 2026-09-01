param(
  [string]$StatePath = "config\google_state_0.json"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path -LiteralPath $StatePath)) {
  throw "Google state file not found: $StatePath. Run `$env:BROWSER_CHANNEL='chrome'; .\.venv\Scripts\python.exe -m meeting.meet.auth first."
}

railway volume files upload $StatePath /app/data/config/google_state_0.json --overwrite --json

Write-Host "Uploaded $StatePath to /app/data/config/google_state_0.json"
