"""Where a test run is allowed to point.

Browser tests click real buttons, and clicking a real button writes real data.
So the target is not a free-text URL: it is one of a named set, and production
is not in it.

Two are supported — the machine you are working on, and the deployed QA site.
Both are safe to write to, which is what makes the tests worth running: a
read-only check could never verify that saving a form works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

ENV_LOCAL = "local"
ENV_QA = "qa"
ENVIRONMENTS = (ENV_LOCAL, ENV_QA)

# Overridable per machine — a deployed QA host is not the same for everyone.
UI_ENV_VARS = {ENV_LOCAL: "SIGO_LOCAL_URL", ENV_QA: "SIGO_QA_URL"}
API_ENV_VARS = {ENV_LOCAL: "SIGO_LOCAL_API_URL", ENV_QA: "SIGO_QA_API_URL"}

DEFAULT_UI = {ENV_LOCAL: "http://localhost:3000", ENV_QA: ""}

# Hostnames that must never be a target. A test suite that can reach
# production is one bad config away from writing to it.
FORBIDDEN_HOST_HINTS = ("prod", "production", "www.", "metrica-global.com")


class EnvironmentError_(ValueError):
    """The requested target is unknown, unconfigured, or not allowed."""


@dataclass(frozen=True)
class Environment:
    """One place tests may run against."""

    name: str
    ui_url: str
    # Where the API lives, when it is not served from the same origin. Empty
    # means «same host as the UI», which is the common case.
    api_url: str = ""

    def api_base(self) -> str:
        return (self.api_url or self.ui_url).rstrip("/")

    def url_for(self, route: str) -> str:
        return f"{self.ui_url.rstrip('/')}/{route.lstrip('/')}"

    def describe(self) -> Dict[str, str]:
        return {"name": self.name, "ui_url": self.ui_url, "api_url": self.api_base()}


def _guard_not_production(name: str, url: str) -> None:
    lowered = url.lower()
    for hint in FORBIDDEN_HOST_HINTS:
        if hint in lowered:
            raise EnvironmentError_(
                f"Refusing to run browser tests against {url!r} (environment {name!r}): "
                f"it looks like production. These tests click real buttons and write real data."
            )


def resolve_environment(name: str, *, ui_url: Optional[str] = None, api_url: Optional[str] = None) -> Environment:
    """Resolve a named environment, from an override or the process env."""
    if name not in ENVIRONMENTS:
        raise EnvironmentError_(
            f"Unknown environment {name!r}. Supported: {', '.join(ENVIRONMENTS)}. "
            "Production is deliberately not one of them."
        )

    resolved_ui = (ui_url or os.getenv(UI_ENV_VARS[name], "") or DEFAULT_UI[name]).strip()
    if not resolved_ui:
        raise EnvironmentError_(
            f"No URL configured for the {name!r} environment. "
            f"Set {UI_ENV_VARS[name]} or pass one explicitly."
        )
    resolved_api = (api_url or os.getenv(API_ENV_VARS[name], "")).strip()

    _guard_not_production(name, resolved_ui)
    if resolved_api:
        _guard_not_production(name, resolved_api)

    return Environment(name=name, ui_url=resolved_ui, api_url=resolved_api)
