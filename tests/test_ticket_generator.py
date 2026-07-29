import json

import pytest

from meta_harness.ticket_generator import (
    IDEA_TICKET_PROMPT,
    MAX_GENERATION_ATTEMPTS,
    TICKET_EXTRACTION_PROMPT,
    ClaudeNotFoundError,
    ProposedTicket,
    TicketExtractionError,
    TicketGenerationError,
    TicketParseError,
    apply_ticket_numbering,
    apply_sprint_due_dates,
    extract_document_text,
    generate_tickets_from_file,
    generate_tickets_from_idea,
    generate_tickets_from_text,
    parse_proposed_tickets,
    split_document,
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


def test_parse_error_shows_the_tail_so_truncation_is_visible():
    # A response cut off by a dropped connection looks fine at the start; the
    # end is the only place that shows it stopped mid-token.
    truncated = '[{"title": "' + "x" * 900 + '", "description": "cortado a mit'

    with pytest.raises(TicketParseError) as excinfo:
        parse_proposed_tickets(truncated)

    message = str(excinfo.value)
    assert "cortado a mit" in message, "the tail must be reported"
    assert f"{len(truncated)} chars" in message, "the length must be reported"


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


def test_prompt_requires_spanish_user_story_template():
    assert "user_story" in TICKET_EXTRACTION_PROMPT
    assert "SPANISH" in TICKET_EXTRACTION_PROMPT
    assert "Como <rol>" in TICKET_EXTRACTION_PROMPT
    assert "quiero <objetivo>" in TICKET_EXTRACTION_PROMPT
    assert "para <beneficio>" in TICKET_EXTRACTION_PROMPT


def test_prompt_requires_structured_spanish_acceptance_criteria():
    # Criteria must come back as parts, not one flattened sentence -- Linear's
    # house format renders Given/When/Then on separate bullets.
    assert '"acceptance_criteria" (array of objects' in TICKET_EXTRACTION_PROMPT
    for field in ('"name"', '"given"', '"when"', '"then"'):
        assert field in TICKET_EXTRACTION_PROMPT
    assert "Dado que" in TICKET_EXTRACTION_PROMPT


def test_prompt_defines_the_parent_child_hierarchy_contract():
    assert '"parent_title"' in TICKET_EXTRACTION_PROMPT
    assert "never nest more than one level deep" in TICKET_EXTRACTION_PROMPT


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
# apply_ticket_numbering — one prefix for every ticket.
# TAB/TAF/TAD/TAM claimed a distinction the model could not make reliably;
# a technical story and a user story differ by what they describe, not by a
# guessed label baked into the identifier.
# ---------------------------------------------------------------------------


def _ticket(title, category="mundane"):
    return ProposedTicket(title=title, description="d", acceptance_criteria=[], priority="normal", category=category)


def test_every_ticket_gets_the_tas_prefix():
    tickets = [_ticket("Do thing one"), _ticket("Do thing two")]

    numbered = apply_ticket_numbering(tickets)

    assert numbered[0].title == "TAS-01 | Do thing one"
    assert numbered[1].title == "TAS-02 | Do thing two"


def test_the_prefix_does_not_depend_on_category():
    tickets = [
        _ticket("General planning", "mundane"),
        _ticket("Build API endpoint", "backend"),
        _ticket("Build login page", "frontend"),
        _ticket("Set up CI pipeline", "deployment"),
    ]

    numbered = apply_ticket_numbering(tickets)

    assert [t.title for t in numbered] == [
        "TAS-01 | General planning",
        "TAS-02 | Build API endpoint",
        "TAS-03 | Build login page",
        "TAS-04 | Set up CI pipeline",
    ]


def test_one_shared_counter_across_categories():
    tickets = [
        _ticket("Backend one", "backend"),
        _ticket("Frontend one", "frontend"),
        _ticket("Backend two", "backend"),
    ]

    numbered = apply_ticket_numbering(tickets)

    assert [t.title for t in numbered] == [
        "TAS-01 | Backend one",
        "TAS-02 | Frontend one",
        "TAS-03 | Backend two",
    ]


def test_numbering_can_continue_an_existing_backlog():
    tickets = [_ticket("Next one"), _ticket("And another")]

    numbered = apply_ticket_numbering(tickets, start_number=7)

    assert numbered[0].title == "TAS-07 | Next one"
    assert numbered[1].title == "TAS-08 | And another"


def test_category_is_still_classified_for_the_badge():
    # It stops driving the identifier, but remains useful when scanning.
    numbered = apply_ticket_numbering([_ticket("Build API endpoint", "backend")])

    assert numbered[0].category == "backend"


def test_numbering_does_not_mutate_input():
    tickets = [_ticket("Do thing", "mundane")]

    apply_ticket_numbering(tickets, start_number=5)

    assert tickets[0].title == "Do thing"


def test_numbering_preserves_other_fields():
    tickets = [ProposedTicket(title="t1", description="desc", acceptance_criteria=["a"], priority="urgent", category="backend")]

    numbered = apply_ticket_numbering(tickets)

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


# ---------------------------------------------------------------------------
# generate_tickets_from_idea — the chat path: one free-text idea -> tickets
# ---------------------------------------------------------------------------


VALID_IDEA_TICKET = json.dumps(
    [
        {
            "title": "Exportar candidatos a Excel",
            "epic": "GESTIÓN DE CANDIDATOS",
            "ui_route": "/erp/talent/candidates",
            "backend_endpoint": "GET /api/v1/talent/candidates/export",
            "user_story": "Como reclutador,\nquiero exportar la lista de candidatos,\npara analizarla fuera del sistema.",
            "description": "Agregar un botón de exportación que descargue un archivo Excel.",
            "acceptance_criteria": ["Descarga: Dado que hay candidatos, cuando exporte, entonces se descarga un .xlsx."],
            "technical_notes": "",
            "priority": "normal",
            "category": "backend",
            "sprint": 1,
        }
    ]
)


def test_idea_prompt_reuses_the_same_ticket_field_contract():
    # The chat path must produce structurally identical tickets to the
    # document path -- same fields, same structured Spanish criteria.
    assert '"acceptance_criteria" (array of objects' in IDEA_TICKET_PROMPT
    assert '"epic"' in IDEA_TICKET_PROMPT
    assert '"technical_notes"' in IDEA_TICKET_PROMPT
    assert "SPANISH" in IDEA_TICKET_PROMPT


def test_generate_tickets_from_idea_happy_path(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.ticket_generator.subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": VALID_IDEA_TICKET, "stderr": ""})(),
    )

    tickets, warnings = generate_tickets_from_idea("quiero exportar candidatos a excel", timeout_s=5)

    assert len(tickets) == 1
    assert tickets[0].title == "Exportar candidatos a Excel"
    assert tickets[0].epic == "GESTIÓN DE CANDIDATOS"
    assert warnings == []


def test_generate_tickets_from_idea_puts_the_idea_in_the_prompt(monkeypatch):
    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return type("R", (), {"returncode": 0, "stdout": VALID_IDEA_TICKET, "stderr": ""})()

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    generate_tickets_from_idea("quiero exportar candidatos a excel", timeout_s=5)

    assert "quiero exportar candidatos a excel" in captured["prompt"]


def test_generate_tickets_from_idea_includes_history_for_refinement(monkeypatch):
    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return type("R", (), {"returncode": 0, "stdout": VALID_IDEA_TICKET, "stderr": ""})()

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    generate_tickets_from_idea(
        "sepáralo en dos tickets",
        history=[{"role": "user", "content": "exportar candidatos"}],
        timeout_s=5,
    )

    assert "exportar candidatos" in captured["prompt"]
    assert "sepáralo en dos tickets" in captured["prompt"]
    assert "continuing conversation" in captured["prompt"]


def test_generate_tickets_from_idea_rejects_empty_idea():
    with pytest.raises(TicketParseError):
        generate_tickets_from_idea("   ")


def test_generate_tickets_from_idea_retries_on_bad_json(monkeypatch):
    calls = []

    def fake_run(command, input, capture_output, text, timeout):
        calls.append(command)
        stdout = "not json" if len(calls) == 1 else VALID_IDEA_TICKET
        return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    tickets, _warnings = generate_tickets_from_idea("una idea", timeout_s=5)

    assert len(calls) == 2
    assert len(tickets) == 1


# ---------------------------------------------------------------------------
# split_document / chunked generation — a large document is generated as
# several concurrent calls instead of one that blows past the timeout.
# ---------------------------------------------------------------------------


def test_split_document_keeps_a_small_document_whole():
    chunks = split_document("un parrafo corto")

    assert len(chunks) == 1
    assert chunks[0].index == 1
    # Budget scales with content: a one-line document is not a 50-80 job.
    assert chunks[0].ticket_budget[1] <= 6


def test_split_document_splits_a_large_document_on_paragraphs():
    paragraph = "x" * 5_000
    doc = "\n\n".join([paragraph] * 20)  # 100k chars

    chunks = split_document(doc, target_chars=20_000)

    assert len(chunks) > 1
    assert [c.index for c in chunks] == list(range(1, len(chunks) + 1))
    # no paragraph was cut in half
    for chunk in chunks:
        for part in chunk.text.split("\n\n"):
            assert part == paragraph


def test_split_document_shares_the_ticket_budget_across_sections():
    doc = "\n\n".join(["y" * 5_000] * 20)

    chunks = split_document(doc, target_chars=20_000, budget=(50, 80))

    assert len(chunks) > 1
    # each section asks only for what its own length warrants, never the
    # whole document's 50-80
    from meta_harness.ticket_generator import MAX_TICKETS_PER_CHUNK
    for chunk in chunks:
        low, high = chunk.ticket_budget
        assert low < high
        assert high <= MAX_TICKETS_PER_CHUNK


def test_split_document_breaks_down_an_oversized_block():
    # Extracted PDF text often has a long stretch with no blank lines. Leaving
    # it whole would defeat the point of chunking, so it must be broken up.
    huge = ". ".join(["frase de relleno numero %d" % i for i in range(3000)])
    doc = "small\n\n" + huge + "\n\nalso small"

    chunks = split_document(doc, target_chars=20_000)

    assert len(huge) > 60_000
    for chunk in chunks:
        assert len(chunk.text) <= 22_000, "no section may blow past the target"


def test_split_document_hard_slices_text_with_no_boundaries_at_all():
    doc = "z" * 100_000  # no paragraphs, no lines, no sentences

    chunks = split_document(doc, target_chars=20_000)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 22_000


def _chunked_claude(monkeypatch, per_call_titles, calls=None):
    """Return a distinct ticket batch per invocation, keyed by call order."""
    counter = {"n": 0}
    lock_free = []

    def fake_run(command, input, capture_output, text, timeout):
        n = counter["n"]
        counter["n"] += 1
        lock_free.append(command[2])
        titles = per_call_titles[min(n, len(per_call_titles) - 1)]
        payload = [_valid_ticket(title=t) for t in titles]
        return Result(stdout=json.dumps(payload))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)
    if calls is not None:
        calls.append(lock_free)
    return lock_free


