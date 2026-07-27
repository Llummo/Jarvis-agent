import json

import pytest

from meta_harness.module_relevance import (
    AnalysisGenerationError,
    AnalysisParseError,
    ClaudeNotFoundError,
    analyze_module_relevance,
)


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


VALID = json.dumps(
    {
        "verdict": "related",
        "confidence": 0.9,
        "rationale": "El ticket modifica la ficha de personas descrita en la documentación.",
        "matched_aspects": ["Ficha de persona", "Permisos talent:write"],
        "module_gaps": [],
    }
)


def _mock_clickup(monkeypatch, name="Editar ficha de persona", description="Permitir editar el perfil."):
    monkeypatch.setattr(
        "meta_harness.module_relevance.get_clickup_task",
        lambda ticket_id, **kw: {"id": ticket_id, "name": name, "text_content": description},
    )


def _mock_linear(monkeypatch, title="Editar ficha de persona", description="Permitir editar el perfil."):
    monkeypatch.setattr(
        "meta_harness.module_relevance.get_linear_issue",
        lambda issue_id, **kw: {"id": issue_id, "title": title, "description": description},
    )


def _mock_claude(monkeypatch, stdout=VALID):
    monkeypatch.setattr("meta_harness.module_relevance.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.module_relevance.subprocess.run", lambda *a, **k: Result(stdout=stdout))


def test_analyze_returns_verdict_and_evidence(monkeypatch):
    _mock_clickup(monkeypatch)
    _mock_claude(monkeypatch)

    result = analyze_module_relevance(
        "T1", tracker="clickup", module_name="Personas", module_context="El módulo de personas...", timeout_s=5
    )

    assert result.verdict == "related"
    assert result.is_related is True
    assert result.confidence == 0.9
    assert result.matched_aspects == ["Ficha de persona", "Permisos talent:write"]
    assert result.ticket_name == "Editar ficha de persona"
    assert result.module_name == "Personas"


def test_analyze_reads_linear_issues_when_tracker_is_linear(monkeypatch):
    _mock_linear(monkeypatch, title="Issue desde Linear")
    _mock_claude(monkeypatch)

    def boom(*a, **kw):
        raise AssertionError("must not read from ClickUp when tracker is linear")

    monkeypatch.setattr("meta_harness.module_relevance.get_clickup_task", boom)

    result = analyze_module_relevance(
        "I1", tracker="linear", module_name="Personas", module_context="doc", timeout_s=5
    )

    assert result.ticket_name == "Issue desde Linear"


