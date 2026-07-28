"""Capture a screenshot of a URL by driving Chromium directly over the
Chrome DevTools Protocol (CDP) — not the Playwright automation library.

Only a plain Chromium *binary* is required; if one happens to have been
downloaded previously by Playwright's own browser-install step, it is
reused as-is, same as any other Chromium install.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import glob
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

import websockets

from meta_harness.mcp_server.images import screenshots_dir

CHROMIUM_PATH_ENV_VAR = "META_HARNESS_CHROMIUM_PATH"

_PLAYWRIGHT_CACHE_GLOBS = [
    "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
    "~/.cache/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "~/AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe",
]
_WHICH_NAMES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]


class ChromiumNotFoundError(RuntimeError):
    """No usable Chromium/Chrome binary could be located."""


class ScreenshotCaptureError(RuntimeError):
    """Raised when launching Chromium, connecting over CDP, or capturing fails."""


def _find_chromium() -> Path:
    """Locate a Chromium/Chrome binary: env var, Playwright cache, then PATH."""
    env_value = os.getenv(CHROMIUM_PATH_ENV_VAR)
    if env_value and Path(env_value).expanduser().exists():
        return Path(env_value).expanduser().resolve()

    for pattern in _PLAYWRIGHT_CACHE_GLOBS:
        matches = sorted(glob.glob(os.path.expanduser(pattern)))
        if matches:
            return Path(matches[-1]).resolve()

    for name in _WHICH_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)

    raise ChromiumNotFoundError(
        f"No Chromium binary found. Set {CHROMIUM_PATH_ENV_VAR}, or install Playwright's "
        "Chromium (`python -m playwright install chromium` — the binary is fine to reuse "
        "even though the Playwright library itself is not used to drive it), or install a "
        "system chromium/google-chrome."
    )


def default_screenshot_path() -> Path:
    directory = screenshots_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"url-{uuid.uuid4().hex[:8]}.png"


def _wait_for_devtools_port(profile_dir: Path, proc: subprocess.Popen, *, timeout_s: float) -> int:
    port_file = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ScreenshotCaptureError(f"Chromium exited early with code {proc.returncode}")
        if port_file.exists():
            content = port_file.read_text(encoding="utf-8").strip()
            if content:
                return int(content.splitlines()[0])
        time.sleep(0.1)
    raise ScreenshotCaptureError("Timed out waiting for Chromium's DevTools port to become available")


def _open_new_tab(port: int, timeout_s: float) -> str:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed capture error
        raise ScreenshotCaptureError(f"Could not open a new Chromium tab via CDP: {exc}") from exc

    ws_url = payload.get("webSocketDebuggerUrl")
    if not ws_url:
        raise ScreenshotCaptureError(f"CDP /json/new response missing webSocketDebuggerUrl: {payload}")
    return ws_url


# How long to let a client-side app paint after the document has loaded.
RENDER_SETTLE_TIMEOUT_S = 8.0
# Give the app a beat even once it has content, so half-drawn frames and
# late-arriving data don't get captured mid-render.
RENDER_SETTLE_GRACE_S = 0.6


async def _wait_for_content(send, *, timeout_s: float) -> None:
    """Wait until the page has actually rendered something visible.

    A single-page app serves an empty shell and fills it in from JavaScript,
    so "document loaded" is not "screen ready". Polls for real rendered text
    or a non-trivial element count, then pauses briefly to let the frame
    settle. Never raises: a genuinely blank page is still evidence worth
    capturing, so this degrades to the old behaviour instead of failing.
    """
    deadline = time.monotonic() + timeout_s
    expression = (
        "(() => { const b = document.body; if (!b) return false;"
        " const text = (b.innerText || '').trim();"
        " return text.length > 0 || b.querySelectorAll('img,svg,canvas,input,button').length > 0; })()"
    )
    while time.monotonic() < deadline:
        try:
            response = await send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
            if response.get("result", {}).get("result", {}).get("value"):
                await asyncio.sleep(RENDER_SETTLE_GRACE_S)
                return
        except Exception:  # noqa: BLE001 - a blank capture beats no capture
            return
        await asyncio.sleep(0.15)


async def _run_navigation_flow(ws_url: str, url: str, *, timeout_s: float) -> str:
    """Connect over CDP and return base64 PNG data for a full-viewport screenshot.

    Uses one persistent background reader task dispatching every incoming
    message (by id to pending command futures, or by method to the load
    event) so a Page.loadEventFired that arrives before its Page.navigate
    command ack is never dropped.
    """
    msg_id_counter = itertools.count(1)
    pending: dict = {}
    load_event = asyncio.Event()

    async with websockets.connect(ws_url, max_size=None) as ws:

        async def reader() -> None:
            async for raw in ws:
                message = json.loads(raw)
                if "id" in message:
                    future = pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif message.get("method") == "Page.loadEventFired":
                    load_event.set()

        reader_task = asyncio.create_task(reader())

        async def send(method: str, params: Optional[dict] = None) -> dict:
            msg_id = next(msg_id_counter)
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            pending[msg_id] = future
            await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            return await asyncio.wait_for(future, timeout=timeout_s)

        try:
            await send("Page.enable")

            nav_response = await send("Page.navigate", {"url": url})
            if "error" in nav_response:
                raise ScreenshotCaptureError(f"Page.navigate failed: {nav_response['error']}")

            await asyncio.wait_for(load_event.wait(), timeout=timeout_s)

            # Page.loadEventFired means the HTML document finished loading —
            # for a single-page app that is BEFORE the client-side router has
            # mounted anything, so capturing here yields a blank page or the
            # router's transient fallback ("Not Found") rather than the real
            # screen. Wait for the app to actually paint something.
            await _wait_for_content(send, timeout_s=min(timeout_s, RENDER_SETTLE_TIMEOUT_S))

            shot_response = await send("Page.captureScreenshot", {"format": "png"})
            if "error" in shot_response:
                raise ScreenshotCaptureError(f"Page.captureScreenshot failed: {shot_response['error']}")
            return shot_response["result"]["data"]
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task


async def _capture_screenshot_async(url: str, output_path: Path, *, timeout_s: float) -> Path:
    chromium_path = _find_chromium()
    profile_dir = Path(tempfile.mkdtemp(prefix="meta-harness-cdp-"))
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [
                str(chromium_path),
                "--headless=new",
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={profile_dir}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        port = await asyncio.to_thread(_wait_for_devtools_port, profile_dir, proc, timeout_s=timeout_s)
        ws_url = await asyncio.to_thread(_open_new_tab, port, timeout_s)
        data = await _run_navigation_flow(ws_url, url, timeout_s=timeout_s)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(data))
        return output_path
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)


def capture_screenshot(url: str, output_path: Optional[Path] = None, *, timeout_s: float = 20.0) -> Path:
    """Synchronous entry point — launches Chromium, drives it over raw CDP,
    and writes a PNG screenshot of `url` to `output_path` (or a default path
    under qa/screenshots/).

    Not safe to call from inside an already-running event loop (e.g.
    directly from an `async def` FastAPI route) — use
    `asyncio.to_thread(capture_screenshot, ...)` there instead.
    """
    resolved_output = output_path if output_path is not None else default_screenshot_path()
    return asyncio.run(_capture_screenshot_async(url, resolved_output, timeout_s=timeout_s))
