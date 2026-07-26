import asyncio
import base64
import json

import pytest

from meta_harness.mcp_server.cdp_screenshot import (
    ChromiumNotFoundError,
    ScreenshotCaptureError,
    _find_chromium,
    _run_navigation_flow,
    capture_screenshot,
)

FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepixels"
FAKE_PNG_B64 = base64.b64encode(FAKE_PNG_BYTES).decode("ascii")


# ---------------------------------------------------------------------------
# _find_chromium precedence
# ---------------------------------------------------------------------------


def test_find_chromium_prefers_env_var(tmp_path, monkeypatch):
    fake_binary = tmp_path / "chrome"
    fake_binary.write_text("#!/bin/sh")
    monkeypatch.setenv("META_HARNESS_CHROMIUM_PATH", str(fake_binary))

    assert _find_chromium() == fake_binary.resolve()


def test_find_chromium_falls_back_to_playwright_cache_glob(monkeypatch, tmp_path):
    monkeypatch.delenv("META_HARNESS_CHROMIUM_PATH", raising=False)
    cached = tmp_path / "chrome-linux64" / "chrome"
    cached.parent.mkdir(parents=True)
    cached.write_text("#!/bin/sh")

    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot.glob.glob",
        lambda pattern: [str(cached)] if "chrome-linux64" in pattern else [],
    )

    assert _find_chromium() == cached.resolve()


def test_find_chromium_falls_back_to_which(monkeypatch):
    monkeypatch.delenv("META_HARNESS_CHROMIUM_PATH", raising=False)
    monkeypatch.setattr("meta_harness.mcp_server.cdp_screenshot.glob.glob", lambda pattern: [])
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot.shutil.which",
        lambda name: "/usr/bin/chromium" if name == "chromium" else None,
    )

    from pathlib import Path

    assert _find_chromium() == Path("/usr/bin/chromium")


def test_find_chromium_raises_when_nothing_found(monkeypatch):
    monkeypatch.delenv("META_HARNESS_CHROMIUM_PATH", raising=False)
    monkeypatch.setattr("meta_harness.mcp_server.cdp_screenshot.glob.glob", lambda pattern: [])
    monkeypatch.setattr("meta_harness.mcp_server.cdp_screenshot.shutil.which", lambda name: None)

    with pytest.raises(ChromiumNotFoundError, match="No Chromium binary found"):
        _find_chromium()


# ---------------------------------------------------------------------------
# _run_navigation_flow — fake CDP websocket
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """A loopback fake: send() scripts what gets enqueued for the reader to see."""

    def __init__(self, script):
        self._script = script
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        for response in self._script(message):
            await self._queue.put(json.dumps(response))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()


class FakeConnect:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc_info):
        return False


def _install_fake_websocket(monkeypatch, script):
    ws = FakeWebSocket(script)
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot.websockets.connect",
        lambda url, max_size=None: FakeConnect(ws),
    )
    return ws


def _happy_path_script(message):
    method = message.get("method")
    msg_id = message.get("id")
    if method == "Page.enable":
        return [{"id": msg_id, "result": {}}]
    if method == "Page.navigate":
        return [
            {"id": msg_id, "result": {"frameId": "f1"}},
            {"method": "Page.loadEventFired", "params": {}},
        ]
    if method == "Page.captureScreenshot":
        return [{"id": msg_id, "result": {"data": FAKE_PNG_B64}}]
    return []


def test_run_navigation_flow_happy_path(monkeypatch):
    _install_fake_websocket(monkeypatch, _happy_path_script)

    data = asyncio.run(_run_navigation_flow("ws://fake", "http://example.test", timeout_s=2.0))

    assert base64.b64decode(data) == FAKE_PNG_BYTES


def _load_event_before_nav_ack_script(message):
    """Simulate Page.loadEventFired arriving before its Page.navigate ack —
    proves the reader task doesn't drop out-of-order messages."""
    method = message.get("method")
    msg_id = message.get("id")
    if method == "Page.enable":
        return [{"id": msg_id, "result": {}}]
    if method == "Page.navigate":
        return [
            {"method": "Page.loadEventFired", "params": {}},
            {"id": msg_id, "result": {"frameId": "f1"}},
        ]
    if method == "Page.captureScreenshot":
        return [{"id": msg_id, "result": {"data": FAKE_PNG_B64}}]
    return []


