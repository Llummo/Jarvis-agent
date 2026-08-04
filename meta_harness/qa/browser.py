"""Drive a real browser, and record what it asked the server for.

The QA flow could already navigate and photograph a page. It could not click,
type, read the DOM, or see which endpoints the page actually called — so it
could report that a route answers, never that a feature works.

This adds the missing half. Two capabilities matter beyond the obvious:

**Network capture.** A ticket declares a route *and* an endpoint. Watching the
traffic the page generates is what turns «the form looks right» into «the form
called POST /api/v1/talent/people and got a 201». That is the difference
between checking a screenshot and checking behaviour, and it is why the CDP
event stream is collected instead of discarded.

**Finding by what a human sees.** `#form > div:nth-child(3) > button` breaks on
the first redesign. «the button that reads Guardar» survives it, so text and
accessible names are the primary way to locate things here.

Same CDP plumbing the screenshot tool already uses — no new browser stack.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from meta_harness.mcp_server.cdp_screenshot import (
    _find_chromium,
    _open_new_tab,
    _wait_for_devtools_port,
)

DEFAULT_TIMEOUT_S = 25.0
# How long to keep re-checking a condition before calling it false. Long enough
# for an app that boots asynchronously, short enough that a genuinely broken
# page fails the step rather than the whole run.
DEFAULT_WAIT_S = 12.0
POLL_INTERVAL_S = 0.15


class BrowserError(RuntimeError):
    """The browser could not do what was asked."""


@dataclass
class NetworkCall:
    """One request the page made, and how it went."""

    method: str
    url: str
    status: Optional[int] = None
    resource_type: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400

    @property
    def path(self) -> str:
        """The path alone, so an expectation can be written without the host —
        the same test has to run against local and the deployed QA site."""
        without_scheme = self.url.split("://", 1)[-1]
        slash = without_scheme.find("/")
        if slash == -1:
            return "/"
        return without_scheme[slash:].split("?", 1)[0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "path": self.path,
            "status": self.status,
            "resource_type": self.resource_type,
            "ok": self.ok,
        }


@dataclass
class Browser:
    """A headless Chromium under CDP control.

    Use as a context manager; the profile directory and the process are cleaned
    up even if a step raises.
    """

    timeout_s: float = DEFAULT_TIMEOUT_S
    _profile: Path = field(default=None, init=False, repr=False)
    _proc: Optional[subprocess.Popen] = field(default=None, init=False, repr=False)
    _loop: Any = field(default=None, init=False, repr=False)
    _ws: Any = field(default=None, init=False, repr=False)
    _msg_id: int = field(default=0, init=False, repr=False)
    _requests: Dict[str, NetworkCall] = field(default_factory=dict, init=False, repr=False)

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "Browser":
        self._profile = Path(tempfile.mkdtemp(prefix="mh-qa-"))
        self._loop = asyncio.new_event_loop()
        chromium = _find_chromium()
        self._proc = subprocess.Popen(
            [
                str(chromium), "--headless=new", "--disable-gpu", "--no-sandbox",
                "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
                "--window-size=1280,900", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port = _wait_for_devtools_port(self._profile, self._proc, timeout_s=self.timeout_s)
        ws_url = _open_new_tab(port, self.timeout_s)
        self._loop.run_until_complete(self._connect(ws_url))
        self._call("Page.enable")
        self._call("Network.enable")
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ws is not None:
                self._loop.run_until_complete(self._ws.close())
        finally:
            if self._loop is not None:
                self._loop.close()
            if self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            if self._profile is not None:
                shutil.rmtree(self._profile, ignore_errors=True)

    async def _connect(self, ws_url: str) -> None:
        import websockets

        self._ws = await websockets.connect(ws_url, max_size=None)

    async def _send(self, method: str, params: Optional[dict] = None) -> dict:
        """Send one command and wait for its reply, keeping the events.

        The reply and the event stream share one socket. Anything arriving
        without an `id` is an event; dropping those would throw away exactly
        the network traffic this class exists to record.
        """
        self._msg_id += 1
        msg_id = self._msg_id
        await self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self.timeout_s)
            message = json.loads(raw)
            if message.get("id") == msg_id:
                return message
            if "method" in message:
                self._absorb_event(message)

    def _absorb_event(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        request_id = params.get("requestId")
        if not request_id:
            return

        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            self._requests[request_id] = NetworkCall(
                method=str(request.get("method") or ""),
                url=str(request.get("url") or ""),
                resource_type=str(params.get("type") or ""),
            )
        elif method == "Network.responseReceived":
            call = self._requests.get(request_id)
            if call is not None:
                response = params.get("response") or {}
                call.status = response.get("status")
                call.resource_type = str(params.get("type") or call.resource_type)

    def _call(self, method: str, params: Optional[dict] = None) -> dict:
        return self._loop.run_until_complete(self._send(method, params))

    # -- navigation and inspection ---------------------------------------

    def goto(self, url: str) -> None:
        self._call("Page.navigate", {"url": url})
        # Waiting for the document to be interactive rather than sleeping: an
        # app that boots asynchronously is the normal case, and a fixed sleep
        # is the usual source of a test that fails one time in ten.
        self.wait_for("document.readyState === 'complete'", message=f"{url} never finished loading")

    def eval(self, expression: str) -> Any:
        """Evaluate JS in the page and return the result."""
        payload = self._call(
            "Runtime.evaluate",
            {"expression": f"(() => {{ {expression} }})()", "returnByValue": True, "awaitPromise": True},
        )
        result = (payload.get("result") or {}).get("result") or {}
        if result.get("subtype") == "error":
            raise BrowserError(str(result.get("description") or "JS error")[:300])
        return result.get("value")

    def wait_for(self, expression: str, *, timeout_s: float = DEFAULT_WAIT_S, message: str = "") -> None:
        """Poll a JS condition until it is true, or give up."""
        deadline = time.monotonic() + timeout_s
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if self.eval(f"return Boolean({expression});"):
                    return
            except BrowserError as exc:
                last_error = str(exc)
            time.sleep(POLL_INTERVAL_S)
        detail = message or f"condition never became true: {expression}"
        raise BrowserError(f"{detail}{f' ({last_error})' if last_error else ''}")

    # -- locating things the way a person would --------------------------

    def find_by_text(self, text: str, *, role: str = "") -> Optional[str]:
        """A CSS selector for the visible element whose text matches, or None.

        Prefers buttons, links and inputs — the things a person clicks — and
        falls back to any element. Returned as a selector so the caller can
        act on it with the ordinary methods.
        """
        script = """
        const wanted = %s.trim().toLowerCase();
        const role = %s;
        const tags = role ? [role] : ['button','a','input','select','textarea','label','[role=button]'];
        const pools = tags.map(t => [...document.querySelectorAll(t)]).flat();
        const all = pools.length ? pools : [...document.querySelectorAll('*')];
        for (const el of all) {
          const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
          if (!label || !label.includes(wanted)) continue;
          const box = el.getBoundingClientRect();
          if (box.width === 0 || box.height === 0) continue;
          if (el.id) return '#' + CSS.escape(el.id);
          el.setAttribute('data-mh-found', '1');
          return '[data-mh-found="1"]';
        }
        return null;
        """ % (json.dumps(text), json.dumps(role))
        return self.eval(script)

    def visible(self, selector: str) -> bool:
        return bool(
            self.eval(
                f"const el = document.querySelector({json.dumps(selector)});"
                " if (!el) return false;"
                " const b = el.getBoundingClientRect();"
                " return !el.hidden && b.width > 0 && b.height > 0;"
            )
        )

    def text_of(self, selector: str = "body") -> str:
        return self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            " return el ? (el.innerText || '') : '';"
        ) or ""

    def page_contains(self, text: str) -> bool:
        return text.strip().lower() in self.text_of().lower()

    # -- acting ----------------------------------------------------------

    def click(self, selector: str) -> None:
        clicked = self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return false; el.click(); return true;"
        )
        if not clicked:
            raise BrowserError(f"nothing to click at {selector!r}")

    def type_into(self, selector: str, text: str) -> None:
        typed = self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return false;"
            " el.focus();"
            f" el.value = {json.dumps(text)};"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return true;"
        )
        if not typed:
            raise BrowserError(f"no field to type into at {selector!r}")

    def screenshot(self, path: Path) -> Path:
        payload = self._call("Page.captureScreenshot", {"format": "png"})
        data = (payload.get("result") or {}).get("data")
        if not data:
            raise BrowserError("the browser returned no screenshot data")
        import base64

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path

    # -- what the page asked the server for ------------------------------

    def network_calls(self) -> List[NetworkCall]:
        """Every request the page made since the last reset, in order."""
        return list(self._requests.values())

    def api_calls(self) -> List[NetworkCall]:
        """Only the calls that look like an API, not assets.

        A page loads dozens of images, fonts and scripts. None of that is what
        a ticket's endpoint expectation is about.
        """
        return [
            call
            for call in self._requests.values()
            if call.resource_type.lower() in ("xhr", "fetch") or "/api/" in call.path
        ]

    def reset_network(self) -> None:
        """Forget traffic so far — called between steps so each one is judged
        on what it caused, not on what the page did while loading."""
        self._requests.clear()
