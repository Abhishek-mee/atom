# Deploy Atom

Atom is a stateful web app. GitHub Pages can host the static web UI, but the Meet bot, database, Gmail delivery, and WebSocket session need the FastAPI backend running on a Docker-capable host.

Recommended production split with Railway:

- Frontend: `https://atom.abhishek-meena.in` on GitHub Pages.
- Backend: `https://api.abhishek-meena.in` on Railway.

## Required services

- A public HTTPS URL for the web app.
- Google OAuth Web Client ID for end-user sign-in.
- Gmail sender account with a Gmail app password.
- Persistent disk for `/app/data`, because it stores:
  - `atom.db`
  - the bot Chrome profile
  - local recordings when S3 is not enabled
- Optional S3 bucket for recording storage.

## Railway backend

Railway will detect the root `Dockerfile` and build the backend container. This repo also includes `railway.json` with a `/health` deployment health check.

1. Push this project to GitHub.
2. In Railway, create a new project from the GitHub repo.
3. Select the backend service that uses the root `Dockerfile`.
4. Add a Railway volume and mount it at:

```text
/app/data
```

5. Add the environment variables from the next section.
6. Open the service networking settings and generate a Railway domain first.
7. Add the custom backend domain:

```text
api.abhishek-meena.in
```

Railway will show the exact DNS records. Add both records at your DNS provider:

```text
CNAME api -> <railway-provided-target>
TXT <railway-provided-name> -> <railway-provided-value>
```

Railway requires both the CNAME and TXT records for custom domain verification.

## Environment

Set these values on your host:

```env
APP_BASE_URL=https://api.abhishek-meena.in
GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
ADMIN_TOKEN=make-a-long-random-secret
COOKIE_SECURE=true
COOKIE_SAMESITE=none
CORS_ORIGINS=https://atom.abhishek-meena.in

ATOM_CONFIG_DIR=/app/data/config
ATOM_DB_PATH=/app/data/atom.db
RECORDINGS_DIR=/app/data/recordings
DEBUG_DIR=/app/data/debug
RECORD_MEETING=true
AGENT_NAME=Atom
REC_WIDTH=854
REC_HEIGHT=480
REC_FORMAT=webm
BROWSER_CHANNEL=

GMAIL_SMTP_USER=your-sender@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
GMAIL_FROM_EMAIL=your-sender@gmail.com
EMAIL_ATTACH_LIMIT_MB=0
```

For S3-backed playback links, also set:

```env
S3_BUCKET=your-atom-recordings
S3_REGION=us-east-1
S3_PREFIX=recordings/
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
S3_PRESIGN_DAYS=7
S3_KEEP_LOCAL=false
```

## Google OAuth setup for public users

Atom cannot automatically connect every user's Google Drive while the Google OAuth app is in Testing mode. Testing mode only allows developer-approved test users. For public Gmail users, move the OAuth app through Google's production and verification flow.

In Google Cloud Console, configure the OAuth consent screen:

1. Set User type to External.
2. Add the app name, user support email, developer contact email, and app logo if used.
3. Add these public app links:

```text
Home page: https://atom.abhishek-meena.in
Privacy policy: https://atom.abhishek-meena.in/privacy
Terms of service: https://atom.abhishek-meena.in/terms
```

4. Add only the scopes Atom needs:

```text
openid
email
profile
https://www.googleapis.com/auth/drive.file
```

5. Add `abhishek-meena.in` as an authorized domain.
6. Publish the app to Production.
7. If Google marks the scopes as sensitive or requests verification, submit the verification form and include a short demo video showing:
   - Google sign-in.
   - User granting Drive permission.
   - User pasting a Google Meet link.
   - Atom uploading the finished recording to that user's Drive.
   - Atom deleting the temporary server copy.

Create an OAuth Web Client and add your deployed frontend origins:

```text
https://atom.abhishek-meena.in
https://abhishek-mee.github.io
```

Use the resulting client ID as `GOOGLE_CLIENT_ID`.

Until the OAuth app is published and verification is accepted, add each Gmail address under Test users or Google will show `Error 403: access_denied`.

## GitHub Pages frontend

This repo includes a GitHub Actions workflow at `.github/workflows/pages.yml`. It publishes `api/static/index.html` plus `pages/config.js` and `pages/CNAME`.

The Pages subdomain is already set in:

```text
pages/CNAME
```

Current value:

```text
atom.abhishek-meena.in
```

Before publishing, make sure `pages/config.js` points to your deployed backend:

```js
window.ATOM_API_BASE = "https://api.abhishek-meena.in";
```

In GitHub repository settings:

1. Go to Settings -> Pages.
2. Set source to GitHub Actions.
3. Run the `Deploy GitHub Pages` workflow.
4. Set the custom domain to `atom.abhishek-meena.in` if GitHub asks for it.

At your DNS provider, add this record:

```text
Type: CNAME
Name: atom
Value: <your-github-username>.github.io
```

Do not include the repository name in the CNAME target.

After Railway gives you the final backend domain, confirm this file still matches:

```text
pages/config.js
```

Expected:

```js
window.ATOM_API_BASE = "https://api.abhishek-meena.in";
```

## Gmail setup

For the sending Gmail account:

1. Enable 2-Step Verification.
2. Create an App Password.
3. Put that value in `GMAIL_APP_PASSWORD`.

Atom sends the finished recording to the signed-in user's Google email. If the file is larger than `EMAIL_ATTACH_LIMIT_MB`, the email contains the playback link instead of attaching the file.

## Bot Google profile

After deployment, open:

```text
https://api.abhishek-meena.in/admin
```

Enter `ADMIN_TOKEN`, then check readiness. The bot profile must show ready before Atom can join restricted Google Meets.

On hosts that cannot show an interactive browser from the container, create the bot profile locally, then copy `config/chrome_profile` into the persistent `/app/data/config/chrome_profile` volume on the server.

## Deploy with Docker Compose

```bash
cp .env.example .env
docker compose up --build -d
```

Open:

```text
http://127.0.0.1:8000
http://127.0.0.1:8000/admin
```

## Verify

Run:

```bash
python scripts/smoke_test.py
```

Production checks:

```text
/health
/ready
/admin
```
