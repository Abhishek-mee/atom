# Atom - meeting recorder. Self-contained: Python + Chromium + ffmpeg.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    ATOM_CONFIG_DIR=/app/data/config \
    ATOM_DB_PATH=/app/data/atom.db \
    RECORDINGS_DIR=/app/data/recordings \
    DEBUG_DIR=/app/data/debug \
    PORT=8000

# System deps: ffmpeg for muxing, Chromium deps from Playwright, and fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg xvfb fonts-liberation ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# App code + Python dependencies.
COPY . .
RUN pip install --upgrade pip && pip install .

# Install Chromium + system deps (works on any arch, incl. ARM64).
# The container runs the headless recording bot, so Chromium is the standard choice.
# (Real Google Chrome is only used for the visible sign-in window on macOS.)
RUN playwright install --with-deps chromium

# Persisted login profile, database, and recordings live here.
RUN mkdir -p /app/data/config/chrome_profile /app/data/recordings /app/data/debug

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["python", "main.py"]
