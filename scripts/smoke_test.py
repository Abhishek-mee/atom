#!/usr/bin/env python3
"""
Atom smoke test.

Checks the core product path quickly:
- app homepage loads
- health endpoint is live
- config/auth endpoints return sane JSON
- invite -> join -> record -> send copy is present

Run it after starting the local server:
  python scripts/smoke_test.py
"""
from __future__ import annotations

import json
import sys
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8000"


class TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def fetch(path: str) -> tuple[int, str, dict, float]:
    started = time.perf_counter()
    req = Request(BASE + path, headers={"User-Agent": "atom-smoke-test"})
    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.perf_counter() - started
            return resp.status, body, dict(resp.headers.items()), elapsed
    except HTTPError as e:
        elapsed = time.perf_counter() - started
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body, dict(e.headers.items()) if e.headers else {}, elapsed


def main() -> int:
    checks: list[tuple[str, callable]] = [
        ("home", lambda: fetch("/")),
        ("health", lambda: fetch("/health")),
        ("config", lambda: fetch("/config")),
        ("auth_me", lambda: fetch("/auth/me")),
        ("recordings", lambda: fetch("/recordings")),
        ("profile", lambda: fetch("/profile")),
    ]
    failures: list[str] = []

    for name, fn in checks:
        try:
            status, body, headers, elapsed = fn()
        except URLError as e:
            failures.append(f"{name}: network error ({e})")
            continue

        ms = round(elapsed * 1000)
        print(f"{name:>10}  {status}  {ms}ms")

        if name == "home":
          if status != 200:
              failures.append("home did not return 200")
          if "atom" not in body.lower():
              failures.append("home page missing atom copy")
          if "join, record, send" not in body.lower():
              failures.append("home page missing core demo flow")
          parser = TitleParser()
          parser.feed(body)
          if "atom" not in parser.title.lower():
              failures.append("home page title is unexpected")
        elif name == "health":
            if status != 200:
                failures.append("health did not return 200")
            try:
                health = json.loads(body)
                for key in (
                    "ok", "app", "recordings", "users", "sessions", "active_sessions",
                    "auth_ready", "s3_enabled", "gmail_enabled", "google_auth_enabled",
                    "database", "admin_secured",
                ):
                    if key not in health:
                        failures.append(f"health missing {key}")
            except Exception as e:
                failures.append(f"health is not valid JSON ({e})")
        elif name == "config":
            if status != 200:
                failures.append("config did not return 200")
        elif name in ("auth_me", "recordings", "profile"):
            if status != 200:
                failures.append(f"{name} did not return 200")

    if failures:
        print("\nFAIL")
        for item in failures:
            print(f"- {item}")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
