"""Getting past the login screen, and noticing when we did not."""

import pytest

from meta_harness.qa.auth import (
    AuthError,
    assert_authenticated,
    authenticate,
    looks_like_login,
    token_for,
)
from meta_harness.qa.environments import resolve_environment

LOGIN_ES = """METRICA
SIGO · PLATAFORMA INTERNA
Inicia sesión
Continúa con tu cuenta corporativa.
Continuar con Google
Continuar con Microsoft
O CON CORREO
Correo"""

LOGIN_EN = "Sign in\nContinue with Google\nContinue with Microsoft"

APP = """Personas
Agregar nueva persona
First name  Paternal last name  Email  Phone  Source  LinkedIn
Guardar"""


# --- detectar el login --------------------------------------------------------


@pytest.mark.parametrize("text", [LOGIN_ES, LOGIN_EN])
def test_the_login_screen_is_recognised(text):
    assert looks_like_login(text)


def test_the_real_screen_is_not_mistaken_for_login():
    assert not looks_like_login(APP)


def test_an_empty_page_is_not_a_login():
    assert not looks_like_login("")


def test_detection_reads_the_page_not_the_url():
    """La SPA redirige en el cliente: la URL puede seguir siendo la pedida
    mientras la pantalla ya es el login."""
    assert looks_like_login("Inicia sesión")


# --- token por entorno --------------------------------------------------------


def test_each_environment_has_its_own_token(monkeypatch):
    monkeypatch.setenv("SIGO_LOCAL_TOKEN", "local-abc")
    monkeypatch.setenv("SIGO_QA_TOKEN", "qa-xyz")
    assert token_for("local") == "local-abc"
    assert token_for("qa") == "qa-xyz"


def test_a_missing_token_is_empty_not_an_error(monkeypatch):
    monkeypatch.delenv("SIGO_LOCAL_TOKEN", raising=False)
    assert token_for("local") == ""


def test_the_environment_carries_its_token(monkeypatch):
    monkeypatch.setenv("SIGO_LOCAL_TOKEN", "abc123")
    env = resolve_environment("local")
    assert env.auth_token == "abc123"


def test_describe_says_whether_there_is_a_session_but_not_the_token(monkeypatch):
    """El token no debe viajar en ninguna respuesta ni reporte."""
    monkeypatch.setenv("SIGO_LOCAL_TOKEN", "secreto-que-no-debe-salir")
    described = resolve_environment("local").describe()
    assert described["authenticated"] == "sí"
    assert "secreto" not in str(described)


# --- instalar la sesión -------------------------------------------------------


class _FakeBrowser:
    def __init__(self, text=APP, accepts=True):
        self.visited, self.scripts, self._text, self._accepts = [], [], text, accepts

    def goto(self, url):
        self.visited.append(url)

    def eval(self, script):
        self.scripts.append(script)
        return self._accepts

    def text_of(self, selector="body"):
        return self._text


def test_the_origin_is_loaded_before_writing_the_session():
    """localStorage vive por origen: escribirlo en una página en blanco lo
    guardaría donde la aplicación nunca lo lee."""
    b = _FakeBrowser()
    authenticate(b, "http://localhost:5180", "abc123")
    assert b.visited == ["http://localhost:5180"]
    assert "localStorage.setItem" in b.scripts[0]
    assert "abc123" in b.scripts[0]


def test_without_a_token_nothing_is_installed():
    b = _FakeBrowser()
    assert authenticate(b, "http://localhost:5180", "") is False
    assert b.visited == []


def test_a_browser_that_refuses_storage_is_reported():
    with pytest.raises(AuthError, match="localStorage"):
        authenticate(_FakeBrowser(accepts=False), "http://x", "abc")


# --- fallar claro -------------------------------------------------------------


def test_landing_on_the_login_raises_with_the_real_cause():
    with pytest.raises(AuthError, match="inicio de sesión"):
        assert_authenticated(_FakeBrowser(text=LOGIN_ES), where="el paso «goto '/'»")


def test_the_error_names_the_variable_to_configure():
    try:
        assert_authenticated(_FakeBrowser(text=LOGIN_ES), where="x")
    except AuthError as exc:
        assert "token" in str(exc).lower()


def test_a_real_screen_passes_through():
    assert_authenticated(_FakeBrowser(text=APP), where="x")
