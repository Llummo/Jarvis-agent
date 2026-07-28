"""Drive the real UI in a real browser, reusing the harness's own Chromium.

The existing cdp_screenshot module already knows how to find Chromium, launch
it headless and attach over raw CDP — but it only navigates and screenshots.
These tests need to click things and read the DOM back, so this adds
Runtime.evaluate on top of the same plumbing rather than pulling in a second
browser stack.

Pairs with StubAPIServer: the UI is served with canned API responses so the
tests are fast, offline and deterministic. The API layer itself is covered by
the unit/integration suite; what is untestable there — that the markup, the
CSS and app.js actually work together in a browser — is what this covers.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from meta_harness.mcp_server.cdp_screenshot import (
    _find_chromium,
    _open_new_tab,
    _wait_for_devtools_port,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "meta_harness" / "webapp" / "static"


# ---------------------------------------------------------------------------
# A stub API so the UI boots fully without ClickUp, Linear or Claude
# ---------------------------------------------------------------------------

CLICKUP_TEAMS = [{"id": "T1", "name": "Team"}]
CLICKUP_SPACES = [
    {"id": "S1", "name": "gru-po", "statuses": [{"status": "open"}, {"status": "done"}]},
    {"id": "S2", "name": "otro-space", "statuses": [{"status": "todo"}]},
]
CLICKUP_LISTS = [{"id": "L1", "name": "Sprint backlog"}]
CLICKUP_TASKS = [
    {"id": "task-1", "name": "TAB-01 | Endpoint de personas", "status": {"status": "open"}},
    {"id": "task-2", "name": "TAF-02 | Formulario de alta", "status": {"status": "done"}},
]
LINEAR_TEAMS = [{"id": "LT1", "name": "Sigo", "key": "SIG"}]
LINEAR_STATES = [{"id": "st1", "name": "Todo", "type": "unstarted"}, {"id": "st2", "name": "Done", "type": "completed"}]
LINEAR_ISSUES = [
    {"id": "i1", "identifier": "SIG-1", "title": "Control de permisos", "state": {"id": "st1", "name": "Todo"}},
    {"id": "i2", "identifier": "SIG-2", "title": "Vista de personas", "state": {"id": "st2", "name": "Done"}},
]
FINDINGS = {
    "findings": [
        {
            "id": 1, "project": "gru-po", "route": "/login", "observation": "Algo falla",
            "severity": "major", "status": "open", "screenshot_path": None, "checked_route": None,
            "status_code": None, "http_error": None, "correction_note": None,
            "clickup_task_id": "task-1", "linear_issue_id": None,
            "created_at": "2026-07-28T00:00:00Z", "updated_at": "2026-07-28T00:00:00Z",
        }
    ],
    "counts": {"total": 1, "open": 1, "acknowledged": 0, "closed": 0, "critical": 0, "major": 1, "minor": 0},
}
REVERTIBLE = [
    {"ticket_id": "task-1", "tracker": "clickup", "title": "Titulo viejo",
     "new_title": "TAB-01 | Endpoint de personas", "saved_at": "2026-07-28T10:00:00Z"}
]


def _ticket(title, category="backend", parent=""):
    """A ProposedTicketOut-shaped ticket. Kept faithful to the real schema so
    the UI is exercised against the payload it will really receive."""
    return {
        "title": title,
        "description": "Registro transversal de personas.",
        "user_story": "Como reclutador,\nquiero registrar personas,\npara centralizar el pool.",
        "epic": "GESTION DE POOL DE TALENTO",
        "ui_route": "/erp/talent/people",
        "backend_endpoint": "POST /api/v1/talent/people",
        "technical_notes": "Permisos:\n```\ntalent:read   Lectura\n```",
        "parent_title": parent,
        "visual_resources": [],
        "acceptance_criteria": [
            {"name": "Alta", "given": "el formulario es valido", "when": "se confirme",
             "then": "la persona queda registrada", "text": ""}
        ],
        "priority": "high",
        "category": category,
        "sprint": 1,
        "due_date": "2026-08-24",
        "assignee_clickup_id": 1,
        "assignee_linear_id": None,
        "assignee_email": "ana@x.com",
        "assignee_name": "Ana",
    }


GENERATED = {
    "tickets": [_ticket("Endpoint de personas"), _ticket("Formulario de alta", "frontend", "Endpoint de personas")],
    "warnings": [],
}

FINDING = dict(FINDINGS["findings"][0])

REVIEW_RESULT = {
    "run_id": "run-1",
    "review": {
        "ticket_id": "task-1", "ticket_name": "TAB-01 | Endpoint de personas",
        "observation": "Verificar validacion de campos obligatorios.", "severity": "major",
        "route": "/erp/talent/people", "status_code": 200, "http_error": None, "screenshot_path": None,
    },
    "finding": None,
    "report_markdown": "# QA Review\n",
}

MODULE_RESULT = {
    "ticket_id": "task-1", "ticket_name": "TAB-01 | Endpoint de personas",
    "module_name": "Personas", "verdict": "related", "confidence": 0.88,
    "rationale": "Modifica la ficha de personas descrita en la documentacion.",
    "matched_aspects": ["Ficha de persona"], "module_gaps": [],
}

REFORMATTED = {
    "ticket_id": "task-1", "tracker": "clickup",
    "original_title": "endpoint personas", "original_description": "hay que hacer el endpoint",
    "ticket": _ticket("TAB-01 | Endpoint de personas"),
    "formatted_title": "TAB-01 | Endpoint de personas",
    "formatted_description": "USER STORY: GESTION DE POOL DE TALENTO\nTitulo: TAB-01 | Endpoint de personas",
}


def _post_route(path: str):
    """Canned response for a POST path, or None if it isn't stubbed."""
    base = path.split("?", 1)[0]
    if base.startswith("/api/qa/findings/") and base.endswith("/close"):
        return {**FINDING, "status": "closed", "correction_note": "corregido"}
    table = {
        "/api/tickets/generate": GENERATED,
        "/api/tickets/from-idea": GENERATED,
        "/api/tickets/create": {
            "results": [
                {"ticket": t, "ok": True, "clickup_task_id": f"new-{i}", "error": None}
                for i, t in enumerate(GENERATED["tickets"], start=1)
            ]
        },
        "/api/linear/issues": {
            "results": [
                {"ticket": t, "ok": True, "linear_issue_id": f"new-{i}", "error": None}
                for i, t in enumerate(GENERATED["tickets"], start=1)
            ]
        },
        "/api/qa/reviews": REVIEW_RESULT,
        "/api/qa/reviews/commit": FINDING,
        "/api/qa/reviews/bulk": {
            "results": [{"ticket_id": "task-1", "review": REVIEW_RESULT["review"], "error": None}],
            "readme_markdown": "# QA-README\n",
        },
        "/api/qa/reviews/bulk/commit": {"results": [{"ticket_id": "task-1", "finding": FINDING, "error": None}]},
        "/api/qa/findings": FINDING,
        "/api/qa/project-config": {"projects": {"gru-po": "https://app.example.com"}},
        "/api/tickets/module-relevance": MODULE_RESULT,
        "/api/tickets/module-relevance/bulk": {
            "module_name": "Personas",
            "summary": {"analyzed": 2, "related": 1, "partially_related": 0, "unrelated": 1, "failed": 0},
            "aligned": [MODULE_RESULT],
            "results": [
                {"ticket_id": "task-1", "relevance": MODULE_RESULT, "error": None},
                {"ticket_id": "task-2", "relevance": {**MODULE_RESULT, "ticket_id": "task-2",
                 "ticket_name": "TAF-02 | Formulario de alta", "verdict": "unrelated",
                 "confidence": 0.91, "matched_aspects": []}, "error": None},
            ],
            "report_markdown": "# Modulo: Personas\n",
        },
        "/api/tickets/reformat": REFORMATTED,
        "/api/tickets/reformat/apply": {"ok": True, "ticket_id": "task-1", "title": "TAB-01 | Endpoint de personas"},
        "/api/tickets/reformat/revert": {"ok": True, "ticket_id": "task-1", "title": "Titulo viejo"},
        "/api/tickets/verify-team": {
            "verified": [{"email": "ana@x.com", "username": "Ana", "clickup_id": 1, "linear_id": None}],
            "not_found": [],
        },
    }
    return table.get(base)


