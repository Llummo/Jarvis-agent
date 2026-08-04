"""One browser, held open across separate tool calls.

MCP tools arrive as independent calls: «open», then «click», then «read». A
browser that started and died inside each of them could never carry a session,
so the model could never log in, fill a form, or see what its own click did.

So the browser is held here, process-wide, until something closes it. That is a
deliberate piece of shared state — the alternative is a model that can only ever
look at first pages.

The environment guard lives on the way in. Every session names a target, and
`resolve_environment` refuses anything that looks like production, so no
sequence of tool calls can steer a live browser at real data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from meta_harness.qa.browser import Browser, BrowserError
from meta_harness.qa.environments import Environment, resolve_environment


class SessionError(RuntimeError):
    """No session is open, or the one asked for cannot be started."""


class BrowserSession:
    """A single live browser, plus the environment it is pointed at."""

    def __init__(self) -> None:
        self._browser: Optional[Browser] = None
        self._environment: Optional[Environment] = None

    @property
    def open(self) -> bool:
        return self._browser is not None

    def start(self, environment: str, *, ui_url: Optional[str] = None) -> Dict[str, Any]:
        """Open a browser against a named environment, replacing any previous one."""
        target = resolve_environment(environment, ui_url=ui_url)
        self.stop()
        browser = Browser()
        browser.__enter__()
        self._browser = browser
        self._environment = target
        return {"opened": True, "environment": target.describe()}

    def stop(self) -> Dict[str, Any]:
        was_open = self._browser is not None
        if self._browser is not None:
            try:
                self._browser.__exit__(None, None, None)
            finally:
                self._browser = None
                self._environment = None
        return {"closed": was_open}

    def require(self) -> Browser:
        if self._browser is None:
            raise SessionError("No hay navegador abierto. Llama primero a qa_browser_open.")
        return self._browser

    def environment(self) -> Environment:
        if self._environment is None:
            raise SessionError("No hay navegador abierto. Llama primero a qa_browser_open.")
        return self._environment

    # -- the actions a model needs to explore ----------------------------

    def goto(self, route: str) -> Dict[str, Any]:
        browser, env = self.require(), self.environment()
        browser.reset_network()
        url = env.url_for(route)
        browser.goto(url)
        return {"url": url, "title": browser.eval("return document.title;")}

    def click_text(self, text: str) -> Dict[str, Any]:
        browser = self.require()
        selector = browser.find_by_text(text)
        if not selector:
            raise BrowserError(f"No se encontró nada que diga {text!r}")
        browser.click(selector)
        return {"clicked": text, "selector": selector}

    def type_label(self, label: str, value: str) -> Dict[str, Any]:
        browser = self.require()
        selector = browser.find_by_text(label, role="input")
        if not selector:
            raise BrowserError(f"No se encontró un campo llamado {label!r}")
        browser.type_into(selector, value)
        return {"typed_into": label, "selector": selector}

    def read(self, *, limit: int = 4000) -> Dict[str, Any]:
        """What the page says, truncated — a model does not need the whole DOM
        to decide what to click next, and a full page would crowd out the rest
        of its context."""
        browser = self.require()
        text = browser.text_of()
        return {
            "url": browser.eval("return location.href;"),
            "text": text[:limit],
            "truncated": len(text) > limit,
        }

    def find(self, text: str) -> Dict[str, Any]:
        browser = self.require()
        selector = browser.find_by_text(text)
        return {"found": bool(selector), "selector": selector or "", "text": text}

    def network(self) -> List[Dict[str, Any]]:
        return [call.to_dict() for call in self.require().api_calls()]

    def screenshot(self, path: Optional[str] = None) -> Dict[str, Any]:
        browser = self.require()
        from meta_harness.mcp_server.cdp_screenshot import default_screenshot_path

        target = Path(path) if path else default_screenshot_path()
        return {"screenshot": str(browser.screenshot(target))}


# Process-wide. One model, one browser.
SESSION = BrowserSession()
