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

_STALE_AFTER_S = 300.0  # dropped lazily whenever a new token is started


def start(token: str) -> None:
    with _lock:
        _steps[token] = []
        _touched_at[token] = time.monotonic()
        _prune_locked()


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
