"""
Google sign-in for Atom — uses REAL Google Chrome with a persistent profile.

Instead of a throwaway "testing" Chromium, this launches the actual Google
Chrome installed on the machine (channel="chrome") with a dedicated profile
directory. The login persists across runs, so you sign in once and Atom reuses
that profile for every meeting.

Usage:
  python -m meeting.meet.auth            # sign in slot 0 (default)
  python -m meeting.meet.auth --slot 1   # additional account for scale

Profiles live in:
  config/chrome_profile      ← slot 0
  config/chrome_profile_1    ← slot 1
  ...
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.getenv("ATOM_CONFIG_DIR", "config"))

# Launch args shared by auth + bot to look like a normal Chrome session
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


def browser_channel() -> str | None:
    """Real Google Chrome ('chrome') where available (e.g. macOS/x86 Linux),
    else None → Playwright's bundled Chromium (works on any arch, incl. ARM64).
    Set BROWSER_CHANNEL=chrome locally for a real-Chrome sign-in window."""
    ch = os.getenv("BROWSER_CHANNEL", "").strip()
    return ch or None


def _launch_kwargs() -> dict:
    """channel kwarg only if a real channel is configured."""
    ch = browser_channel()
    return {"channel": ch} if ch else {}


def profile_dir(slot: int = 0) -> Path:
    return CONFIG_DIR / ("chrome_profile" if slot == 0 else f"chrome_profile_{slot}")


def storage_state_path(slot: int = 0) -> Path:
    return CONFIG_DIR / f"google_state_{slot}.json"


def clear_profile(slot: int = 0) -> bool:
    """Delete a profile's stored login so a different account can sign in."""
    import shutil
    p = profile_dir(slot)
    state = storage_state_path(slot)
    cleared = False
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
        logger.info("Cleared profile %s", p)
        cleared = True
    if state.exists():
        state.unlink(missing_ok=True)
        logger.info("Cleared storage state %s", state)
        cleared = True
    return cleared


def clear_profile_locks(slot: int = 0) -> list[str]:
    """Remove stale Chromium singleton locks from a persistent profile.

    A failed or interrupted Railway auth attempt can leave these files behind.
    Chromium then refuses to reopen the profile even though no useful browser
    session is running.
    """
    p = profile_dir(slot)
    removed: list[str] = []
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        target = p / name
        if not target.exists() and not target.is_symlink():
            continue
        try:
            target.unlink()
            removed.append(name)
        except OSError as exc:
            logger.warning("Could not remove Chromium profile lock %s: %s", target, exc)
    return removed


def _profile_has_login(p: Path) -> bool:
    """A signed-in Chrome profile has a Default/Cookies (or Network/Cookies) file."""
    if not p.exists():
        return False
    for cookie in [p / "Default" / "Cookies", p / "Default" / "Network" / "Cookies"]:
        if cookie.exists() and cookie.stat().st_size > 1000:
            return True
    return False


def list_profile_slots() -> list[int]:
    """Return slot numbers that have a logged-in profile."""
    slots: set[int] = set()
    for state in CONFIG_DIR.glob("google_state_*.json"):
        try:
            slot = int(state.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if state.exists() and state.stat().st_size > 100:
            slots.add(slot)
    for d in CONFIG_DIR.glob("chrome_profile*"):
        if not d.is_dir():
            continue
        name = d.name
        slot = 0 if name == "chrome_profile" else int(name.rsplit("_", 1)[-1])
        if _profile_has_login(d):
            slots.add(slot)
    return sorted(slots)


def has_auth() -> bool:
    return bool(list_profile_slots())


async def _is_signed_in(ctx) -> bool:
    """True once the context holds a Google auth cookie (SAPISID/SID on .google.com)."""
    try:
        cookies = await ctx.cookies()
    except Exception:
        return False
    for c in cookies:
        if c.get("name") in ("SAPISID", "SID", "__Secure-1PSID") and "google.com" in c.get("domain", ""):
            return True
    return False


async def save_auth(slot: int = 0, timeout_s: int = 300) -> None:
    """Open real Google Chrome, let the user sign in, persist to the profile dir.

    Completes when a Google auth cookie appears, the user closes the window,
    or the timeout elapses — never hangs forever.
    """
    from playwright.async_api import async_playwright

    pdir = profile_dir(slot)
    pdir.mkdir(parents=True, exist_ok=True)
    clear_profile_locks(slot)

    print("\n" + "=" * 62)
    print(f"  ATOM — Google Sign-In  (slot {slot})")
    print("=" * 62)
    print("  Real Google Chrome will open. Sign into the Atom account.")
    print("  Closes automatically once you're signed in.")
    print("=" * 62 + "\n")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(pdir),
            headless=False,
            args=CHROME_ARGS,
            no_viewport=True,
            **_launch_kwargs(),        # real Chrome if BROWSER_CHANNEL=chrome, else Chromium
        )

        # Track if the user closes the window manually
        closed = {"v": False}
        ctx.on("close", lambda: closed.update(v=True))

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("close", lambda: closed.update(v=True))
        try:
            await page.goto("https://accounts.google.com/")
        except Exception:
            pass

        print("  Waiting for sign-in...")
        deadline = asyncio.get_event_loop().time() + timeout_s
        signed = False
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            if closed["v"]:
                print("  Window closed by user.")
                break
            if await _is_signed_in(ctx):
                print("  Signed in - auth cookie detected.")
                await asyncio.sleep(2)   # let cookies flush to disk
                signed = True
                break

        if signed or _profile_has_login(pdir):
            state_path = storage_state_path(slot)
            try:
                await ctx.storage_state(path=str(state_path))
            except Exception as exc:
                logger.warning("Could not save portable Google storage state: %s", exc)

        if not closed["v"]:
            try:
                await ctx.close()
            except Exception:
                pass

        # Final truth: did the profile actually capture a login?
        if signed or _profile_has_login(pdir):
            print(f"\nProfile saved -> {pdir}")
            print(f"Portable state saved -> {storage_state_path(slot)}")
        else:
            raise RuntimeError("Sign-in not completed (no auth cookie captured)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Authenticate an Atom Chrome profile")
    parser.add_argument("--slot", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(save_auth(args.slot))
