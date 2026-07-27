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
    apply_sprint_due_dates,
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
        "sprint": 2,
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
    assert tickets[0].sprint == 2
    assert warnings == []


def test_parse_proposed_tickets_missing_sprint_defaults_to_1():
    raw = json.dumps([{**_valid_ticket(), "sprint": None}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert tickets[0].sprint == 1
    assert any("sprint" in w for w in warnings)


def test_parse_proposed_tickets_zero_sprint_defaults_to_1():
    raw = json.dumps([{**_valid_ticket(), "sprint": 0}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert tickets[0].sprint == 1
    assert any("sprint" in w for w in warnings)


def test_parse_proposed_tickets_bool_sprint_defaults_to_1():
    # bool is a subclass of int in Python -- must not silently pass as a sprint number.
    raw = json.dumps([{**_valid_ticket(), "sprint": True}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert tickets[0].sprint == 1
    assert any("sprint" in w for w in warnings)


def test_parse_proposed_tickets_string_sprint_defaults_to_1():
    raw = json.dumps([{**_valid_ticket(), "sprint": "2"}])

    tickets, warnings = parse_proposed_tickets(raw)

    assert tickets[0].sprint == 1
    assert any("sprint" in w for w in warnings)


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


def test_prompt_requires_sized_decomposition_not_unbounded_fragmentation():
    assert "half a day to two days" in TICKET_EXTRACTION_PROMPT
    assert "do not pad the list to hit any particular number" in TICKET_EXTRACTION_PROMPT


def test_prompt_caps_total_count_for_large_documents():
    assert "50-80 range" in TICKET_EXTRACTION_PROMPT
    assert "rather than letting the total run unbounded" in TICKET_EXTRACTION_PROMPT


def test_prompt_requires_scrum_sprint_reasoning():
    assert '"sprint"' in TICKET_EXTRACTION_PROMPT
    assert "4-week" in TICKET_EXTRACTION_PROMPT
    assert "never be scheduled in an earlier sprint than a ticket it depends on" in TICKET_EXTRACTION_PROMPT


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


def test_generate_tickets_from_text_includes_deadline_in_prompt_when_given(monkeypatch):
    from datetime import date

    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    generate_tickets_from_text(
        "Build a login page.", timeout_s=5,
        project_start=date(2026, 7, 27), project_end=date(2026, 8, 24),
    )

    assert "2026-08-24" in captured["prompt"]
    assert "2026-07-27" in captured["prompt"]


def test_generate_tickets_from_text_omits_deadline_note_when_not_given(monkeypatch):
    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    generate_tickets_from_text("Build a login page.", timeout_s=5)

    assert "must be complete by" not in captured["prompt"]


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


# ---------------------------------------------------------------------------
# apply_sprint_due_dates
# ---------------------------------------------------------------------------


def test_apply_sprint_due_dates_sprint_1_is_4_weeks_out():
    from datetime import date

    start = date(2026, 1, 1)
    tickets = [ProposedTicket(title="t1", description="d", sprint=1)]

    dated = apply_sprint_due_dates(tickets, sprint_start=start)

    assert dated[0].due_date == "2026-01-29"  # start + 28 days


def test_apply_sprint_due_dates_sprint_2_is_8_weeks_out():
    from datetime import date

    start = date(2026, 1, 1)
    tickets = [ProposedTicket(title="t1", description="d", sprint=2)]

    dated = apply_sprint_due_dates(tickets, sprint_start=start)

    assert dated[0].due_date == "2026-02-26"  # start + 56 days


def test_apply_sprint_due_dates_defaults_to_today(monkeypatch):
    from datetime import date, timedelta

    fake_today = date(2026, 3, 1)

    class FakeDate(date):
        @classmethod
        def today(cls):
            return fake_today

    monkeypatch.setattr("meta_harness.ticket_generator.date", FakeDate)
    tickets = [ProposedTicket(title="t1", description="d", sprint=1)]

    dated = apply_sprint_due_dates(tickets)

    assert dated[0].due_date == (fake_today + timedelta(days=28)).isoformat()


def test_apply_sprint_due_dates_does_not_mutate_input():
    tickets = [ProposedTicket(title="t1", description="d", sprint=1)]

    apply_sprint_due_dates(tickets, sprint_start=date_for_test())

    assert tickets[0].due_date is None


def date_for_test():
    from datetime import date

    return date(2026, 1, 1)


def test_apply_sprint_due_dates_preserves_other_fields():
    tickets = [ProposedTicket(title="t1", description="d", priority="urgent", category="backend", sprint=1)]

    dated = apply_sprint_due_dates(tickets, sprint_start=date_for_test())

    assert dated[0].title == "t1"
    assert dated[0].priority == "urgent"
    assert dated[0].category == "backend"


# ---------------------------------------------------------------------------
# apply_sprint_due_dates — project_end compresses sprints into the real window
# ---------------------------------------------------------------------------


def test_apply_sprint_due_dates_with_project_end_pins_highest_sprint_to_deadline():
    from datetime import date

    start = date(2026, 7, 27)
    end = date(2026, 8, 24)  # 28 days out
    tickets = [ProposedTicket(title="t1", description="d", sprint=6)]

    dated = apply_sprint_due_dates(tickets, sprint_start=start, project_end=end)

    assert dated[0].due_date == "2026-08-24"


def test_apply_sprint_due_dates_with_project_end_spaces_sprints_proportionally():
    from datetime import date

    start = date(2026, 1, 1)
    end = date(2026, 1, 29)  # 28 days out
    tickets = [
        ProposedTicket(title="t1", description="d", sprint=1),
        ProposedTicket(title="t2", description="d", sprint=2),
        ProposedTicket(title="t3", description="d", sprint=4),
    ]

    dated = apply_sprint_due_dates(tickets, sprint_start=start, project_end=end)

    assert dated[0].due_date == "2026-01-08"  # 1/4 of 28 days
    assert dated[1].due_date == "2026-01-15"  # 2/4 of 28 days
    assert dated[2].due_date == "2026-01-29"  # 4/4 -- exactly project_end


def test_apply_sprint_due_dates_with_project_end_never_exceeds_deadline_even_with_many_sprints():
    from datetime import date

    start = date(2026, 7, 27)
    end = date(2026, 8, 24)
    # a large backlog that would normally span 6 sprints (24 weeks) under
    # the fixed-sprint-length default -- must all land within the 28-day window
    tickets = [ProposedTicket(title=f"t{i}", description="d", sprint=i) for i in range(1, 7)]

    dated = apply_sprint_due_dates(tickets, sprint_start=start, project_end=end)

    for ticket in dated:
        assert date.fromisoformat(ticket.due_date) <= end
    assert dated[-1].due_date == end.isoformat()


def test_apply_sprint_due_dates_ignores_project_end_if_not_after_start():
    from datetime import date

    start = date(2026, 7, 27)
    tickets = [ProposedTicket(title="t1", description="d", sprint=1)]

    dated = apply_sprint_due_dates(tickets, sprint_start=start, project_end=start)

    # falls back to the fixed 4-week sprint length instead of a zero-width window
    assert dated[0].due_date == "2026-08-24"
