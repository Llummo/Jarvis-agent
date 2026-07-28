"""A screenshot must wait for the app to paint, not just for the document.

Page.loadEventFired fires when the HTML finishes loading. A single-page app
serves an empty shell and renders from JavaScript afterwards, so capturing at
that moment produced a blank image — or the router's transient "Not Found"
fallback — instead of the screen being reviewed.
"""

import asyncio

import pytest

from meta_harness.mcp_server.cdp_screenshot import (
    RENDER_SETTLE_GRACE_S,
    RENDER_SETTLE_TIMEOUT_S,
    _wait_for_content,
)


def _send_returning(values):
    """A fake CDP send() yielding each value in turn from Runtime.evaluate."""
    calls = {"n": 0}

    async def send(method, params=None):
        index = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return {"result": {"result": {"value": values[index]}}}

    return send, calls


def test_returns_as_soon_as_the_page_has_content():
    send, calls = _send_returning([True])

    asyncio.run(_wait_for_content(send, timeout_s=5))

    assert calls["n"] == 1


def test_keeps_polling_until_the_app_renders():
    # An empty shell first, then content once the client-side app mounts.
    send, calls = _send_returning([False, False, True])

    asyncio.run(_wait_for_content(send, timeout_s=5))

    assert calls["n"] == 3


def test_gives_up_quietly_on_a_genuinely_blank_page():
    # A blank page is still evidence worth capturing, so this must not raise.
    send, _ = _send_returning([False])

    asyncio.run(_wait_for_content(send, timeout_s=0.4))


def test_a_cdp_failure_never_blocks_the_capture():
    async def send(method, params=None):
        raise RuntimeError("CDP went away")

    asyncio.run(_wait_for_content(send, timeout_s=5))  # must not raise


def test_it_actually_waits_after_finding_content():
    # Without the grace period a half-drawn frame gets captured.
    send, _ = _send_returning([True])

    async def timed():
        start = asyncio.get_event_loop().time()
        await _wait_for_content(send, timeout_s=5)
        return asyncio.get_event_loop().time() - start

    assert asyncio.run(timed()) >= RENDER_SETTLE_GRACE_S


def test_the_settle_budget_is_bounded():
    # A page that never renders must not hold the QA sweep open indefinitely.
    assert 0 < RENDER_SETTLE_TIMEOUT_S <= 15