def _route(path: str) -> Optional[Any]:
    """Canned response for an API path, or None if it isn't stubbed."""
    base = path.split("?", 1)[0]
    table = {
        "/api/clickup/teams": CLICKUP_TEAMS,
        "/api/clickup/spaces": CLICKUP_SPACES,
        "/api/clickup/folders": [],
        "/api/clickup/lists": CLICKUP_LISTS,
        "/api/clickup/tasks": CLICKUP_TASKS,
        "/api/linear/teams": LINEAR_TEAMS,
        "/api/linear/states": LINEAR_STATES,
        "/api/linear/projects": [],
        "/api/linear/issues": LINEAR_ISSUES,
        "/api/qa/findings": FINDINGS,
        "/api/qa/project-config": {"projects": {}},
        "/api/qa/reviews": [],
        "/api/tickets/team-members": [
            {"email": "ana@x.com", "username": "Ana", "clickup_id": 1, "linear_id": None}
        ],
        "/api/tickets/reformat/revertible": REVERTIBLE,
    }
    return table.get(base)


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output readable
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        # Escape hatch so the error/retry path is still reachable now that the
        # happy path is stubbed.
        if b"FORCE_ERROR" in body:
            self.send_error(502, "forced failure")
            return
        payload = _post_route(self.path)
        if payload is None:
            self.send_error(404)
            return
        self._json(payload)

    def do_GET(self):
        # Progress polling: any token returns a finished-looking log.
        if self.path.startswith("/api/progress/"):
            return self._json({"steps": ["Working…", "Done."]})
        payload = _route(self.path)
        if payload is None:
            return super().do_GET()
        self._json(payload)


