"""Templates must survive a rewrite byte-for-byte.

A ticket that says «the body greets the client and lists the requirement» is
useless for actually sending email. The literal text is the deliverable.
"""

import pytest

from meta_harness.ticket_format import (
    extract_verbatim_blocks,
    format_clickup_description,
    format_linear_description,
)
from meta_harness.ticket_generator import ProposedTicket

TEMPLATE = """```
Estimado(a) {nombre_cliente}:

Agradecemos el tiempo brindado en la reunión de kickoff para la posición
de {cargo}.

Saludos cordiales,
{nombre_remitente}
Metrica - Conectando estrategia, tecnologia y personas
```"""

BODY = f"""Como reclutador, quiero enviar el correo estándar.

{TEMPLATE}

El asunto se compone aparte.
"""


# --- extraction ---------------------------------------------------------------


def test_a_fenced_template_is_extracted_whole():
    blocks = extract_verbatim_blocks(BODY)
    assert len(blocks) == 1
    assert "Estimado(a) {nombre_cliente}:" in blocks[0]
    assert "{nombre_remitente}" in blocks[0]
    assert blocks[0].startswith("```") and blocks[0].endswith("```")


def test_every_variable_survives():
    """A lost {variable} is a template that cannot be filled in."""
    block = extract_verbatim_blocks(BODY)[0]
    for variable in ("{nombre_cliente}", "{cargo}", "{nombre_remitente}"):
        assert variable in block


def test_blank_lines_inside_the_template_are_kept():
    """Paragraph breaks are part of an email, not formatting noise."""
    assert "\n\n" in extract_verbatim_blocks(BODY)[0]


def test_several_templates_are_all_kept_in_order():
    two = BODY + "\n```\nHola {nombre_candidato}\n```\n"
    blocks = extract_verbatim_blocks(two)
    assert len(blocks) == 2
    assert "Estimado" in blocks[0] and "Hola" in blocks[1]


def test_duplicates_are_collapsed():
    assert len(extract_verbatim_blocks(BODY + "\n" + TEMPLATE)) == 1


def test_prose_is_not_mistaken_for_a_template():
    assert extract_verbatim_blocks("Solo prosa, sin bloques.") == []


def test_an_empty_description_yields_nothing():
    assert extract_verbatim_blocks("") == []


# --- rendering ----------------------------------------------------------------


def _ticket(**over):
    payload = {
        "title": "2.7. Template de correo",
        "description": "Enviar el correo al pasar a Tanda enviada a cliente.",
        "verbatim_blocks": extract_verbatim_blocks(BODY),
    }
    payload.update(over)
    return ProposedTicket(**payload)


def test_the_rendered_ticket_carries_the_template_verbatim():
    rendered = format_linear_description(_ticket())
    assert "Estimado(a) {nombre_cliente}:" in rendered
    assert "Metrica - Conectando estrategia, tecnologia y personas" in rendered


def test_the_template_gets_its_own_labelled_section():
    """So nobody edits it thinking it is commentary."""
    rendered = format_linear_description(_ticket())
    assert "CONTENIDO LITERAL" in rendered
    assert rendered.index("CONTENIDO LITERAL") < rendered.index("NOTAS TÉCNICAS")


def test_technical_notes_still_render_alongside():
    rendered = format_linear_description(_ticket(technical_notes="Usar el proveedor SMTP."))
    assert "CONTENIDO LITERAL" in rendered
    assert "Usar el proveedor SMTP." in rendered


def test_a_ticket_without_templates_is_unchanged():
    rendered = format_linear_description(_ticket(verbatim_blocks=[]))
    assert "CONTENIDO LITERAL" not in rendered
    assert "NOTAS TÉCNICAS" in rendered


def test_the_clickup_renderer_does_not_crash_on_the_new_field():
    assert format_clickup_description(_ticket())


# --- the reformat path --------------------------------------------------------


def test_reformatting_carries_the_template_across(monkeypatch):
    """The model rewrites the prose; the template must not be part of what it
    is allowed to rewrite."""
    import json

    from meta_harness import ticket_reformat

    original = f"Texto viejo y desordenado.\n\n{TEMPLATE}\n"
    monkeypatch.setattr(ticket_reformat, "_fetch", lambda tid, tracker: ("Viejo título", original))
    monkeypatch.setattr(ticket_reformat, "_find_claude", lambda: "/usr/bin/claude")
    # The model paraphrases the template away — exactly the reported failure.
    monkeypatch.setattr(
        ticket_reformat,
        "_run_claude",
        lambda *a, **k: json.dumps([{
            "title": "2.7. Template de correo",
            "description": "El cuerpo saluda al cliente y cierra con la firma.",
            "acceptance_criteria": [
                {"name": "Envío", "given": "una tanda", "when": "pasa a enviada", "then": "se manda el correo"}
            ],
        }]),
    )

    result = ticket_reformat.reformat_ticket("SIG-130", tracker="linear")

    assert result.ticket.verbatim_blocks, "el bloque literal debe conservarse"
    assert "Estimado(a) {nombre_cliente}:" in result.formatted_description
    assert "{nombre_remitente}" in result.formatted_description
