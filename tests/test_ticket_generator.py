import json

import pytest

from meta_harness.ticket_generator import (
    TICKET_EXTRACTION_PROMPT,
    ClaudeNotFoundError,
    ProposedTicket,
    TicketExtractionError,
    TicketGenerationError,
    TicketParseError,
    apply_category_numbering,
    extract_document_text,
    generate_tickets_from_file,
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
        "user_story": "As a user, I want to log in, so I can access my account.",
        "description": "Add a login form.",
        "acceptance_criteria": ["Given a valid email and password, when the user submits the form, then they are logged in."],
        "priority": "high",
        "category": "backend",
    }
    ticket.update(overrides)
    return ticket


def test_parse_proposed_tickets_happy_path():
    raw = json.dumps([_valid_ticket()])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert tickets[0].title == "Build login page"
    assert tickets[0].user_story == "As a user, I want to log in, so I can access my account."
    assert tickets[0].priority == "high"
    assert tickets[0].category == "backend"
    assert warnings == []


def test_parse_proposed_tickets_missing_user_story_keeps_ticket_with_warning():
    raw = json.dumps([{**_valid_ticket(), "user_story": None}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert tickets[0].user_story == ""
    assert any("user_story" in w for w in warnings)


def test_parse_proposed_tickets_empty_user_story_keeps_ticket_with_warning():
    raw = json.dumps([{**_valid_ticket(), "user_story": "   "}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert tickets[0].user_story == ""
    assert any("user_story" in w for w in warnings)


def test_parse_proposed_tickets_invalid_category_defaults_to_mundane():
    raw = json.dumps([{**_valid_ticket(), "category": "database-admin"}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert len(tickets) == 1
    assert tickets[0].category == "mundane"
    assert any("category" in w for w in warnings)


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
# TICKET_EXTRACTION_PROMPT — locks in the required content shape so this
# can't silently regress (it did once already, per direct user feedback).
# ---------------------------------------------------------------------------


def test_prompt_requires_english_user_story_template():
    assert "user_story" in TICKET_EXTRACTION_PROMPT
    assert "As a <role>, I want to <goal>, so I can <benefit>" in TICKET_EXTRACTION_PROMPT


def test_prompt_requires_gherkin_acceptance_criteria():
    assert "Gherkin" in TICKET_EXTRACTION_PROMPT
    assert "Given <context>, when <action>, then <expected outcome>" in TICKET_EXTRACTION_PROMPT


def test_prompt_requires_a_brief_description_distinct_from_user_story():
    assert "description" in TICKET_EXTRACTION_PROMPT
    assert "distinct from" in TICKET_EXTRACTION_PROMPT


def test_prompt_requires_logical_task_ordering():
    assert "logical implementation sequence" in TICKET_EXTRACTION_PROMPT


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


def test_generate_tickets_from_text_reports_steps_via_on_step(monkeypatch):
    calls = []

    def fake_run(command, input, capture_output, text, timeout):
        calls.append(command)
        if len(calls) == 1:
            return Result(stdout="not valid json")
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    steps = []
    generate_tickets_from_text("Build a login page.", timeout_s=5, on_step=steps.append)

    assert steps[0] == "Sending document to Claude for ticket extraction — this can take a minute…"
    assert "attempt 2/3" in steps[1]
    assert steps[-1] == "Extracted 1 ticket(s) from the document."


def test_generate_tickets_from_file_reports_extraction_step(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.ticket_generator.subprocess.run",
        lambda *a, **k: Result(stdout=json.dumps([_valid_ticket()])),
    )

    steps = []
    generate_tickets_from_file("spec.txt", b"Build a login page.", timeout_s=5, on_step=steps.append)

    assert steps[0] == 'Extracting text from "spec.txt"…'


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


# ---------------------------------------------------------------------------
# apply_category_numbering
# ---------------------------------------------------------------------------


def _ticket(title, category="mundane"):
    return ProposedTicket(title=title, description="d", acceptance_criteria=[], priority="normal", category=category)


def test_apply_category_numbering_single_category_default_start():
    tickets = [_ticket("Do thing one", "mundane"), _ticket("Do thing two", "mundane")]

    numbered = apply_category_numbering(tickets)

    assert numbered[0].title == "TAM-01 | Do thing one"
    assert numbered[1].title == "TAM-02 | Do thing two"


def test_apply_category_numbering_uses_correct_prefix_per_category():
    tickets = [
        _ticket("General planning", "mundane"),
        _ticket("Build API endpoint", "backend"),
        _ticket("Build login page", "frontend"),
        _ticket("Set up CI pipeline", "deployment"),
    ]

    numbered = apply_category_numbering(tickets)

    assert numbered[0].title == "TAM-01 | General planning"
    assert numbered[1].title == "TAB-01 | Build API endpoint"
    assert numbered[2].title == "TAF-01 | Build login page"
    assert numbered[3].title == "TAD-01 | Set up CI pipeline"


def test_apply_category_numbering_counts_independently_per_category():
    tickets = [
        _ticket("Backend one", "backend"),
        _ticket("Frontend one", "frontend"),
        _ticket("Backend two", "backend"),
        _ticket("Backend three", "backend"),
        _ticket("Frontend two", "frontend"),
    ]

    numbered = apply_category_numbering(tickets)

    assert [t.title for t in numbered] == [
        "TAB-01 | Backend one",
        "TAF-01 | Frontend one",
        "TAB-02 | Backend two",
        "TAB-03 | Backend three",
        "TAF-02 | Frontend two",
    ]


def test_apply_category_numbering_respects_per_category_start_numbers():
    tickets = [_ticket("Existing series continues", "mundane"), _ticket("Fresh backend series", "backend")]

    numbered = apply_category_numbering(tickets, start_numbers={"mundane": 2})

    assert numbered[0].title == "TAM-02 | Existing series continues"
    assert numbered[1].title == "TAB-01 | Fresh backend series"


def test_apply_category_numbering_does_not_mutate_input():
    tickets = [_ticket("Do thing", "mundane")]

    apply_category_numbering(tickets, start_numbers={"mundane": 5})

    assert tickets[0].title == "Do thing"


def test_apply_category_numbering_preserves_other_fields():
    tickets = [ProposedTicket(title="t1", description="desc", acceptance_criteria=["a"], priority="urgent", category="backend")]

    numbered = apply_category_numbering(tickets)

    assert numbered[0].description == "desc"
    assert numbered[0].acceptance_criteria == ["a"]
    assert numbered[0].priority == "urgent"
    assert numbered[0].category == "backend"
