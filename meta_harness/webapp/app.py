"""FastAPI app: QA findings dashboard + ClickUp ticket browser, served with
the static frontend from the same origin (no CORS needed)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.linear_bridge import LinearReadError
from meta_harness.qa_findings import QAFindingNotFoundError
from meta_harness.webapp.routes_clickup import router as clickup_router
from meta_harness.webapp.routes_linear import router as linear_router
from meta_harness.webapp.routes_qa import router as qa_router
from meta_harness.webapp.routes_qa_flow import router as qa_flow_router
from meta_harness.webapp.routes_progress import router as progress_router
from meta_harness.webapp.routes_sources import router as sources_router
from meta_harness.webapp.routes_tickets import router as tickets_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


class RevalidatedStaticFiles(StaticFiles):
    """Static assets that must be revalidated on every request.

    StaticFiles sends ETag and Last-Modified but no Cache-Control, which
    leaves browsers free to heuristically cache. That is actively harmful
    here: this is a local tool whose HTML and JS are edited together, and a
    browser holding a stale app.js against fresh markup produces controls that
    silently do nothing — the JS looks for elements the new page no longer
    has, its listener setup dies, and buttons stop responding with no error
    anywhere. `no-cache` still allows a 304, so nothing is re-downloaded
    unless it actually changed.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return super().is_not_modified(response_headers, request_headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def with_revalidation(message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"cache-control", b"no-cache, must-revalidate"))
            await send(message)

        await super().__call__(scope, receive, with_revalidation)


def create_app() -> FastAPI:
    app = FastAPI(title="Meta-Harness UI")

    @app.exception_handler(ClickUpReadError)
    def _clickup_read_error(request: Request, exc: ClickUpReadError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(LinearReadError)
    def _linear_read_error(request: Request, exc: LinearReadError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(QAFindingNotFoundError)
    def _qa_not_found(request: Request, exc: QAFindingNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # /api/* routers must be registered before the "/" static mount — Starlette
    # matches routes in registration order.
    app.include_router(qa_router, prefix="/api/qa", tags=["qa"])
    app.include_router(qa_flow_router, prefix="/api/qa/reviews", tags=["qa-reviews"])
    app.include_router(clickup_router, prefix="/api/clickup", tags=["clickup"])
    app.include_router(linear_router, prefix="/api/linear", tags=["linear"])
    app.include_router(tickets_router, prefix="/api/tickets", tags=["tickets"])
    app.include_router(sources_router, prefix="/api/sources", tags=["sources"])
    app.include_router(progress_router, prefix="/api/progress", tags=["progress"])
    app.mount("/", RevalidatedStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
