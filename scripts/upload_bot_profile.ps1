param(
  [string]$ProfilePath = "config\chrome_profile"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path -LiteralPath $ProfilePath)) {
  throw "Profile path not found: $ProfilePath. Run `python -m meeting.meet.auth` first."
}

railway volume files upload $ProfilePath /app/data/config/chrome_profile --overwrite --json

Write-Host "Uploaded $ProfilePath to /app/data/config/chrome_profile"
