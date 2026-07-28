"""Render QA findings as downloadable reports: a single finding as
Markdown/PDF, or a whole bulk-QA sweep as one QA-README.md.

Pure rendering — takes already-computed data (a QAFinding, or a batch of
(ticket_name, review, error) tuples) and produces text/bytes. No I/O to
ClickUp or Claude here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from meta_harness.qa_findings import QAFinding
from meta_harness.qa_flow import QATicketReview, review_passed


def _outcome_label(severity: str) -> str:
    return "PASS" if review_passed(severity) else "FAIL"


def _line(pdf: FPDF, height: float, text: str) -> None:
    # multi_cell with w=0 leaves the cursor at the end of the last line by
    # default, so a second call in the same loop sees ~0 width left and
    # raises — new_x/new_y explicitly reset it back to the left margin.
    pdf.multi_cell(0, height, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def render_finding_markdown(finding: QAFinding) -> str:
    """Render one persisted finding as a standalone Markdown report."""
    lines = [
        f"# QA Finding #{finding.id}: {finding.route}",
        "",
        f"- **Project:** {finding.project}",
        f"- **Severity:** {finding.severity} ({_outcome_label(finding.severity)})",
        f"- **Status:** {finding.status}",
        f"- **Reported:** {finding.created_at}",
    ]
    if finding.checked_route:
        lines.append(f"- **Route checked:** {finding.checked_route}")
    if finding.status_code is not None:
        lines.append(f"- **HTTP status:** {finding.status_code}")
    if finding.http_error:
        lines.append(f"- **Error:** {finding.http_error}")
    if finding.clickup_task_id:
        lines.append(f"- **ClickUp task:** {finding.clickup_task_id}")

    lines += ["", "## Observation", "", finding.observation]

    if finding.screenshot_path:
        lines += ["", "## Screenshot evidence", "", f"![screenshot]({finding.screenshot_path})"]

    if finding.correction_note:
        lines += ["", "## Correction note", "", finding.correction_note]

    return "\n".join(lines) + "\n"


def render_review_markdown(review: QATicketReview) -> str:
    """Render a not-yet-persisted review (dry-run) as a standalone Markdown
    report — same shape as render_finding_markdown, minus the fields that
    only exist once something is actually saved (id, status, ClickUp task)."""
    lines = [
        f"# QA Review: {review.ticket_name}",
        "",
        f"- **Ticket ID:** {review.ticket_id}",
        f"- **Severity:** {review.severity} ({_outcome_label(review.severity)})",
    ]
    if review.route:
        lines.append(f"- **Route checked:** {review.route}")
    if review.status_code is not None:
        lines.append(f"- **HTTP status:** {review.status_code}")
    if review.http_error:
        lines.append(f"- **Error:** {review.http_error}")

    lines += ["", "## Observation", "", review.observation]

    if review.screenshot_path:
        lines += ["", "## Screenshot evidence", "", f"![screenshot]({review.screenshot_path})"]

    return "\n".join(lines) + "\n"


def render_finding_pdf(finding: QAFinding) -> bytes:
    """Render one persisted finding as a simple PDF report (metadata,
    observation, and the embedded screenshot if one was captured)."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("helvetica", "B", 16)
    _line(pdf, 10, f"QA Finding #{finding.id}: {finding.route}")
    pdf.ln(2)

    pdf.set_font("helvetica", "", 11)
    meta_lines = [
        f"Project: {finding.project}",
        f"Severity: {finding.severity} ({_outcome_label(finding.severity)})",
        f"Status: {finding.status}",
        f"Reported: {finding.created_at}",
    ]
    if finding.checked_route:
        meta_lines.append(f"Route checked: {finding.checked_route}")
    if finding.status_code is not None:
        meta_lines.append(f"HTTP status: {finding.status_code}")
    if finding.http_error:
        meta_lines.append(f"Error: {finding.http_error}")
    if finding.clickup_task_id:
        meta_lines.append(f"ClickUp task: {finding.clickup_task_id}")
    for meta_line in meta_lines:
        _line(pdf, 7, meta_line)
    pdf.ln(4)

    pdf.set_font("helvetica", "B", 13)
    _line(pdf, 8, "Observation")
    pdf.set_font("helvetica", "", 11)
    _line(pdf, 7, finding.observation)
    pdf.ln(4)

    if finding.correction_note:
        pdf.set_font("helvetica", "B", 13)
        _line(pdf, 8, "Correction note")
        pdf.set_font("helvetica", "", 11)
        _line(pdf, 7, finding.correction_note)
        pdf.ln(4)

    if finding.screenshot_path and Path(finding.screenshot_path).exists():
        pdf.set_font("helvetica", "B", 13)
        _line(pdf, 8, "Screenshot evidence")
        pdf.image(finding.screenshot_path, w=170)

    return bytes(pdf.output())


def render_bulk_readme_markdown(
    project: str,
    results: List[Tuple[str, Optional[QATicketReview], Optional[str]]],
    *,
    pass_status: Optional[str] = None,
    fail_status: Optional[str] = None,
) -> str:
    """Render a whole bulk-QA sweep as one QA-README.md: performed checks,
    routes, status codes, screenshot evidence, and the proposed status move
    per ticket — the same structured data that /bulk/commit acts on, so the
    README and the "move tickets" action are always in sync.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    ok = sum(1 for _tid, review, error in results if review and not error)
    failed_to_analyze = len(results) - ok
    passed = sum(1 for _tid, review, error in results if review and not error and review_passed(review.severity))
    failed_qa = ok - passed

    lines = [
        "# QA-README",
        "",
        f"- **Project:** {project}",
        f"- **Generated:** {generated_at}",
        f"- **Tickets analyzed:** {len(results)} ({ok} succeeded, {failed_to_analyze} failed to analyze)",
        f"- **QA passed:** {passed}",
        f"- **QA failed:** {failed_qa}",
        f'- **On pass, move to:** {pass_status or "(leave as-is)"}',
        f'- **On fail, move to:** {fail_status or "(leave as-is)"}',
        "",
        "## Results",
        "",
    ]

    for ticket_id, review, error in results:
        if error or review is None:
            lines += [f"### {ticket_id} — ERROR", "", f"Could not analyze: {error}", ""]
            continue

        outcome = _outcome_label(review.severity)
        target_status = pass_status if review_passed(review.severity) else fail_status
        lines += [
            f"### {review.ticket_name} — {outcome} ({review.severity})",
            "",
            f"- **Ticket ID:** {ticket_id}",
        ]
        if review.route:
            lines.append(f"- **Route checked:** {review.route}")
        if review.status_code is not None:
            lines.append(f"- **HTTP status:** {review.status_code}")
        if review.http_error:
            lines.append(f"- **Error:** {review.http_error}")
        lines.append(f"- **Proposed status move:** {target_status or '(leave as-is)'}")
        lines += ["", "**Observation:**", "", review.observation]
        if review.screenshot_path:
            lines += ["", f"![screenshot]({review.screenshot_path})"]
        lines.append("")

    return "\n".join(lines) + "\n"
