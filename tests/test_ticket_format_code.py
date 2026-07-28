"""Fenced code blocks must survive rendering intact.

The Linear renderer bullets every line of the technical notes, which would
mangle a code block: prefixing "* " breaks the fence so it renders as literal
backticks, and dropping the blank lines inside it destroys the snippet.
"""

from meta_harness.ticket_format import bulleted_preserving_code, format_linear_description
from meta_harness.ticket_generator import AcceptanceCriterion, ProposedTicket

SQL = "```sql\nSELECT id, email\nFROM talent_people\n\nWHERE status = 'ACTIVE';\n```"


def _ticket(**kw):
    base = dict(
        title="Registro de persona",
        description="Alta de persona.",
        epic="PERSONAS",
        acceptance_criteria=[AcceptanceCriterion(name="Alta", given="a", when="b", then="c")],
    )
    base.update(kw)
    return ProposedTicket(**base)


# ---------------------------------------------------------------------------
# bulleted_preserving_code
# ---------------------------------------------------------------------------


def test_prose_lines_are_bulleted():
    out = bulleted_preserving_code("Limite de importacion: 100\nPermisos: talent:read")

    assert out == ["* Limite de importacion: 100", "* Permisos: talent:read"]


def test_code_block_lines_are_never_bulleted():
    out = bulleted_preserving_code(f"Consulta base:\n{SQL}")

    assert out[0] == "* Consulta base:"
    assert "```sql" in out
    # not a single line inside the fence may be bulleted
    inside = out[out.index("```sql") + 1 : out.index("```", out.index("```sql") + 1)]
    assert inside == ["SELECT id, email", "FROM talent_people", "", "WHERE status = 'ACTIVE';"]
    assert not any(line.startswith("*") for line in inside)


def test_blank_lines_inside_code_are_preserved():
    out = bulleted_preserving_code(SQL)

    assert "" in out, "a blank line inside a fence is part of the snippet"


def test_blank_lines_outside_code_are_dropped():
    out = bulleted_preserving_code("uno\n\n\ndos")

    assert out == ["* uno", "* dos"]


def test_indentation_inside_code_is_preserved():
    out = bulleted_preserving_code('```json\n{\n  "a": 1\n}\n```')

    assert '  "a": 1' in out


def test_already_bulleted_prose_is_not_double_bulleted():
    out = bulleted_preserving_code("* ya tiene vinieta\n- tambien esta")

    assert out == ["* ya tiene vinieta", "- tambien esta"]


def test_an_unclosed_fence_is_closed():
    # Otherwise the missing fence swallows everything rendered after it.
    out = bulleted_preserving_code("Nota:\n```\nSELECT 1;")

    assert out[-1] == "```"
    assert out.count("```") == 2


# ---------------------------------------------------------------------------
# end to end through the Linear renderer
# ---------------------------------------------------------------------------


def test_linear_description_keeps_a_code_block_intact():
    body = format_linear_description(_ticket(technical_notes=f"Consulta usada:\n{SQL}"))

    assert "* Consulta usada:" in body
    assert "```sql" in body
    assert "* SELECT id, email" not in body, "code must not be bulleted"
    assert "SELECT id, email" in body
    assert body.count("```") == 2


def test_linear_description_still_bullets_plain_notes():
    body = format_linear_description(_ticket(technical_notes="Limite: 100 por lote"))

    assert "* Limite: 100 por lote" in body
    assert "```" not in body


def test_linear_description_handles_notes_that_are_only_code():
    body = format_linear_description(_ticket(technical_notes=SQL))

    assert "```sql" in body
    assert "FROM talent_people" in body
