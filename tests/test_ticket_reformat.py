import json

import pytest

from meta_harness.ticket_generator import TicketParseError
from meta_harness.ticket_reformat import apply_reformatted_ticket, reformat_ticket


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


VALID = json.dumps(
    [
        {
            "title": "TAB-07 | Endpoint de exportación",
            "epic": "GESTIÓN DE CANDIDATOS",
            "ui_route": "",
            "backend_endpoint": "GET /api/v1/candidates/export",
            "user_story": "Como reclutador,\nquiero exportar candidatos,\npara analizarlos fuera del sistema.",
            "description": "Endpoint que devuelve un Excel.",
            "technical_notes": "Formato .xlsx",
            "acceptance_criteria": [
                {"name": "Descarga", "given": "hay candidatos", "when": "se invoque", "then": "se descarga un .xlsx"}
            ],
            "priority": "normal",
            "category": "backend",
            "sprint": 1,
        }
    ]
)


def _mock_clickup(monkeypatch, name="TAB-07 | exportar", description="hay que exportar a excel"):
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.get_clickup_task",
        lambda ticket_id, **kw: {"id": ticket_id, "name": name, "text_content": description},
    )


def _mock_linear(monkeypatch, title="SIG-9 exportar", description="hay que exportar a excel"):
    monkeypatch.setattr(
        "meta_harness.ticket_reformat.get_linear_issue",
        lambda issue_id, **kw: {"id": issue_id, "title": title, "description": description},
    )


def _mock_claude(monkeypatch, stdout=VALID):
    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", lambda *a, **k: Result(stdout=stdout))


# ---------------------------------------------------------------------------
# reformat_ticket — preview only
# ---------------------------------------------------------------------------


def test_reformat_returns_the_original_and_the_proposal(monkeypatch):
    _mock_clickup(monkeypatch, name="TAB-07 | exportar", description="texto viejo")
    _mock_claude(monkeypatch)

    result = reformat_ticket("T1", tracker="clickup", timeout_s=5)

    assert result.original_title == "TAB-07 | exportar"
    assert result.original_description == "texto viejo"
    assert result.formatted_title == "TAB-07 | Endpoint de exportación"
    assert "📝 DESCRIPCIÓN" in result.formatted_description
    assert result.ticket.epic == "GESTIÓN DE CANDIDATOS"


def test_reformat_renders_with_the_linear_house_format(monkeypatch):
    _mock_linear(monkeypatch)
    _mock_claude(monkeypatch)

    result = reformat_ticket("I1", tracker="linear", timeout_s=5)

    # Linear's bulleted criteria, not ClickUp's one-liner
    assert "📌 Criterio 1: Descarga" in result.formatted_description
    assert "* Dado que hay candidatos," in result.formatted_description
    assert "📌 Criterio X: [Espacio para criterios adicionales]" in result.formatted_description


def test_reformat_renders_with_the_clickup_format(monkeypatch):
    _mock_clickup(monkeypatch)
    _mock_claude(monkeypatch)

    result = reformat_ticket("T1", tracker="clickup", timeout_s=5)

    assert "📌 Criterio 1: Descarga: Dado que hay candidatos, cuando se invoque, entonces se descarga un .xlsx." in (
        result.formatted_description
    )


def test_reformat_never_writes_back(monkeypatch):
    _mock_clickup(monkeypatch)
    _mock_claude(monkeypatch)

    def boom(*a, **kw):
        raise AssertionError("reformat_ticket must not write to the tracker")

    monkeypatch.setattr("meta_harness.ticket_reformat.update_clickup_task", boom)
    monkeypatch.setattr("meta_harness.ticket_reformat.update_linear_issue", boom)

    reformat_ticket("T1", tracker="clickup", timeout_s=5)


def test_reformat_prompt_carries_the_current_ticket(monkeypatch):
    _mock_clickup(monkeypatch, name="TAB-07 | exportar", description="cuerpo actual del ticket")
    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return Result(stdout=VALID)

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    reformat_ticket("T1", tracker="clickup", timeout_s=5)

    assert "TAB-07 | exportar" in captured["prompt"]
    assert "cuerpo actual del ticket" in captured["prompt"]
    assert "do NOT invent new requirements" in captured["prompt"]
    assert "Preserve any identifier or prefix" in captured["prompt"]


def test_reformat_handles_a_ticket_with_no_description(monkeypatch):
    _mock_clickup(monkeypatch, description="")
    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return Result(stdout=VALID)

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    reformat_ticket("T1", tracker="clickup", timeout_s=5)

    assert "(this ticket has no description)" in captured["prompt"]


def test_reformat_retries_on_bad_json(monkeypatch):
    _mock_clickup(monkeypatch)
    calls = []

    def fake_run(command, input, capture_output, text, timeout):
        calls.append(command)
        return Result(stdout="not json" if len(calls) == 1 else VALID)

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    result = reformat_ticket("T1", tracker="clickup", timeout_s=5)

    assert len(calls) == 2
    assert result.formatted_title == "TAB-07 | Endpoint de exportación"


def test_reformat_gives_up_after_max_attempts(monkeypatch):
    _mock_clickup(monkeypatch)
    _mock_claude(monkeypatch, stdout="still not json")

    with pytest.raises(TicketParseError):
        reformat_ticket("T1", tracker="clickup", timeout_s=5, max_attempts=2)


