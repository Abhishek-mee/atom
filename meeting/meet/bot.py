"""
Google Meet bot — joins via headless Chromium (Playwright) and RECORDS the meeting.

Pure audio + video capture → MP4. No transcription, no LLM, no API keys.
 - Video: Playwright records the rendered Meet page (participant tiles).
 - Audio: incoming WebRTC audio is captured in-page via MediaRecorder and
          streamed to the server, which appends it to one webm file.
 - On leave: ffmpeg muxes video + audio into a single MP4.

Auto-leaves when alone for ALONE_TIMEOUT seconds.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import Page, async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from meeting.meet.auth import (
    profile_dir,
    storage_state_path,
    list_profile_slots,
    CHROME_ARGS,
    browser_channel,
)

# ── Config ──────────────────────────────────────────────────────────────────
ALONE_TIMEOUT  = 30          # seconds alone before leaving
MAX_DURATION   = 7200        # 2 hour hard cap
JOIN_WAIT      = 60          # seconds to wait for admission
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "api/static/recordings"))
DEBUG_DIR      = Path(os.getenv("DEBUG_DIR", "api/static/debug"))
DEBUG_SCREENSHOTS = os.getenv("DEBUG_SCREENSHOTS", "false").lower() in ("1", "true", "yes")


def record_enabled() -> bool:
    return os.getenv("RECORD_MEETING", "true").lower() in ("1", "true", "yes")


def guest_fallback_enabled() -> bool:
    return os.getenv("BOT_GUEST_FALLBACK", "true").lower() in ("1", "true", "yes")


def audio_file_for(session_id: str) -> Path:
    return RECORDINGS_DIR / f"{session_id}_audio.webm"


# ── Auth pool ───────────────────────────────────────────────────────────────
import itertools as _itertools
_slot_cycle = None

def _pick_auth_slot() -> int | None:
    """Pick next available signed-in Google auth slot, round-robin. None if none."""
    global _slot_cycle
    slots = list_profile_slots()
    if not slots:
        return None
    _slot_cycle = _itertools.cycle(slots)
    return next(_slot_cycle)


# ── Callbacks ───────────────────────────────────────────────────────────────
StatusCB = Callable[[str], Awaitable[None]]
CountCB  = Callable[[int], Awaitable[None]]


@dataclass
class CaptionSegment:
    """Kept for API compatibility (unused in recording mode)."""
    speaker: str
    text: str
    timestamp: float


# ── In-page audio capture (injected before Meet loads) ───────────────────────
# Intercepts RTCPeerConnection, mixes ALL incoming audio tracks into one stream
# via Web Audio, records 3s timeslices, and hands each chunk to Python through
# the exposed function window.__atomAudio(base64) — no network, no CORS.
_AUDIO_CAPTURE_JS = """
(function() {
  const CHUNK_MS = 3000;
  const _OrigPC  = window.RTCPeerConnection;
  if (!_OrigPC || window.__atomAudioHooked) return;
  window.__atomAudioHooked = true;

  let ac, dest, rec, started = false;

  const toB64 = (buf) => {
    const bytes = new Uint8Array(buf);
    let bin = '';
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    return btoa(bin);
  };

  const ensureRecorder = () => {
    if (started) return;
    started = true;
    rec = new MediaRecorder(dest.stream, { mimeType: 'audio/webm;codecs=opus' });
    rec.ondataavailable = async (e) => {
      if (!e.data || e.data.size < 100) return;
      try {
        const buf = await e.data.arrayBuffer();
        if (window.__atomAudio) await window.__atomAudio(toB64(buf));
      } catch (_) {}
    };
    rec.start(CHUNK_MS);
    console.log('[ATOM] mixed audio recorder started');
  };

  const addTrack = (track) => {
    try {
      if (!ac)   { ac = new (window.AudioContext || window.webkitAudioContext)(); }
      if (ac.state === 'suspended') ac.resume();
      if (!dest) { dest = ac.createMediaStreamDestination(); }
      const src = ac.createMediaStreamSource(new MediaStream([track]));
      src.connect(dest);
      ensureRecorder();
      console.log('[ATOM] mixed in audio track', track.id);
    } catch (err) {
      console.warn('[ATOM] addTrack failed:', err);
    }
  };

  window.RTCPeerConnection = function(...args) {
    const pc = new _OrigPC(...args);
    pc.addEventListener('track', (evt) => {
      if (evt.track && evt.track.kind === 'audio') addTrack(evt.track);
    });
    return pc;
  };
  Object.assign(window.RTCPeerConnection, _OrigPC);
  window.RTCPeerConnection.prototype = _OrigPC.prototype;
})();
"""

# Counts participants for alone-detection.
_PARTICIPANT_JS = """
window.__atomPaxCount = 1;
const _count = () => {
    const tiles = document.querySelectorAll(
        '[data-participant-id], [data-requested-participant-id]'
    );
    if (tiles.length > 0) { window.__atomPaxCount = tiles.length; return; }
    const vids = [...document.querySelectorAll('video')].filter(v => !v.paused);
    window.__atomPaxCount = Math.max(vids.length, 1);
};
setInterval(_count, 3000);
_count();
"""


class MeetBot:
    def __init__(
        self,
        meeting_url: str,
        bot_name: str,
        on_status: StatusCB,
        on_count: CountCB,
    ) -> None:
        self.url        = meeting_url
        self.name       = bot_name
        self.on_status  = on_status
        self.on_count   = on_count
        self._running   = False
        self._page: Page | None = None
        self.session_id: str = uuid.uuid4().hex
        self.recording_path: str | None = None
        self._audio_path: Path | None = None
        self._audio_chunks: int = 0
        self._started_at: float | None = None
        self._context_opened_at: float | None = None
        self._meeting_started_at: float | None = None
        self._admitted: bool = False
        self._guest_mode: bool = False

    async def _status(self, message: str) -> None:
        if self._started_at is None:
            await self.on_status(message)
            return
        elapsed = max(0, int(time.time() - self._started_at))
        await self.on_status(f"[{elapsed:02d}s] {message}")

    def _reset_capture_state(self) -> None:
        if self._audio_path:
            try:
                self._audio_path.unlink(missing_ok=True)
            except Exception:
                pass
        self._audio_path = None
        self._audio_chunks = 0
        self._context_opened_at = None

    async def _prepare_audio_capture(self, ctx) -> None:
        # Audio bytes flow page -> Python via this exposed function.
        apath = audio_file_for(self.session_id)
        apath.parent.mkdir(parents=True, exist_ok=True)
        apath.unlink(missing_ok=True)
        self._audio_path = apath
        self._audio_chunks = 0

        import base64 as _b64

        async def _recv_audio(b64: str) -> None:
            try:
                with open(apath, "ab") as f:
                    f.write(_b64.b64decode(b64))
                self._audio_chunks += 1
            except Exception as e:
                logger.warning("audio write failed: %s", e)

        await ctx.expose_function("__atomAudio", _recv_audio)
        await ctx.add_init_script(_AUDIO_CAPTURE_JS)
        logger.info("Audio capture injected (session %s)", self.session_id)

    async def join(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Run: pip install playwright && playwright install chromium")

        self._started_at = time.time()
        slot = _pick_auth_slot()
        if slot is None and not guest_fallback_enabled():
            await self._status("⚠️ No Google session — click 'sign in as atom' first")
            raise RuntimeError("No signed-in Chrome profile. Run sign-in first.")
        if slot is None:
            await self._status("No bot Google profile found — trying guest join…")

        recording = record_enabled()

        async with async_playwright() as pw:
            await self._status("Launching Chrome…")
            self._context_opened_at = time.time()

            # Resolution is configurable — lower it (e.g. 854x480) to run on a
            # cheap 1GB host. Default 1280x720.
            rw = int(os.getenv("REC_WIDTH", "1280"))
            rh = int(os.getenv("REC_HEIGHT", "720"))

            launch_kwargs = dict(
                headless=True,
                args=CHROME_ARGS + [
                    # Auto-grant the permission prompt, but DON'T supply a fake
                    # camera/mic — Atom sends nothing, so no synthetic video
                    # pattern or beep appears on its tile.
                    "--use-fake-ui-for-media-stream",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                    # Memory/CPU savers so a 1GB box survives a long meeting
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-translate",
                    "--mute-audio",
                    "--renderer-process-limit=2",
                    "--js-flags=--max-old-space-size=512",
                ],
            )
            ctx_kwargs = dict(
                permissions=["microphone", "camera"],
                viewport={"width": rw, "height": rh},
            )
            _ch = browser_channel()
            if _ch:
                launch_kwargs["channel"] = _ch   # real Chrome (macOS/x86); else bundled Chromium
            if recording:
                RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
                ctx_kwargs["record_video_dir"] = str(RECORDINGS_DIR)
                ctx_kwargs["record_video_size"] = {"width": rw, "height": rh}
                logger.info("Video recording enabled → %s (%dx%d)", RECORDINGS_DIR, rw, rh)

            async def open_context(use_guest: bool):
                browser_obj = None
                if use_guest:
                    browser_obj = await pw.chromium.launch(**launch_kwargs)
                    context = await browser_obj.new_context(**ctx_kwargs)
                    logger.info("Launched Chrome with guest context")
                    return browser_obj, context

                pdir = profile_dir(slot)
                state_path = storage_state_path(slot)
                use_storage_state = state_path.exists()
                if use_storage_state:
                    browser_obj = await pw.chromium.launch(**launch_kwargs)
                    context = await browser_obj.new_context(
                        storage_state=str(state_path),
                        **ctx_kwargs,
                    )
                    logger.info("Launched Chrome with storage state %s", state_path)
                    return browser_obj, context

                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(pdir),
                    **launch_kwargs,
                    **ctx_kwargs,
                )
                logger.info("Launched Chrome with profile %s", pdir)
                return None, context

            browser, ctx = await open_context(use_guest=slot is None)
            self._guest_mode = slot is None

            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            if recording:
                await self._prepare_audio_capture(ctx)

            self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            # Surface page console logs (look for [ATOM] audio messages)
            self._page.on("console", lambda m: (
                logger.info("PAGE: %s", m.text) if "[ATOM]" in m.text else None
            ))
            self._running = True

            await self._status("Opening meeting…")
            await self._page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2.5)
            await self._screenshot("01_loaded")
            if await self._is_private_google_screen():
                if not guest_fallback_enabled() or self._guest_mode:
                    raise RuntimeError(
                        "Atom could not open this Meet. The meeting may require a signed-in Google account or host admission."
                    )
                await self._status("Bot profile is signed out — retrying as guest…")
                failed_video = self._page.video if recording else None
                await ctx.close()
                if browser is not None:
                    await browser.close()
                if failed_video is not None:
                    try:
                        Path(await failed_video.path()).unlink(missing_ok=True)
                    except Exception as exc:
                        logger.warning("Could not remove failed preflight video: %s", exc)
                self._reset_capture_state()
                browser, ctx = await open_context(use_guest=True)
                self._guest_mode = True
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                if recording:
                    await self._prepare_audio_capture(ctx)
                self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                self._page.on("console", lambda m: (
                    logger.info("PAGE: %s", m.text) if "[ATOM]" in m.text else None
                ))
                self._context_opened_at = time.time()
                await self._page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2.5)
                await self._screenshot("01_loaded_guest")

            await self._dismiss_popups()
            await self._set_name()
            await self._dismiss_popups()
            await self._mute_av()
            await asyncio.sleep(0.5)
            await self._click_join()
            await asyncio.sleep(2)
            await self._screenshot("02_after_join_click")

            await self._status("Waiting to be admitted…")
            await self._wait_for_admission()
            self._admitted = True
            self._meeting_started_at = time.time()
            await asyncio.sleep(1.5)
            await self._screenshot("03_in_meeting")

            await self._change_display_name()
            await self._ensure_av_off()   # make sure cam + mic are off in-meeting

            try:
                await self._page.evaluate(_PARTICIPANT_JS)
            except Exception as e:
                logger.warning("Participant JS failed: %s", e)

            await self._status("Recording…")

            start = time.time()
            alone_t: float | None = None

            while self._running:
                count = await self._pax_count()
                await self.on_count(count)

                if count <= 1:
                    if alone_t is None:
                        alone_t = time.time()
                    elif time.time() - alone_t >= ALONE_TIMEOUT:
                        await self._status("Everyone left — wrapping up…")
                        break
                else:
                    alone_t = None

                if time.time() - start > MAX_DURATION:
                    await self._status("Max duration reached — leaving…")
                    break

                await asyncio.sleep(5)

            await self._leave()

            video_obj = self._page.video if recording else None
            if recording:
                await self._status("Saving recording…")
                await asyncio.sleep(2)   # let MediaRecorder flush final chunk
                logger.info("Audio chunks received: %d", self._audio_chunks)

            await ctx.close()        # finalizes the webm video file
            if browser is not None:
                await browser.close()

            if video_obj is not None:
                try:
                    webm_video = await video_obj.path()
                    self.recording_path = await self._finalize_recording(Path(webm_video))
                except Exception as e:
                    logger.warning("Recording finalize failed: %s", e)

    # ── Recording mux ─────────────────────────────────────────────────────────
    @staticmethod
    def _ffprobe_dur(path: Path) -> float:
        try:
            import subprocess
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=20,
            )
            return float(out.stdout.strip())
        except Exception:
            return 0.0

    async def _finalize_recording(self, video_webm: Path) -> str | None:
        if not video_webm.exists():
            logger.warning("Video file not found: %s", video_webm)
            return None

        # Guard: if the video is basically empty, the bot never really recorded
        # (e.g. never admitted to the meeting). Don't save a broken file.
        vdur_check = self._ffprobe_dur(video_webm)
        if not self._admitted or not self._meeting_started_at:
            logger.warning("Bot was not admitted to the meeting — discarding video")
            video_webm.unlink(missing_ok=True)
            return None
        if video_webm.stat().st_size < 50_000 or vdur_check < 2.0:
            logger.warning("Empty recording (%.1fs, %d bytes) — discarding",
                           vdur_check, video_webm.stat().st_size)
            video_webm.unlink(missing_ok=True)
            return None

        audio_webm = audio_file_for(self.session_id)
        has_audio  = audio_webm.exists() and audio_webm.stat().st_size > 2_000

        # Output format: 'webm' = stream-copy mux (near-zero CPU, no transcode);
        # 'mp4' = re-encode to H.264/AAC (universal, heavier). Default mp4.
        fmt = os.getenv("REC_FORMAT", "mp4").lower()
        if fmt not in ("mp4", "webm"):
            fmt = "mp4"
        out = RECORDINGS_DIR / f"meeting_{self.session_id}.{fmt}"

        logger.info("Muxing (%s) — video=%d bytes, audio=%s", fmt,
                    video_webm.stat().st_size,
                    f"{audio_webm.stat().st_size} bytes" if has_audio else "none")

        # Privacy guard: Playwright video starts when the browser context opens,
        # so trim every pre-admission frame before muxing the deliverable.
        head_trim = max(0.0, (self._meeting_started_at or 0) - (self._context_opened_at or 0))
        if has_audio:
            vdur, adur = self._ffprobe_dur(video_webm), self._ffprobe_dur(audio_webm)
            head_trim = max(head_trim, max(0.0, vdur - adur))
            logger.info("Sync: video=%.1fs audio=%.1fs → trim head %.1fs", vdur, adur, head_trim)

        cmd = ["ffmpeg", "-y"]
        if head_trim > 0.3:
            cmd += ["-ss", f"{head_trim:.2f}"]
        cmd += ["-i", str(video_webm)]
        if has_audio:
            cmd += ["-i", str(audio_webm)]

        if fmt == "webm":
            # No re-encode — copy VP8 video + Opus audio straight into webm.
            cmd += ["-c", "copy"]
        else:
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast"]
            if has_audio:
                cmd += ["-c:a", "aac", "-b:a", "128k"]

        if has_audio:
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        cmd += [str(out)]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("ffmpeg failed: %s", stderr.decode()[-500:])
            return None

        try:
            video_webm.unlink(missing_ok=True)
            if has_audio:
                audio_webm.unlink(missing_ok=True)
        except Exception:
            pass

        logger.info("Recording saved: %s (%d bytes)", out, out.stat().st_size)
        return f"/recordings/{out.name}"

    # ── Join helpers ────────────────────────────────────────────────────────────
    async def _dismiss_popups(self) -> None:
        for text in ["Got it", "Continue without signing in", "Use without an account", "Dismiss", "Skip"]:
            try:
                btn = self._page.get_by_role("button", name=re.compile(f"^{text}$", re.I))
                if await btn.is_visible(timeout=1_500):
                    await btn.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

    async def _set_name(self) -> None:
        for sel in ['input[placeholder="Your name"]', 'input[placeholder*="name" i]',
                    'input[aria-label*="name" i]', 'input[type="text"]']:
            try:
                el = await self._page.wait_for_selector(sel, timeout=4_000)
                if el:
                    await el.click()
                    await self._page.keyboard.press("Control+a")
                    await self._page.keyboard.press("Backspace")
                    await el.type(self.name, delay=80)
                    if await el.input_value():
                        return
            except Exception:
                continue

    async def _mute_av(self) -> None:
        for label in ["Turn off microphone", "Mute microphone", "Turn off camera", "Stop camera"]:
            try:
                btn = self._page.locator(f'button[aria-label*="{label}" i]').first
                if await btn.is_visible(timeout=1_200):
                    await btn.click()
            except Exception:
                pass

    async def _ensure_av_off(self) -> None:
        """Inside the meeting, click 'Turn off' only if cam/mic are still ON.
        A visible 'Turn off …' button means it's currently on → click to disable."""
        for label in ["Turn off camera", "Turn off microphone"]:
            try:
                btn = self._page.locator(f'button[aria-label*="{label}" i]').first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    logger.info("Disabled: %s", label)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

    async def _click_join(self) -> None:
        for attempt in range(8):
            for label in ["Join now", "Ask to join", "Join"]:
                try:
                    btn = self._page.get_by_role("button", name=re.compile(label, re.I))
                    if await btn.is_visible(timeout=2_000):
                        await btn.click()
                        logger.info("Clicked join: %s", label)
                        return
                except Exception:
                    pass
                try:
                    btn = self._page.locator(f'button:has-text("{label}")').first
                    if await btn.is_visible(timeout=1_000):
                        await btn.click()
                        logger.info("Clicked join (text): %s", label)
                        return
                except Exception:
                    pass
            await asyncio.sleep(1)
        logger.warning("Could not click join button")

    async def _wait_for_admission(self) -> None:
        deadline = time.time() + JOIN_WAIT
        while time.time() < deadline:
            try:
                for label in ["Share screen", "Turn on captions", "Send a reaction"]:
                    btn = self._page.locator(f'button[aria-label*="{label}" i]').first
                    if await btn.is_visible(timeout=700):
                        logger.info("Admitted — detected: %s", label)
                        return
            except Exception:
                pass
            if await self._is_private_google_screen():
                raise RuntimeError(
                    "Atom could not open this Meet. The meeting may require a signed-in Google account or host admission."
                )
            await asyncio.sleep(1.5)
        raise RuntimeError("Atom was not admitted to the meeting, so no recording was saved.")

    async def _is_private_google_screen(self) -> bool:
        try:
            url = self._page.url.lower()
            if "accounts.google." in url or "/signin/" in url:
                return True
            text = (await self._page.locator("body").inner_text(timeout=1_000)).lower()
            private_markers = [
                "choose an account",
                "use another account",
                "remove an account",
                "signed out",
            ]
            return any(marker in text for marker in private_markers)
        except Exception:
            return False

    async def _change_display_name(self) -> None:
        try:
            for label in ["More options", "More"]:
                try:
                    btn = self._page.locator(f'button[aria-label*="{label}" i]').first
                    if await btn.is_visible(timeout=2_000):
                        await btn.click(); await asyncio.sleep(0.8); break
                except Exception:
                    pass
            for text in ["Change name", "Edit name"]:
                try:
                    item = self._page.get_by_role("menuitem", name=re.compile(text, re.I))
                    if await item.is_visible(timeout=2_000):
                        await item.click(); await asyncio.sleep(0.5)
                        inp = await self._page.wait_for_selector('input[type="text"]', timeout=3_000)
                        if inp:
                            await inp.click()
                            await self._page.keyboard.press("Control+a")
                            await inp.type(self.name, delay=60)
                            await self._page.keyboard.press("Enter")
                            logger.info("Display name set to: %s", self.name)
                        return
                except Exception:
                    pass
        except Exception as e:
            logger.debug("change name: %s", e)
        finally:
            try:
                await self._page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass

    async def _pax_count(self) -> int:
        try:
            return int(await self._page.evaluate("(() => window.__atomPaxCount || 1)()"))
        except Exception:
            return 1

    async def _leave(self) -> None:
        for label in ["Leave call", "Leave", "End call"]:
            try:
                btn = self._page.locator(f'button[aria-label*="{label}" i]').first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    await asyncio.sleep(2)
                    logger.info("Left meeting")
                    return
            except Exception:
                continue

    async def _screenshot(self, name: str) -> None:
        if not DEBUG_SCREENSHOTS:
            return
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            await self._page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=False)
        except Exception:
            pass

    async def stop(self) -> None:
        self._running = False
