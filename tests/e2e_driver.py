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
from urllib.parse import unquote

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
REWORK_PARENTS = [
    {"issue_id": "p1", "identifier": "SIG-90", "title": "Módulo de selección", "state_name": "Todo"}
]

# One name resolved cleanly, one left ambiguous — the two branches the review
# table has to render differently.
_MATCH_A = {
    "issue_id": "i1",
    "identifier": "SIG-1",
    "title": "6.5 Tabla de Carga por responsable",
    "score": 0.95,
    "state_name": "Todo",
    "state_type": "unstarted",
}
_MATCH_B = {
    "issue_id": "i2",
    "identifier": "SIG-2",
    "title": "Mover candidato a Tanda enviada a cliente",
    "score": 0.71,
    "state_name": "Todo",
    "state_type": "unstarted",
}
_MATCH_C = {
    "issue_id": "i3",
    "identifier": "SIG-3",
    "title": "Mover candidato a Tanda enviada a Comercial",
    "score": 0.70,
    "state_name": "Todo",
    "state_type": "unstarted",
}
REWORK_PREVIEW = {
    "team_id": "LT1",
    "cancelled_state": {"id": "st9", "name": "Cancelado", "type": "canceled"},
    "warnings": ["1 of 2 name(s) could not be matched confidently."],
    "matched_count": 1,
    "unresolved_count": 1,
    "resolutions": [
        {
            "name": "6.5 Tabla de Carga por responsable",
            "raw": "- ✅ *6.5 Tabla de Carga por responsable*",
            "status": "matched",
            "method": "score",
            "note": "",
            "chosen": _MATCH_A,
            "candidates": [_MATCH_A],
        },
        {
            "name": "Mover candidato a Tanda enviada",
            "raw": "- ✅ *Mover candidato a Tanda enviada*",
            "status": "ambiguous",
            "method": "",
            "note": "scoring could not separate the top candidates",
            "chosen": None,
            "candidates": [_MATCH_B, _MATCH_C],
        },
    ],
}
REWORK_UNDO = {
    "run_id": "rework-1",
    "summary": "1 original(s) restored, 1 replacement(s) deleted",
    "complete": True,
    "restored_count": 1,
    "deleted_count": 1,
    "failed_count": 0,
    "outcomes": [
        {"issue_id": "i1", "identifier": "SIG-1", "restored_to": "Todo",
         "deleted_issue_id": "n1", "error": "", "ok": True}
    ],
}
REWORK_REPORT = {
    "run_id": "rework-1",
    "undoable": True,
    "parent_issue_id": "p1",
    "summary": "1/1 created, 1 original(s) still open",
    "created_count": 1,
    "failed_count": 0,
    "stranded_count": 1,
    "outcomes": [
        {
            "issue_id": "i1",
            "identifier": "SIG-1",
            "original_title": "6.5 Tabla de Carga por responsable",
            "created_issue_id": "n1",
            "created_title": "6.5 Tabla de Carga por responsable",
            "cancelled": False,
            "error": "",
            "ok": True,
            "needs_attention": True,
        }
    ],
}
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

# The chat endpoint's shape: the same tickets plus the flag that decides
# whether they're readable inside a bubble or must move below the page.
CHAT_GENERATED = {**GENERATED, "overflow": False, "source_document": None}

