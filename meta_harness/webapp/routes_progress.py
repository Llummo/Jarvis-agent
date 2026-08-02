"""Polling endpoint for step-by-step progress of long-running agent calls.

See meta_harness/webapp/progress.py for how steps get pushed.
"""

from __future__ import annotations

from fastapi import APIRouter

from meta_harness.webapp import progress
from meta_harness.webapp.schemas import ProgressOut

router = APIRouter()


@router.get("/{token}", response_model=ProgressOut)
def get_progress(token: str) -> ProgressOut:
    return ProgressOut(
        steps=progress.get(token),
        done=progress.is_done(token),
        error=progress.error_of(token),
    )
