# Atom

Atom is a Google Meet recording app that demonstrates the core loop first: provide a Meet link or invite, let the bot join by itself, record audio + video, and send the finished recording over Gmail.

## Current progress

- Google sign-in for end users is working.
- Users, sessions, recordings, and Gmail delivery status are stored in SQLite.
- Meet join flow is working through Playwright.
- Audio + video recording is working and saved as MP4 after the meeting ends.
- Gmail delivery is wired through server-side Gmail SMTP settings.
- Auto-leave when the meeting is alone is implemented.
- Per-user profile and recording library are implemented.
- Optional S3 storage is supported from server-side env vars.
- Smoke tests cover the main API routes.

## What the app does today

1. User signs in with Google.
2. User pastes a Google Meet link.
3. Atom joins the meeting and records it.
4. When the meeting ends, the recording is finalized.
5. The finished file is sent to the user's Gmail address.
6. The finished file also appears in the user library.

## Project layout

- `main.py` - Uvicorn entrypoint.
- `api/routes.py` - FastAPI routes and websocket session handling.
- `api/static/index.html` - Single-page UI.
- `meeting/meet/bot.py` - Playwright meeting join and recording flow.
- `meeting/meet/auth.py` - Persistent Chrome profile sign-in used by the bot.
- `core/users.py` - Google user identities and sessions.
- `core/database.py` - SQLite schema and legacy JSON import.
- `core/storage.py` - Database-backed recording library and S3 upload.
- `core/mailer.py` - Gmail SMTP delivery for finished recordings.
- `scripts/smoke_test.py` - Basic health checks.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Environment

Minimal `.env` values for local development:

```env
GOOGLE_CLIENT_ID=...
ADMIN_TOKEN=change-this-long-random-secret
COOKIE_SECURE=false
COOKIE_SAMESITE=lax
CORS_ORIGINS=
ATOM_CONFIG_DIR=config
ATOM_DB_PATH=config/atom.db
RECORDINGS_DIR=api/static/recordings
DEBUG_DIR=api/static/debug
RECORD_MEETING=true
AGENT_NAME=Atom
GMAIL_SMTP_USER=your-sender@gmail.com
GMAIL_APP_PASSWORD=your-gmail-app-password
GMAIL_FROM_EMAIL=your-sender@gmail.com
APP_BASE_URL=http://127.0.0.1:8000
```

Optional S3 settings for cloud storage:

```env
S3_BUCKET=...
S3_REGION=us-east-1
S3_PREFIX=recordings/
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

## Smoke test

```bash
python scripts/smoke_test.py
```

## Deployment

Atom is deployable as a Docker web service. Use [DEPLOY.md](DEPLOY.md) for the full checklist.

Minimum production requirements:

- Public HTTPS frontend URL, for example `https://atom.abhishek-meena.in`.
- Docker backend URL, for example `https://api.abhishek-meena.in`.
- `GOOGLE_CLIENT_ID` with that URL added to Google OAuth Authorized JavaScript origins.
- `ADMIN_TOKEN` to protect bot-profile setup endpoints.
- Persistent volume mounted at `/app/data` for `atom.db`, recordings, debug captures, and the bot Chrome profile.
- Gmail app password configured through `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD`.
- Google OAuth app published to Production and verified if Google requests it; Testing mode only works for listed test users.

Useful production endpoints:

- `/health` - liveness.
- `/ready` - deployment readiness checks.
- `/admin` - bot profile and service readiness panel.
- `/privacy` and `/terms` - public links for Google OAuth consent review.

GitHub Pages is supported for the static frontend through `.github/workflows/pages.yml`; the backend is configured for Railway through `Dockerfile` and `railway.json`.

## Notes

- Runtime files such as session state, user data, recordings, and browser profile data are ignored by git.
- If old `config/users.json`, `config/sessions.json`, or `config/recordings.json` files exist, Atom imports them into SQLite on startup.
- The first demo focuses on invite -> join -> record -> send. Summaries and transcripts can be layered on later.