def _big_doc(target_chars=20_000):
    return "\n\n".join(["w" * 5_000] * 12)


def test_chunked_generation_merges_every_section(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.CHUNK_TARGET_CHARS", 20_000)
    _chunked_claude(monkeypatch, [["A1", "A2"], ["B1"], ["C1", "C2"]])

    tickets, warnings = generate_tickets_from_text(_big_doc(), timeout_s=5, workers=1)

    assert len(tickets) >= 3
    assert warnings == []


def test_chunked_generation_dedupes_titles_repeated_across_sections(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.CHUNK_TARGET_CHARS", 20_000)
    # every section proposes the same ticket -- a feature described either
    # side of a section boundary must not become two tickets
    _chunked_claude(monkeypatch, [["Duplicado"]])

    tickets, _warnings = generate_tickets_from_text(_big_doc(), timeout_s=5, workers=1)

    assert [t.title for t in tickets] == ["Duplicado"]


def test_chunked_generation_survives_one_failing_section(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.CHUNK_TARGET_CHARS", 20_000)
    counter = {"n": 0}

    def fake_run(command, input, capture_output, text, timeout):
        counter["n"] += 1
        # the very first call returns junk on all its attempts, later ones work
        if counter["n"] <= MAX_GENERATION_ATTEMPTS:
            return Result(stdout="not json")
        return Result(stdout=json.dumps([_valid_ticket(title=f"OK{counter['n']}")]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    tickets, warnings = generate_tickets_from_text(_big_doc(), timeout_s=5, workers=1)

    assert tickets, "surviving sections must still produce tickets"
    assert any("could not be analyzed" in w for w in warnings)


def test_chunked_generation_raises_only_when_every_section_fails(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.CHUNK_TARGET_CHARS", 20_000)
    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.ticket_generator.subprocess.run",
        lambda *a, **k: Result(stdout="never valid"),
    )

    with pytest.raises(TicketParseError, match="All .* sections"):
        generate_tickets_from_text(_big_doc(), timeout_s=5, workers=1)


def test_chunked_generation_tells_each_call_which_section_it_is(monkeypatch):
    monkeypatch.setattr("meta_harness.ticket_generator.CHUNK_TARGET_CHARS", 20_000)
    prompts = _chunked_claude(monkeypatch, [["A"]])

    generate_tickets_from_text(_big_doc(), timeout_s=5, workers=1)

    assert any("SECTION 1 OF" in p for p in prompts)
    assert any("SECTION 2 OF" in p for p in prompts)
    # later sections are told the groundwork already exists
    assert any("Earlier sections have already been processed" in p for p in prompts)


def test_small_document_still_takes_the_single_call_path(monkeypatch):
    prompts = _chunked_claude(monkeypatch, [["Solo"]])

    generate_tickets_from_text("documento corto", timeout_s=5)

    assert len(prompts) == 1
    assert "SECTION" not in prompts[0]


def test_a_failed_cli_invocation_is_retried_not_fatal(monkeypatch):
    # A non-zero exit has nothing to repair in the prompt, but it is often
    # transient under load -- it must not kill the section on first stumble.
    calls = []

    def fake_run(command, input, capture_output, text, timeout):
        calls.append(command)
        if len(calls) == 1:
            return Result(returncode=1, stdout="", stderr="")
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    tickets, _warnings = generate_tickets_from_text("documento corto", timeout_s=5)

    assert len(calls) == 2
    assert len(tickets) == 1


def test_a_retried_cli_failure_does_not_corrupt_the_prompt(monkeypatch):
    # There is no bad response to quote back, so the retry must send the
    # original prompt rather than a "your previous response was invalid" one.
    prompts = []

    def fake_run(command, input, capture_output, text, timeout):
        prompts.append(command[2])
        if len(prompts) == 1:
            return Result(returncode=1, stderr="boom")
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    generate_tickets_from_text("documento corto", timeout_s=5)

    assert "previous response was invalid" not in prompts[1]
    assert prompts[0] == prompts[1]


def test_cli_failure_message_falls_back_to_stdout(monkeypatch):
    # An exit code with empty stderr is undiagnosable; surface stdout instead.
    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.ticket_generator.subprocess.run",
        lambda *a, **k: Result(returncode=1, stdout="usage limit reached", stderr=""),
    )

    with pytest.raises(TicketGenerationError, match="usage limit reached"):
        generate_tickets_from_text("documento corto", timeout_s=5, max_attempts=1)


# ---------------------------------------------------------------------------
# ticket_budget_for — the ask has to scale with the content.
# Asking a fixed 50-80 of every call made a small document as slow and as
# fragile as a huge one, and fragmented it into dozens of tiny tickets.
# ---------------------------------------------------------------------------


def test_a_small_document_is_not_asked_for_dozens_of_tickets():
    from meta_harness.ticket_generator import ticket_budget_for

    low, high = ticket_budget_for(11_000)  # a ~3k-token proposal

    assert high <= 12, "a short proposal must not be asked for a full document's worth"
    assert low < high


def test_a_tiny_document_still_asks_for_a_few():
    from meta_harness.ticket_generator import ticket_budget_for

    low, high = ticket_budget_for(500)

    assert low >= 2 and high >= 4, "even a one-pager should yield something"


def test_no_single_call_exceeds_the_safe_output_volume():
    from meta_harness.ticket_generator import MAX_TICKETS_PER_CHUNK, ticket_budget_for

    # Exceeding this is what got responses truncated mid-JSON.
    for chars in (50_000, 200_000, 1_000_000):
        assert ticket_budget_for(chars)[1] <= MAX_TICKETS_PER_CHUNK


def test_an_unsplit_document_gets_a_size_derived_budget():
    # The single-call path used to inherit the whole 50-80 regardless of size.
    chunks = split_document("x" * 11_000)

    assert len(chunks) == 1
    assert chunks[0].ticket_budget[1] <= 12


def test_a_full_size_document_still_totals_the_intended_range():
    doc = "\n\n".join(["y" * 4_000] * 24)  # ~96k chars

    chunks = split_document(doc)
    total = sum(c.ticket_budget[1] for c in chunks)

    assert 40 <= total <= 90, f"a full spec should still land near the intended range, got {total}"


def test_the_single_call_prompt_states_the_size_derived_target(monkeypatch):
    captured = {}

    def fake_run(command, input, capture_output, text, timeout):
        captured["prompt"] = command[2]
        return Result(stdout=json.dumps([_valid_ticket()]))

    monkeypatch.setattr("meta_harness.ticket_generator.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.ticket_generator.subprocess.run", fake_run)

    generate_tickets_from_text("x" * 11_000, timeout_s=5)

    assert "50-80" not in captured["prompt"], "a small document must not be asked for 50-80"
    assert "for a document of this size" in captured["prompt"].lower()


# ---------------------------------------------------------------------------
# Technical stories vs user stories.
# Backend tickets were being written as though someone were clicking through a
# screen, leaving the endpoint they exist to define unstated.
# ---------------------------------------------------------------------------


def test_every_prompt_teaches_the_two_story_kinds():
    from meta_harness.ticket_generator import IDEA_TICKET_PROMPT, TICKET_EXTRACTION_PROMPT
    from meta_harness.ticket_reformat import REFORMAT_PROMPT

    for prompt in (TICKET_EXTRACTION_PROMPT, IDEA_TICKET_PROMPT, REFORMAT_PROMPT):
        assert "TECHNICAL STORY" in prompt
        assert "USER STORY" in prompt
        # stated once, not duplicated by the shared field spec
        assert prompt.count("EVERY ticket is one of two kinds") == 1


def test_a_technical_story_must_carry_its_endpoint():
    from meta_harness.ticket_generator import TICKET_EXTRACTION_PROMPT

    assert 'fill "backend_endpoint"' in TICKET_EXTRACTION_PROMPT
    assert 'do not leave "backend_endpoint"' in TICKET_EXTRACTION_PROMPT


def test_a_technical_story_is_described_by_its_contract():
    from meta_harness.ticket_generator import TICKET_EXTRACTION_PROMPT

    for expected in ("what it receives", "what it validates", "status codes"):
        assert expected in TICKET_EXTRACTION_PROMPT
    assert "Do NOT write a technical story as though a person were clicking" in TICKET_EXTRACTION_PROMPT


def test_mixed_work_is_split_rather_than_blurred():
    from meta_harness.ticket_generator import TICKET_EXTRACTION_PROMPT

    assert "split it into one technical story and one user story" in TICKET_EXTRACTION_PROMPT


def test_the_category_label_no_longer_claims_to_set_the_identifier():
    from meta_harness.ticket_generator import TICKET_EXTRACTION_PROMPT

    assert "does not change the ticket identifier" in TICKET_EXTRACTION_PROMPT
