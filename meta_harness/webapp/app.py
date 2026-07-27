"""FastAPI app: QA findings dashboard + ClickUp ticket browser, served with
the static frontend from the same origin (no CORS needed)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from meta_harness.clickup_bridge import ClickUpReadError
from meta_harness.qa_findings import QAFindingNotFoundError
from meta_harness.webapp.routes_clickup import router as clickup_router
from meta_harness.webapp.routes_qa import router as qa_router
from meta_harness.webapp.routes_qa_flow import router as qa_flow_router
from meta_harness.webapp.routes_progress import router as progress_router
from meta_harness.webapp.routes_tickets import router as tickets_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Meta-Harness UI")

    @app.exception_handler(ClickUpReadError)
    def _clickup_read_error(request: Request, exc: ClickUpReadError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(QAFindingNotFoundError)
    def _qa_not_found(request: Request, exc: QAFindingNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # /api/* routers must be registered before the "/" static mount — Starlette
    # matches routes in registration order.
    app.include_router(qa_router, prefix="/api/qa", tags=["qa"])
    app.include_router(qa_flow_router, prefix="/api/qa/reviews", tags=["qa-reviews"])
    app.include_router(clickup_router, prefix="/api/clickup", tags=["clickup"])
    app.include_router(tickets_router, prefix="/api/tickets", tags=["tickets"])
    app.include_router(progress_router, prefix="/api/progress", tags=["progress"])
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