CHAT_OVERFLOW = {
    "tickets": [_ticket(f"Ticket {i}") for i in range(1, 9)],
    "warnings": [],
    "overflow": True,
    "source_document": "requisitos.md",
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


# What the page actually put on the wire, so a test can assert a request was
# really sent — not merely that the UI looked as though it had been.
LAST_POST: dict = {}


def _post_route(path: str):
    """Canned response for a POST path, or None if it isn't stubbed."""
    base = path.split("?", 1)[0]
    if base.startswith("/api/qa/findings/") and base.endswith("/close"):
        return {**FINDING, "status": "closed", "correction_note": "corregido"}
    table = {
        "/api/tickets/generate": GENERATED,
        "/api/tickets/from-idea": GENERATED,
        "/api/tickets/chat": CHAT_GENERATED,
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
        "/api/qa/tests/plan": {"ticket_id": "i1", "route": "/erp/talent/people",
                               "cases": [{"name": "01-alta", "criterion": "c",
                                          "steps": [{"action": "goto", "target": "/erp/talent/people"}],
                                          "expectation": {"ui_text": "Persona registrada",
                                                          "api_path": "/api/v1/talent/people",
                                                          "api_method": "POST",
                                                          "api_status_range": "2xx"}}],
                               "notes": []},
        "/api/rework/preview": REWORK_PREVIEW,
        "/api/rework/apply": REWORK_REPORT,
        "/api/rework/undo": REWORK_UNDO,
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
        "/api/rework/parents": REWORK_PARENTS,
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
        LAST_POST[self.path.split("?", 1)[0]] = body.decode("utf-8", "replace")
        # Escape hatch so the error/retry path is still reachable now that the
        # happy path is stubbed.
        if b"FORCE_ERROR" in body:
            self.send_error(502, "forced failure")
            return
        # Escape hatch for the too-many-tickets branch, which is a property of
        # the response rather than of anything the UI can be clicked into.
        if b"FORCE_OVERFLOW" in body:
            return self._json(CHAT_OVERFLOW)
        payload = _post_route(self.path)
        if payload is None:
            self.send_error(404)
            return
        self._json(payload)

    def do_GET(self):
        # Progress polling: any token returns a finished-looking log.
        if self.path.startswith("/api/progress/"):
            return self._json({"steps": ["Working…", "Done."]})
        if self.path.startswith("/__last_post"):
            wanted = unquote(self.path.split("?path=", 1)[1]) if "?path=" in self.path else ""
            return self._json({"body": LAST_POST.get(wanted, "")})
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

    def attach_file(self, selector: str, filename: str, content: str = "# Requisitos\n") -> None:
        """Put a real File on a file input, exactly as the file picker would.

        A file input's `files` is read-only to assignment of anything but a
        FileList, so the file is built through a DataTransfer — the same
        object a drag-and-drop would hand it."""
        self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            f"const dt = new DataTransfer();"
            f"dt.items.add(new File([{json.dumps(content)}], {json.dumps(filename)}, "
            "{type: 'text/markdown'}));"
            "el.files = dt.files;"
            "el.dispatchEvent(new Event('change', {bubbles: true}));"
            "return true;"
        )

    def paste(self, selector: str, *, image_b64: str = "", mime: str = "image/png", text: str = "") -> bool:
        """Fire a real paste event carrying an image and/or text.

        The bytes are rebuilt into a genuine File inside the page, and handed
        over on a DataTransfer, so the handler under test sees exactly the
        shape Chrome gives it for a Ctrl+V — not a hand-rolled stand-in.

        Returns whether the app claimed the event (called preventDefault)."""
        return self.eval(
            f"const el = document.querySelector({json.dumps(selector)});"
            "const dt = new DataTransfer();"
            f"const b64 = {json.dumps(image_b64)};"
            "if (b64) {"
            "  const raw = atob(b64);"
            "  const bytes = new Uint8Array(raw.length);"
            "  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);"
            f"  dt.items.add(new File([bytes], 'pasted.png', {{type: {json.dumps(mime)}}}));"
            "}"
            f"const text = {json.dumps(text)};"
            "if (text) dt.setData('text/plain', text);"
            "el.focus();"
            "const event = new ClipboardEvent('paste', "
            "  {clipboardData: dt, bubbles: true, cancelable: true});"
            "el.dispatchEvent(event);"
            # Whether the app claimed the paste. A text paste it does not
            # claim is left for the browser to insert as usual.
            "return event.defaultPrevented;"
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
