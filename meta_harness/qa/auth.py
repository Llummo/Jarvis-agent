"""Get the browser past the login screen, and notice when it did not.

Sigo authenticates with OTP or a Microsoft/Google account. Neither can be
driven from a script: an OTP arrives by email and OAuth leaves the app. So the
browser is not asked to log in — it is handed a session that already exists.

The frontend keeps its session in `localStorage["token"]` and sends it as a
bearer header, so writing that one key is the whole of it. The token comes from
the environment, never from a form.

The second half matters as much as the first. Without it a run against an
unauthenticated browser still «succeeds»: every navigation returns the login
screen, every screenshot captures it, and the report reads as evidence of a
feature nobody tested.
"""

from __future__ import annotations

import json
import os
from typing import Optional

# The key the Sigo frontend reads on boot. Writing it before the app loads is
# indistinguishable, to the app, from having logged in.
TOKEN_STORAGE_KEY = "token"

# The admin app authenticates with this cookie — see pkg/iam/auth/middleware.go,
# which falls back to c.Cookies("access_token") when there is no bearer header.
# It is httpOnly, so it has to be installed through CDP, not from JavaScript.
SESSION_COOKIE = "access_token"

# Per-environment token, so a local session is never sent to the deployed site.
TOKEN_ENV_VARS = {"local": "SIGO_LOCAL_TOKEN", "qa": "SIGO_QA_TOKEN"}

# Phrases that only appear when the app is asking who you are. Kept in both
# languages because the login screen follows the browser locale.
_LOGIN_MARKERS = (
    "inicia sesión",
    "iniciar sesión",
    "continuar con google",
    "continuar con microsoft",
    "continúa con tu cuenta",
    "sign in",
    "log in",
    "continue with google",
    "continue with microsoft",
)

# How many of those have to appear. One is enough — «Sign in» alone is decisive
# — but requiring the phrase to be prominent avoids matching a footer link.
_MIN_MARKERS = 1


class AuthError(RuntimeError):
    """The browser is not authenticated, or no token was configured."""


def token_for(environment: str) -> str:
    """The session token configured for an environment, or empty."""
    var = TOKEN_ENV_VARS.get(environment)
    return (os.getenv(var, "") if var else "").strip()


def looks_like_login(page_text: str) -> bool:
    """Whether this page is the login screen.

    Deliberately checks the rendered text rather than the URL: the app redirects
    on the client, so the address bar can still read /erp/talent/people while
    the screen shows a login form.
    """
    if not page_text:
        return False
    lowered = page_text.lower()
    hits = sum(1 for marker in _LOGIN_MARKERS if marker in lowered)
    return hits >= _MIN_MARKERS


def inject_script(token: str) -> str:
    """JS that installs the session, for running before the app boots."""
    return (
        f"try {{ localStorage.setItem({json.dumps(TOKEN_STORAGE_KEY)}, {json.dumps(token)}); "
        "return true; } catch (e) { return false; }"
    )


def authenticate(browser, base_url: str, token: Optional[str]) -> bool:
    """Install `token` for `base_url`, returning whether anything was done.

    The origin must be loaded first: localStorage is scoped per origin, so
    writing the key on a blank page would store it somewhere the app never
    reads.
    """
    if not token:
        return False
    browser.goto(base_url)
    # The cookie is what the admin app actually reads.
    browser.set_cookie(SESSION_COOKIE, token, base_url)
    # The portals read a bearer token from storage instead; setting both costs
    # nothing and covers either surface.
    browser.eval(inject_script(token))
    return True


def assert_authenticated(browser, *, where: str) -> None:
    """Raise if the current page is the login screen.

    Called after navigating, so a missing or expired token stops the run with
    the real reason instead of producing screenshots of a login form.
    """
    if looks_like_login(browser.text_of()):
        raise AuthError(
            f"la sesión no es válida: {where} mostró la pantalla de inicio de sesión. "
            "Configure un token vigente antes de ejecutar las pruebas."
        )