class StubAPIServer:
    """Serves the real static UI plus canned API responses."""

    def __init__(self):
        self._httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_Handler, directory=str(STATIC_DIR))
        )
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/index.html"

    def __enter__(self) -> "StubAPIServer":
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------


class BrowserError(RuntimeError):
    pass


class Browser:
    """Minimal CDP client: navigate, evaluate JS, wait for a condition."""

    def __init__(self, *, timeout_s: float = 25.0):
        self._timeout = timeout_s
        self._profile = Path(tempfile.mkdtemp(prefix="mh-e2e-"))
        self._proc: Optional[subprocess.Popen] = None
        self._loop = asyncio.new_event_loop()
        self._ws = None
        self._msg_id = 0

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Browser":
        chromium = _find_chromium()
        self._proc = subprocess.Popen(
            [
                str(chromium), "--headless=new", "--disable-gpu", "--no-sandbox",
                "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
                "--window-size=1280,900", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port = _wait_for_devtools_port(self._profile, self._proc, timeout_s=self._timeout)
        ws_url = _open_new_tab(port, self._timeout)
        self._loop.run_until_complete(self._connect(ws_url))
        return self

    def __exit__(self, *exc):
        try:
            if self._ws is not None:
                self._loop.run_until_complete(self._ws.close())
        finally:
            self._loop.close()
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            shutil.rmtree(self._profile, ignore_errors=True)

    async def _connect(self, ws_url: str):
        import websockets

        self._ws = await websockets.connect(ws_url, max_size=None)

    async def _send(self, method: str, params: Optional[dict] = None) -> dict:
        self._msg_id += 1
        msg_id = self._msg_id
        await self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self._timeout)
            message = json.loads(raw)
            if message.get("id") == msg_id:
                return message

    # -- api ---------------------------------------------------------------

    def goto(self, url: str) -> None:
        self._loop.run_until_complete(self._send("Page.enable"))
        self._loop.run_until_complete(self._send("Page.navigate", {"url": url}))
        # The app boots asynchronously; wait for it to have wired itself up.
        self.wait_for("document.readyState === 'complete' && !!document.getElementById('theme-toggle')")

    def eval(self, expression: str):
        """Evaluate JS and return its value. Raises on a page-side exception."""
        wrapped = f"(() => {{ {expression} }})()" if ";" in expression.strip() else expression
        response = self._loop.run_until_complete(
            self._send("Runtime.evaluate", {"expression": wrapped, "returnByValue": True, "awaitPromise": True})
        )
        result = response.get("result", {})
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise BrowserError(detail.get("exception", {}).get("description") or json.dumps(detail))
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, *, timeout_s: float = 12.0, message: str = "") -> None:
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            try:
                if self.eval(expression):
                    return
            except BrowserError as exc:
                last = str(exc)
            time.sleep(0.1)
        raise AssertionError(message or f"Timed out waiting for: {expression}" + (f" (last error: {last})" if last else ""))

    def click(self, selector: str) -> None:
        self.eval(f"document.querySelector({json.dumps(selector)}).click(); return true;")

    def type_into(self, selector: str, text: str) -> None:
        """Set a value and fire the events a real keystroke would."""
        self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            f"el.value = {json.dumps(text)};"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
            "el.dispatchEvent(new Event('change', {bubbles: true}));"
            "return true;"
        )

    def press_enter(self, selector: str, *, shift: bool = False) -> None:
        self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            "el.dispatchEvent(new KeyboardEvent('keydown', "
            f"{{key: 'Enter', shiftKey: {str(shift).lower()}, bubbles: true, cancelable: true}}));"
            "return true;"
        )

    def visible(self, selector: str) -> bool:
        return bool(
            self.eval(
                f"const el = document.querySelector({json.dumps(selector)});"
                "if (!el) return false;"
                "const s = getComputedStyle(el);"
                "return s.display !== 'none' && s.visibility !== 'hidden' && !el.hidden;"
            )
        )


def chromium_available() -> bool:
    try:
        _find_chromium()
    except Exception:
        return False
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True
