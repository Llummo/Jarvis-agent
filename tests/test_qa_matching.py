"""How a step's wording maps onto a real button.

These run against a real headless Chromium and a page whose labels are copied
from Sigo's PERSONAS listing, because the matcher is JavaScript evaluated in the
page — testing it in Python would test a reimplementation, not the thing that
ships.

Both directions matter and are pinned here. A ticket says «Agregar nueva
persona» and the button reads «Agregar Persona»: that has to match, or the
report blames a missing button for a wording difference. But «Eliminar persona»
must NOT match, or a step clicks the sidebar and the run reports a pass for a
deletion nobody performed.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from meta_harness.qa.browser import Browser, _find_chromium

# Copied from the real screen: the button, the two toolbar actions, the sidebar
# entry that shares a word with the button, and the language switch that broke
# an earlier version of the matcher.
FIXTURE_HTML = """<html><body>
  <a href="#personas">Personas</a>
  <a href="#requerimientos">Requerimientos</a>
  <button>Agregar Persona</button>
  <button>Importar CSV</button>
  <button>Plantilla CSV</button>
  <button>ES</button>
</body></html>"""

# Codificado entero: sin escapar, el `#` de un href abre el fragmento y el resto
# del documento se pierde — la página carga vacía y todo empareja con nada.
FIXTURE = "data:text/html," + quote(FIXTURE_HTML)


@pytest.fixture(scope="module")
def page():
    try:
        _find_chromium()
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the skip
        pytest.skip(f"sin Chromium: {exc}")
    with Browser() as browser:
        browser.goto(FIXTURE)
        yield browser


@pytest.mark.parametrize(
    "wanted, expected",
    [
        ("Agregar Persona", "Agregar Persona"),
        # El caso que motivó todo esto: el ticket dice una cosa, el botón otra.
        ("Agregar nueva persona", "Agregar Persona"),
        ("Persona agregar", "Agregar Persona"),
        ("Agregar personas", "Agregar Persona"),
        # Una letra de menos y dos letras cambiadas de sitio son los dos errores
        # de tecleo habituales al redactar un criterio.
        ("Agrgar persona", "Agregar Persona"),
        ("Impotrar CSV", "Importar CSV"),
        # Sin verbo: no hay otra cosa que se llame así.
        ("Agregar", "Agregar Persona"),
    ],
)
def test_finds_the_element_despite_the_wording(page, wanted, expected):
    found = page.find_best_match(wanted)
    assert found is not None, f"{wanted!r} no encontró nada"
    assert found["text"] == expected


@pytest.mark.parametrize(
    "wanted, why",
    [
        # «es» está dentro de «pesroan»: comparar por subcadena hacía que unas
        # letras revueltas emparejaran con el selector de idioma.
        ("Aregagr pesroan", "letras revueltas"),
        # Comparte «persona» con el botón y con el menú, pero el verbo es otro y
        # ninguno de los dos hace lo que pide el paso.
        ("Eliminar persona", "verbo distinto"),
        ("Exportar a contabilidad", "no existe en la pantalla"),
        ("Descargar reporte mensual", "no existe en la pantalla"),
    ],
)
def test_refuses_to_settle_for_the_wrong_element(page, wanted, why):
    assert page.find_best_match(wanted) is None, f"{wanted!r} emparejó pese a: {why}"


def test_reports_whether_the_match_was_exact(page):
    """El informe distingue un acierto literal de uno interpretado."""
    assert page.find_best_match("Agregar Persona")["exact"] is True
    assert page.find_best_match("Agregar nueva persona")["exact"] is False


def test_ignores_elements_with_no_size(page):
    """Un botón oculto no es un botón: hacer clic en él no prueba nada."""
    page.eval(
        "document.querySelectorAll('button').forEach((b) => "
        "{ if (b.innerText === 'Importar CSV') b.style.display = 'none'; }); return true;"
    )
    try:
        assert page.find_best_match("Importar CSV") is None
    finally:
        page.eval(
            "document.querySelectorAll('button').forEach((b) => "
            "{ if (b.innerText === 'Importar CSV') b.style.display = ''; }); return true;"
        )
