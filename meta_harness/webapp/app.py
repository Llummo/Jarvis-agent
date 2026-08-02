"""FastAPI app: QA findings dashboard + ClickUp ticket browser, served with
the static frontend from the same origin (no CORS needed)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.linear_bridge import LinearReadError
from meta_harness.qa_findings import QAFindingNotFoundError
from meta_harness.webapp.routes_clickup import router as clickup_router
from meta_harness.webapp.routes_linear import router as linear_router
from meta_harness.webapp.routes_qa import router as qa_router
from meta_harness.webapp.routes_qa_flow import router as qa_flow_router
from meta_harness.webapp.routes_progress import router as progress_router
from meta_harness.webapp.routes_rework import router as rework_router
from meta_harness.webapp.routes_tickets import router as tickets_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


class RevalidatedStaticFiles(StaticFiles):
    """Serve the UI, but make the browser check it is current.

    The default headers let a browser reuse app.js and index.html from cache
    without asking. After an upgrade that shows the previous version of the
    page — controls that do nothing, panels that are simply absent — and it
    looks like a broken feature rather than a stale file.

    `must-revalidate` still allows a 304, so this costs one conditional
    request per file, not a re-download.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


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
    app.include_router(rework_router, prefix="/api/rework", tags=["rework"])
    app.include_router(progress_router, prefix="/api/progress", tags=["progress"])
    app.mount("/", RevalidatedStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