def test_run_navigation_flow_handles_out_of_order_load_event(monkeypatch):
    _install_fake_websocket(monkeypatch, _load_event_before_nav_ack_script)

    data = asyncio.run(_run_navigation_flow("ws://fake", "http://example.test", timeout_s=2.0))

    assert base64.b64decode(data) == FAKE_PNG_BYTES


def test_run_navigation_flow_raises_on_navigate_error(monkeypatch):
    def script(message):
        method = message.get("method")
        msg_id = message.get("id")
        if method == "Page.enable":
            return [{"id": msg_id, "result": {}}]
        if method == "Page.navigate":
            return [{"id": msg_id, "error": {"message": "net::ERR_NAME_NOT_RESOLVED"}}]
        return []

    _install_fake_websocket(monkeypatch, script)

    with pytest.raises(ScreenshotCaptureError, match="Page.navigate failed"):
        asyncio.run(_run_navigation_flow("ws://fake", "http://bad.test", timeout_s=2.0))


def test_run_navigation_flow_times_out_waiting_for_load(monkeypatch):
    def script(message):
        method = message.get("method")
        msg_id = message.get("id")
        if method == "Page.enable":
            return [{"id": msg_id, "result": {}}]
        if method == "Page.navigate":
            return [{"id": msg_id, "result": {}}]  # never fires Page.loadEventFired
        return []

    _install_fake_websocket(monkeypatch, script)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_run_navigation_flow("ws://fake", "http://slow.test", timeout_s=0.2))


# ---------------------------------------------------------------------------
# capture_screenshot — process lifecycle / cleanup
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def test_capture_screenshot_terminates_process_on_success(tmp_path, monkeypatch):
    fake_proc = FakeProcess()
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot._find_chromium", lambda: tmp_path / "chrome"
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot.subprocess.Popen", lambda *a, **k: fake_proc
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot._wait_for_devtools_port", lambda *a, **k: 9222
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot._open_new_tab", lambda *a, **k: "ws://fake"
    )

    async def fake_flow(ws_url, url, *, timeout_s):
        return FAKE_PNG_B64

    monkeypatch.setattr("meta_harness.mcp_server.cdp_screenshot._run_navigation_flow", fake_flow)

    output = tmp_path / "shot.png"
    result = capture_screenshot("http://example.test", output_path=output, timeout_s=2.0)

    assert result == output
    assert output.read_bytes() == FAKE_PNG_BYTES
    assert fake_proc.terminated is True


def test_capture_screenshot_terminates_process_on_failure(tmp_path, monkeypatch):
    fake_proc = FakeProcess()
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot._find_chromium", lambda: tmp_path / "chrome"
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot.subprocess.Popen", lambda *a, **k: fake_proc
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot._wait_for_devtools_port", lambda *a, **k: 9222
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.cdp_screenshot._open_new_tab", lambda *a, **k: "ws://fake"
    )

    async def failing_flow(ws_url, url, *, timeout_s):
        raise ScreenshotCaptureError("boom")

    monkeypatch.setattr("meta_harness.mcp_server.cdp_screenshot._run_navigation_flow", failing_flow)

    with pytest.raises(ScreenshotCaptureError, match="boom"):
        capture_screenshot("http://example.test", output_path=tmp_path / "shot.png", timeout_s=2.0)

    assert fake_proc.terminated is True


# ---------------------------------------------------------------------------
# One real integration test against the actual Playwright-cached Chromium
# ---------------------------------------------------------------------------

import glob as _glob  # noqa: E402
import os as _os  # noqa: E402

_KNOWN_CHROMIUM_MATCHES = _glob.glob(
    _os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")
)


@pytest.mark.skipif(not _KNOWN_CHROMIUM_MATCHES, reason="No local Chromium binary available to test against")
def test_capture_screenshot_real_chromium_writes_png(tmp_path):
    output = tmp_path / "real.png"

    result = capture_screenshot("data:text/html,<h1>hi</h1>", output_path=output, timeout_s=20.0)

    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0
    assert output.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
