"""In-memory progress tracker for long-running agent operations.

A single POST to e.g. /api/qa/reviews can take a minute (fetch a ClickUp
ticket, call Claude, possibly retry with a repair prompt) and the caller
has no visibility into which of those steps is currently happening. The
client generates a token up front, passes it in the request body, and
polls GET /api/progress/{token} from a second, concurrent request while
the main one is in flight; route handlers push a message here each time
a real step starts.

Deliberately a plain in-process dict, not a queue/pubsub — this is a
single-user local dev tool, not a multi-worker deployment.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List

_lock = threading.Lock()
_steps: Dict[str, List[str]] = {}
_touched_at: Dict[str, float] = {}
# Terminal state for work that outlives its HTTP request. Indexing a large
# repository runs for minutes to hours, so it cannot be held open on the
# response — the client polls instead, and needs to know when to stop.
_done: Dict[str, bool] = {}
_errors: Dict[str, str] = {}

_STALE_AFTER_S = 21600.0  # 6h — indexing a large repository is a long job


def start(token: str) -> None:
    with _lock:
        _steps[token] = []
        _done[token] = False
        _errors.pop(token, None)
        _touched_at[token] = time.monotonic()
        _prune_locked()


def finish(token: str, *, error: str = "") -> None:
    """Mark a job terminal. An empty error means it succeeded."""
    with _lock:
        _done[token] = True
        if error:
            _errors[token] = error
        _touched_at[token] = time.monotonic()


def is_done(token: str) -> bool:
    with _lock:
        return _done.get(token, False)


def error_of(token: str) -> str:
    with _lock:
        return _errors.get(token, "")


def push(token: str, message: str) -> None:
    with _lock:
        _steps.setdefault(token, []).append(message)
        _touched_at[token] = time.monotonic()


def get(token: str) -> List[str]:
    with _lock:
        return list(_steps.get(token, []))


def _prune_locked() -> None:
    now = time.monotonic()
    stale = [token for token, ts in _touched_at.items() if now - ts > _STALE_AFTER_S]
    for token in stale:
        _steps.pop(token, None)
        _touched_at.pop(token, None)
        _done.pop(token, None)
        _errors.pop(token, None)
