import json

import pytest

from meta_harness.ticket_generator import (
    ClaudeNotFoundError,
    TicketExtractionError,
    TicketGenerationError,
    TicketParseError,
    extract_document_text,
    generate_tickets_from_text,
    parse_proposed_tickets,
)

MINIMAL_TEST_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 200]/Contents 5 0 R>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 55>>stream
BT /F1 12 Tf 20 100 Td (Hello test PDF) Tj ET
endstream
endobj
xref
0 6
0000000000 65535 f
trailer<</Size 6/Root 1 0 R>>
startxref
0
%%EOF"""


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# extract_document_text — real, non-mocked PDF parsing
# ---------------------------------------------------------------------------


def test_extract_document_text_real_pdf():
    text = extract_document_text("spec.pdf", MINIMAL_TEST_PDF)

    assert "Hello test PDF" in text


def test_extract_document_text_txt():
    text = extract_document_text("spec.txt", "Build a login page.".encode("utf-8"))

    assert text == "Build a login page."


def test_extract_document_text_md():
    text = extract_document_text("spec.md", b"# Requirements\n\nDo the thing.")

    assert "Do the thing." in text


def test_extract_document_text_unsupported_extension():
    with pytest.raises(TicketExtractionError, match="Unsupported file type"):
        extract_document_text("spec.docx", b"whatever")


def test_extract_document_text_garbage_pdf_bytes():
    with pytest.raises(TicketExtractionError, match="Could not parse PDF"):
        extract_document_text("spec.pdf", b"not a pdf at all, just junk bytes")


def test_extract_document_text_invalid_utf8_txt():
    with pytest.raises(TicketExtractionError, match="Could not decode"):
        extract_document_text("spec.txt", b"\xff\xfe\x00bad")


# ---------------------------------------------------------------------------
# parse_proposed_tickets — JSON-shape validation
# ---------------------------------------------------------------------------


def _valid_ticket(**overrides):
    ticket = {
        "title": "Build login page",
        "description": "Add a login form.",
        "acceptance_criteria": ["Has email field", "Has password field"],
        "priority": "high",
    }
    ticket.update(overrides)
    return ticket


def test_parse_proposed_tickets_happy_path():
    raw = json.dumps([_valid_ticket()])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert tickets[0].title == "Build login page"
    assert tickets[0].priority == "high"
    assert warnings == []


def test_parse_proposed_tickets_strips_code_fences():
    raw = "```json\n" + json.dumps([_valid_ticket()]) + "\n```"

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert warnings == []


def test_parse_proposed_tickets_not_json_raises():
    with pytest.raises(TicketParseError, match="did not return valid JSON"):
        parse_proposed_tickets("not json at all")


def test_parse_proposed_tickets_object_not_array_raises():
    with pytest.raises(TicketParseError, match="Expected a JSON array"):
        parse_proposed_tickets(json.dumps({"tickets": [_valid_ticket()]}))


def test_parse_proposed_tickets_missing_title_drops_ticket():
    raw = json.dumps([_valid_ticket(), {**_valid_ticket(), "title": ""}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert any("title" in w for w in warnings)


def test_parse_proposed_tickets_missing_acceptance_criteria_keeps_ticket():
    raw = json.dumps([{**_valid_ticket(), "acceptance_criteria": None}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert tickets[0].acceptance_criteria == []
    assert any("acceptance_criteria" in w for w in warnings)


def test_parse_proposed_tickets_invalid_priority_defaults_to_normal():
    raw = json.dumps([{**_valid_ticket(), "priority": "urgentish"}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert tickets[0].priority == "normal"
    assert any("priority" in w for w in warnings)


def test_parse_proposed_tickets_all_dropped_raises():
    raw = json.dumps([{**_valid_ticket(), "title": ""}, {**_valid_ticket(), "title": None}])

    with pytest.raises(TicketParseError, match="No usable tickets"):
        parse_proposed_tickets(raw)


def test_parse_proposed_tickets_non_object_item_dropped():
    raw = json.dumps(["just a string", _valid_ticket()])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert any("not a JSON object" in w for w in warnings)


# ---------------------------------------------------------------------------
# generate_tickets_from_text — subprocess invocation
# ---------------------------------------------------------------------------


def test_generate_tickets_from_text_pipes_document_via_stdin(monkeypatch):
    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["command"] = command
        captured["input"] = input
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    tickets, warnings = generate_tickets_from_text("Build a login page.", timeout_s=5)

    assert tickets[0].title == "Build login page"
    assert captured["input"] == "Build a login page."
    assert captured["command"][0] == "/usr/bin/claude"
    assert "-p" in captured["command"]


def test_generate_tickets_from_text_raises_when_claude_not_found(monkeypatch):
    monkeypatch.delenv("META_HARNESS_CLAUDE_PATH", raising=False)
    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: None)

    with pytest.raises(ClaudeNotFoundError, match="No `claude` CLI binary found"):
        generate_tickets_from_text("some text")


def test_generate_tickets_from_text_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.ticket_generator.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr="claude exploded"),
    )

    with pytest.raises(TicketGenerationError, match="claude exploded"):
        generate_tickets_from_text("some text", timeout_s=5)


def test_generate_tickets_from_text_raises_on_timeout(monkeypatch):
    import subprocess as subprocess_module

    def fake_run(*a, **k):
        raise subprocess_module.TimeoutExpired(cmd="claude", timeout=5)

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    with pytest.raises(TicketGenerationError, match="timed out"):
        generate_tickets_from_text("some text", timeout_s=5)


# ---------------------------------------------------------------------------
# generate_tickets_from_text — retry-with-repair harness
# ---------------------------------------------------------------------------


def test_generate_tickets_retries_and_recovers_from_bad_json(monkeypatch):
    calls = []

    def fake_run(command, input, capture_output, text, timeout):
        calls.append(command)
        if len(calls) == 1:
            return Result(stdout="not valid json")
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    tickets, warnings = generate_tickets_from_text("Build a login page.", timeout_s=5)

    assert len(calls) == 2
    assert tickets[0].title == "Build login page"
    # the retry prompt must reference the specific failure so Claude can self-correct
    second_prompt = calls[1][2]
    assert "previous response was invalid" in second_prompt
    assert "did not return valid JSON" in second_prompt


def test_generate_tickets_gives_up_after_max_attempts(monkeypatch):
    calls = []

    def fake_run(command, input, capture_output, text, timeout):
        calls.append(command)
        return Result(stdout="still not valid json")

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    with pytest.raises(TicketParseError, match="did not return valid JSON"):
        generate_tickets_from_text("some text", timeout_s=5, max_attempts=3)

    assert len(calls) == 3


def test_generate_tickets_succeeds_first_try_does_not_retry(monkeypatch):
    calls = []

    def fake_run(command, input, capture_output, text, timeout):
        calls.append(command)
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    generate_tickets_from_text("some text", timeout_s=5)

    assert len(calls) == 1