def test_analyze_includes_module_docs_and_ticket_in_the_prompt(monkeypatch):
    _mock_clickup(monkeypatch, name="Editar ficha", description="Cuerpo del ticket")
    captured = {}

    def fake_run(command, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return Result(stdout=VALID)

    monkeypatch.setattr("meta_harness.module_relevance.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.module_relevance.subprocess.run", fake_run)

    analyze_module_relevance(
        "T1", tracker="clickup", module_name="Personas",
        module_context="DOCUMENTACION OFICIAL DEL MODULO", timeout_s=5,
    )

    assert "DOCUMENTACION OFICIAL DEL MODULO" in captured["prompt"]
    assert "Editar ficha" in captured["prompt"]
    assert "Cuerpo del ticket" in captured["prompt"]


def test_analyze_rejects_unrelated_verdict_shapes(monkeypatch):
    _mock_clickup(monkeypatch)
    _mock_claude(monkeypatch, stdout=json.dumps({"verdict": "maybe", "rationale": "x"}))

    with pytest.raises(AnalysisParseError, match="invalid verdict"):
        analyze_module_relevance(
            "T1", tracker="clickup", module_name="Personas", module_context="doc",
            timeout_s=5, max_attempts=1,
        )


def test_analyze_retries_and_recovers_from_bad_json(monkeypatch):
    _mock_clickup(monkeypatch)
    calls = []

    def fake_run(command, capture_output, text, timeout):
        calls.append(command)
        return Result(stdout="not json" if len(calls) == 1 else VALID)

    monkeypatch.setattr("meta_harness.module_relevance.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.module_relevance.subprocess.run", fake_run)

    result = analyze_module_relevance(
        "T1", tracker="clickup", module_name="Personas", module_context="doc", timeout_s=5
    )

    assert len(calls) == 2
    assert result.verdict == "related"


def test_analyze_clamps_out_of_range_confidence(monkeypatch):
    _mock_clickup(monkeypatch)
    _mock_claude(monkeypatch, stdout=json.dumps({"verdict": "unrelated", "confidence": 7, "rationale": "x"}))

    result = analyze_module_relevance(
        "T1", tracker="clickup", module_name="Personas", module_context="doc", timeout_s=5
    )

    assert result.confidence == 1.0
    assert result.is_related is False


def test_analyze_requires_module_context():
    with pytest.raises(ValueError, match="module_context"):
        analyze_module_relevance("T1", module_name="Personas", module_context="   ")


def test_analyze_requires_module_name():
    with pytest.raises(ValueError, match="module_name"):
        analyze_module_relevance("T1", module_name="  ", module_context="doc")


def test_analyze_rejects_unknown_tracker(monkeypatch):
    with pytest.raises(ValueError, match="Invalid tracker"):
        analyze_module_relevance("T1", tracker="jira", module_name="Personas", module_context="doc")


def test_analyze_raises_when_claude_missing(monkeypatch):
    _mock_clickup(monkeypatch)
    monkeypatch.delenv("META_HARNESS_CLAUDE_PATH", raising=False)
    monkeypatch.setattr("meta_harness.module_relevance.shutil.which", lambda name: None)

    with pytest.raises(ClaudeNotFoundError):
        analyze_module_relevance("T1", module_name="Personas", module_context="doc")


def test_analyze_raises_on_claude_failure(monkeypatch):
    _mock_clickup(monkeypatch)
    monkeypatch.setattr("meta_harness.module_relevance.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.module_relevance.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="boom"),
    )

    with pytest.raises(AnalysisGenerationError, match="boom"):
        analyze_module_relevance("T1", module_name="Personas", module_context="doc", timeout_s=5)


def test_analyze_reports_progress_steps(monkeypatch):
    _mock_clickup(monkeypatch)
    _mock_claude(monkeypatch)
    steps = []

    analyze_module_relevance(
        "T1", tracker="clickup", module_name="Personas", module_context="doc",
        timeout_s=5, on_step=steps.append,
    )

    assert steps[0] == "Fetching ticket T1 from ClickUp…"
    assert any("Personas" in step for step in steps)
    assert "Verdict: related" in steps[-1]


# ---------------------------------------------------------------------------
# analyze_modules_bulk / sort_by_relevance / render_module_report_markdown
# ---------------------------------------------------------------------------


def _relevance(ticket_id, verdict, confidence=0.8, name=None):
    from meta_harness.module_relevance import ModuleRelevance

    return ModuleRelevance(
        ticket_id=ticket_id, ticket_name=name or f"Ticket {ticket_id}", module_name="Personas",
        verdict=verdict, confidence=confidence, rationale="Motivo.",
        matched_aspects=["Ficha"] if verdict != "unrelated" else [], module_gaps=[],
    )


def test_bulk_analyzes_every_ticket(monkeypatch):
    from meta_harness.module_relevance import analyze_modules_bulk

    monkeypatch.setattr(
        "meta_harness.module_relevance.analyze_module_relevance",
        lambda ticket_id, **kw: _relevance(ticket_id, "related"),
    )

    results = analyze_modules_bulk(
        ["T1", "T2", "T3"], tracker="clickup", module_name="Personas", module_context="doc"
    )

    assert [tid for tid, _rel, _err in results] == ["T1", "T2", "T3"]
    assert all(rel and not err for _tid, rel, err in results)


def test_bulk_isolates_per_ticket_failures(monkeypatch):
    from meta_harness.module_relevance import AnalysisParseError, analyze_modules_bulk

    def fake(ticket_id, **kw):
        if ticket_id == "BAD":
            raise AnalysisParseError("unusable response")
        return _relevance(ticket_id, "related")

    monkeypatch.setattr("meta_harness.module_relevance.analyze_module_relevance", fake)

    results = analyze_modules_bulk(["T1", "BAD", "T2"], module_name="Personas", module_context="doc")

    assert results[0][1] is not None and results[0][2] is None
    assert results[1][1] is None and "unusable response" in results[1][2]
    assert results[2][1] is not None


def test_bulk_reports_progress_and_the_aligned_count(monkeypatch):
    from meta_harness.module_relevance import analyze_modules_bulk

    verdicts = {"T1": "related", "T2": "unrelated"}
    monkeypatch.setattr(
        "meta_harness.module_relevance.analyze_module_relevance",
        lambda ticket_id, **kw: _relevance(ticket_id, verdicts[ticket_id]),
    )

    steps = []
    analyze_modules_bulk(["T1", "T2"], module_name="Personas", module_context="doc", on_step=steps.append)

    assert "Checking 1/2" in steps[0]
    assert "1 of 2 ticket(s) align" in steps[-1]


def test_sort_by_relevance_puts_aligned_tickets_first():
    from meta_harness.module_relevance import sort_by_relevance

    results = [
        ("T1", _relevance("T1", "unrelated", 0.9), None),
        ("T2", None, "boom"),
        ("T3", _relevance("T3", "partially_related", 0.7), None),
        ("T4", _relevance("T4", "related", 0.6), None),
        ("T5", _relevance("T5", "related", 0.95), None),
    ]

    assert [tid for tid, _rel, _err in sort_by_relevance(results)] == ["T5", "T4", "T3", "T1", "T2"]


def test_report_leads_with_the_tickets_that_align():
    from meta_harness.module_relevance import render_module_report_markdown

    results = [
        ("T1", _relevance("T1", "unrelated", 0.9, name="Reportes de asistencia"), None),
        ("T2", _relevance("T2", "related", 0.95, name="Editar ficha de persona"), None),
        ("T3", None, "no se pudo leer"),
    ]

    report = render_module_report_markdown("Personas", results)

    assert "# Módulo: Personas" in report
    assert "- **Pertenecen al módulo:** 1" in report
    assert "- **No pertenecen:** 1" in report
    assert "- **No se pudieron analizar:** 1" in report
    aligned_section = report.split("## Tickets que se alinean con el módulo")[1].split("## Detalle")[0]
    assert "Editar ficha de persona" in aligned_section
    assert "Reportes de asistencia" not in aligned_section
    assert "no se pudo leer" in report


def test_report_says_so_when_nothing_aligns():
    from meta_harness.module_relevance import render_module_report_markdown

    report = render_module_report_markdown("Personas", [("T1", _relevance("T1", "unrelated"), None)])

    assert "Ningún ticket analizado pertenece a este módulo" in report
