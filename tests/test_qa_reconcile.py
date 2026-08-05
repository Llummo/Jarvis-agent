"""Lining a ticket's claims up against the screen that exists.

The case these were written from is SIG-109, whose criteria describe the
add-candidate form that used to live under Requerimientos. That form is gone;
Personas asks for different things. The point of reconciliation is that this
shows up as a stated gap rather than as a plan quietly written against whatever
the screen happens to offer.
"""

from __future__ import annotations

import pytest

from meta_harness.qa.reconcile import (
    FOUND_EQUIVALENT,
    FOUND_EXACT,
    FOUND_MISSING,
    describe,
    reconcile,
    summarize,
)
from meta_harness.qa.textmatch import best_match, score
from meta_harness.qa.vocabulary import Vocabulary

# The real Personas screen, trimmed to what matters here.
PERSONAS = Vocabulary(
    actions=["Importar CSV", "Plantilla CSV", "Agregar persona"],
    form_actions=["Cancelar", "Volver", "Agregar persona"],
    fields=[
        "Nombres *",
        "Apellido paterno *",
        "Correo electrónico *",
        "Teléfono *",
        "LinkedIn",
        "Resumen profesional",
    ],
    texts=["PERSONAS", "AGREGAR PERSONA", "INFORMACIÓN BÁSICA"],
)


@pytest.mark.parametrize(
    "wanted, expected",
    [
        ("Agregar Persona", "Agregar persona"),
        ("Agregar nueva persona", "Agregar persona"),
        ("Importar csv", "Importar CSV"),
        # Un typo y una transposición: los dos errores de tecleo habituales.
        ("Agrgar persona", "Agregar persona"),
        ("Impotrar CSV", "Importar CSV"),
    ],
)
def test_the_python_matcher_agrees_with_the_browser(wanted, expected):
    """Misma puntuación aquí que en la página; si divergen, mienten los dos."""
    found = best_match(wanted, PERSONAS.actions)
    assert found is not None and found[0] == expected


@pytest.mark.parametrize(
    "wanted",
    [
        "Aregagr pesroan",     # letras revueltas
        "Eliminar persona",    # el verbo cambia lo que hace
        "Exportar a contabilidad",
    ],
)
def test_the_python_matcher_refuses_the_wrong_thing(wanted):
    assert best_match(wanted, PERSONAS.actions) is None


def test_score_is_measured_against_what_was_asked():
    """A la etiqueta le pueden sobrar palabras; que le falten es otra cosa."""
    assert score("Agregar", "Agregar persona") >= 0.6
    assert score("Eliminar persona", "Personas") < 0.6


def test_a_field_the_ticket_names_in_english_resolves_to_the_spanish_label():
    matches = reconcile(["LinkedIn"], PERSONAS)
    assert matches[0].status == FOUND_EXACT
    assert matches[0].where == "campo"


def test_a_field_that_was_removed_is_reported_as_absent():
    """«First name» es del formulario viejo: no está, y no se sustituye."""
    matches = reconcile(["First name", "Paternal last name"], PERSONAS)
    assert [match.status for match in matches] == [FOUND_MISSING, FOUND_MISSING]
    assert all(match.label == "" for match in matches)


def test_an_absent_element_becomes_a_note_for_whoever_reviews_the_plan():
    notes = summarize(reconcile(["First name"], PERSONAS))
    assert len(notes) == 1
    assert "First name" in notes[0]
    # La nota no decide cuál de las dos causas es: eso lo dice una persona.
    assert "desactualizado" in notes[0] and "no está hecha" in notes[0]


def test_the_text_pass_claims_only_what_is_obvious():
    """Un parecido no basta para afirmar que algo existe.

    «Resumen» y «Resumen profesional» puntúan 0.85, y «Email» contra la entrada
    de menú «Automatización de Email» puntúa 0.80. Separar los dos con un umbral
    entre ambos sería afinar una constante a dos ejemplos. En vez de eso la
    pasada textual solo reclama la coincidencia casi literal, y todo lo demás
    —incluido «Resumen»— pasa a resolverse por sentido, donde una respuesta
    puede ser «no hay nada que haga eso».
    """
    assert 0.8 < score("Resumen", "Resumen profesional") < 0.9
    assert reconcile(["Resumen"], PERSONAS)[0].status == FOUND_MISSING


def test_a_word_shared_with_a_menu_entry_is_not_a_match():
    """«Email» aparece dentro de «Automatización de Email», y no es ese campo."""
    vocabulary = Vocabulary(actions=["Automatización de Email"], fields=["Correo electrónico *"])
    assert reconcile(["Email"], vocabulary)[0].status == FOUND_MISSING


def test_a_meaning_match_is_declared_as_such():
    """«Phone» → «Teléfono *» hay que creérselo; «LinkedIn» se comprueba leyendo."""
    from meta_harness.qa.reconcile import Match

    literal = Match(term="LinkedIn", status=FOUND_EXACT, label="LinkedIn", where="campo")
    semantic = Match(
        term="Phone", status=FOUND_EQUIVALENT, label="Teléfono *",
        where="campo", by_meaning=True,
    )
    assert "equivale a" in semantic.describe()
    assert "equivale a" not in literal.describe()


def test_the_prompt_text_tells_the_author_not_to_substitute_what_is_missing():
    text = describe(reconcile(["First name", "LinkedIn"], PERSONAS))
    assert "no existe en la pantalla" in text
    assert "no lo sustituyas" in text


def test_reconciling_without_a_screen_yields_nothing():
    """Sin vocabulario no hay nada que contrastar, y no se inventa."""
    assert reconcile(["First name"], None) == []
    assert reconcile([], PERSONAS) == []
