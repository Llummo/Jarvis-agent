from meta_harness.qa_findings import QAFinding
from meta_harness.qa_flow import QATicketReview
from meta_harness.qa_report import (
    render_bulk_readme_markdown,
    render_finding_markdown,
    render_finding_pdf,
    render_review_markdown,
)


def _finding(**overrides):
    defaults = dict(
        id=1, project="sigo-front", route="Fix login bug", observation="Check login works",
        severity="major", status="open", created_at="2026-07-27T00:00:00+00:00",
        updated_at="2026-07-27T00:00:00+00:00",
    )
    defaults.update(overrides)
    return QAFinding(**defaults)


def test_render_finding_markdown_includes_core_fields():
    md = render_finding_markdown(_finding())

    assert "QA Finding #1" in md
    assert "sigo-front" in md
    assert "Check login works" in md
    assert "major" in md
    assert "FAIL" in md  # major severity does not count as passing


def test_render_finding_markdown_marks_minor_as_pass():
    md = render_finding_markdown(_finding(severity="minor"))

    assert "PASS" in md


def test_render_finding_markdown_includes_route_check_evidence():
    md = render_finding_markdown(
        _finding(checked_route="/login", status_code=500, http_error="HTTP 500: Internal Server Error")
    )

    assert "/login" in md
    assert "500" in md
    assert "Internal Server Error" in md


def test_render_finding_markdown_includes_screenshot_link():
    md = render_finding_markdown(_finding(screenshot_path="qa/screenshots/shot.png"))

    assert "qa/screenshots/shot.png" in md


def test_render_finding_markdown_omits_optional_sections_when_absent():
    md = render_finding_markdown(_finding())

    assert "Screenshot evidence" not in md
    assert "Correction note" not in md


def test_render_finding_pdf_produces_real_pdf_bytes():
    pdf_bytes = render_finding_pdf(_finding())

    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 100


def test_render_finding_pdf_handles_missing_screenshot_file_gracefully(tmp_path):
    pdf_bytes = render_finding_pdf(_finding(screenshot_path=str(tmp_path / "does-not-exist.png")))

    assert pdf_bytes.startswith(b"%PDF")


def _review(**overrides):
    defaults = dict(ticket_id="T1", ticket_name="Fix login bug", observation="Check login", severity="major")
    defaults.update(overrides)
    return QATicketReview(**defaults)


def test_render_bulk_readme_markdown_summarizes_pass_and_fail_counts():
    results = [
        ("T1", _review(severity="minor"), None),
        ("T2", _review(severity="critical"), None),
        ("T3", None, "ticket not found"),
    ]

    md = render_bulk_readme_markdown("sigo-front", results)

    assert "Tickets analyzed:** 3" in md
    assert "QA passed:** 1" in md
    assert "QA failed:** 1" in md
    assert "T3" in md
    assert "ticket not found" in md


def test_render_bulk_readme_markdown_includes_proposed_status_moves():
    results = [("T1", _review(severity="minor"), None), ("T2", _review(severity="critical"), None)]

    md = render_bulk_readme_markdown("sigo-front", results, pass_status="done", fail_status="blocked")

    assert "Proposed status move:** done" in md
    assert "Proposed status move:** blocked" in md


def test_render_bulk_readme_markdown_includes_route_and_screenshot_evidence():
    results = [
        ("T1", _review(route="/login", status_code=200, screenshot_path="qa/screenshots/shot.png"), None),
    ]

    md = render_bulk_readme_markdown("sigo-front", results)

    assert "/login" in md
    assert "200" in md
    assert "qa/screenshots/shot.png" in md


def test_render_review_markdown_includes_core_fields():
    md = render_review_markdown(_review(severity="critical"))

    assert "QA Review: Fix login bug" in md
    assert "T1" in md
    assert "critical" in md
    assert "FAIL" in md
    assert "Check login" in md


def test_render_review_markdown_includes_route_check_evidence():
    md = render_review_markdown(
        _review(route="/login", status_code=500, http_error="HTTP 500: Internal Server Error",
                screenshot_path="qa/screenshots/shot.png")
    )

    assert "/login" in md
    assert "500" in md
    assert "Internal Server Error" in md
    assert "qa/screenshots/shot.png" in md