def test_reformat_rejects_unknown_tracker():
    with pytest.raises(ValueError, match="Invalid tracker"):
        reformat_ticket("T1", tracker="jira")


def test_reformat_reports_progress(monkeypatch):
    _mock_linear(monkeypatch)
    _mock_claude(monkeypatch)
    steps = []

    reformat_ticket("I1", tracker="linear", timeout_s=5, on_step=steps.append)

    assert steps[0] == "Fetching ticket I1 from Linear…"
    assert "Restructuring the ticket into the standard format…" in steps


# ---------------------------------------------------------------------------
# apply_reformatted_ticket — the confirm step
# ---------------------------------------------------------------------------


def test_apply_updates_the_clickup_task(monkeypatch):
    captured = {}

    def fake_update(task_id, **kw):
        captured.update(kw, task_id=task_id)
        return {"id": task_id, "name": kw.get("name")}

    monkeypatch.setattr("meta_harness.ticket_reformat.update_clickup_task", fake_update)

    apply_reformatted_ticket("T1", tracker="clickup", title="Nuevo", description="Cuerpo")

    assert captured["task_id"] == "T1"
    assert captured["name"] == "Nuevo"
    assert captured["description"] == "Cuerpo"


def test_apply_updates_the_linear_issue(monkeypatch):
    captured = {}

    def fake_update(issue_id, **kw):
        captured.update(kw, issue_id=issue_id)
        return {"id": issue_id, "title": kw.get("title")}

    monkeypatch.setattr("meta_harness.ticket_reformat.update_linear_issue", fake_update)

    def boom(*a, **kw):
        raise AssertionError("must not touch ClickUp when tracker is linear")

    monkeypatch.setattr("meta_harness.ticket_reformat.update_clickup_task", boom)

    apply_reformatted_ticket("I1", tracker="linear", title="Nuevo", description="Cuerpo")

    assert captured["issue_id"] == "I1"
    assert captured["title"] == "Nuevo"


def test_apply_requires_something_to_change():
    with pytest.raises(ValueError, match="title and/or a description"):
        apply_reformatted_ticket("T1", tracker="clickup")


def test_apply_rejects_unknown_tracker():
    with pytest.raises(ValueError, match="Invalid tracker"):
        apply_reformatted_ticket("T1", tracker="jira", title="x")


# ---------------------------------------------------------------------------
# Images must survive reformatting — a rewrite that drops a ticket's
# screenshots destroys information the model cannot regenerate.
# ---------------------------------------------------------------------------


IMG_A = "![image.png](https://uploads.linear.app/49f0ee8d/c81589e5/cd31de32-d2e9-4fe0-be40-9ce.png)"
IMG_B = "![captura](https://uploads.linear.app/49f0ee8d/7f540bc7/3d2ec07f-3776-41a0-be93-97d.png)"


def test_extract_visual_resources_finds_markdown_images():
    from meta_harness.ticket_format import extract_visual_resources

    found = extract_visual_resources(f"texto\n\n{IMG_A}\n\nmas texto\n\n{IMG_B}")

    assert found == [IMG_A, IMG_B]


def test_extract_visual_resources_does_not_double_count_an_image():
    # The attachment-link pattern also matches the inner half of a markdown
    # image unless guarded -- that reported every image twice.
    from meta_harness.ticket_format import extract_visual_resources

    assert extract_visual_resources(IMG_A) == [IMG_A]


def test_extract_visual_resources_finds_html_and_attachments():
    from meta_harness.ticket_format import extract_visual_resources

    body = '<img src="https://x.test/a.png" alt="a"> y [adjunto.pdf](https://x.test/uploads/adjunto.pdf)'

    found = extract_visual_resources(body)

    assert any("<img" in f for f in found)
    assert any("adjunto.pdf" in f for f in found)


def test_extract_visual_resources_empty_for_a_plain_ticket():
    from meta_harness.ticket_format import extract_visual_resources

    assert extract_visual_resources("solo texto, sin imagenes") == []
    assert extract_visual_resources("") == []


def test_reformat_carries_the_original_images_across(monkeypatch):
    _mock_linear(monkeypatch, description=f"Contexto del ticket\n\n{IMG_A}\n\n{IMG_B}")
    _mock_claude(monkeypatch)

    result = reformat_ticket("I1", tracker="linear", timeout_s=5)

    assert result.ticket.visual_resources == [IMG_A, IMG_B]
    # and they land in the rendered body, not just the data
    assert IMG_A in result.formatted_description
    assert IMG_B in result.formatted_description


def test_reformat_keeps_the_placeholder_when_there_were_no_images(monkeypatch):
    _mock_linear(monkeypatch, description="un ticket sin imagenes")
    _mock_claude(monkeypatch)

    result = reformat_ticket("I1", tracker="linear", timeout_s=5)

    assert result.ticket.visual_resources == []
    assert "[Link a Figma]" in result.formatted_description


def test_carried_images_replace_the_placeholder_not_append_to_it(monkeypatch):
    # The placeholder is a prompt to add images; showing it alongside real
    # ones reads as though the real ones are missing.
    _mock_clickup(monkeypatch, description=f"texto\n\n{IMG_A}")
    _mock_claude(monkeypatch)

    result = reformat_ticket("T1", tracker="clickup", timeout_s=5)

    assert IMG_A in result.formatted_description
    assert "No se proporcionaron recursos visuales" not in result.formatted_description
