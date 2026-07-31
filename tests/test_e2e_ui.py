"""End-to-end tests: the real UI, in a real browser, one section per module.

These cover what the unit and route tests structurally cannot — that the
markup, the CSS and app.js actually work together once a browser runs them.
The API is stubbed (see e2e_driver.StubAPIServer) so the tests are fast,
offline and deterministic; the API layer itself is already covered elsewhere.

Skipped automatically when Chromium or `websockets` is unavailable, so this
never breaks a machine that can't run a browser.
"""

from __future__ import annotations

import pytest

from e2e_driver import Browser, StubAPIServer, chromium_available

pytestmark = pytest.mark.skipif(
    not chromium_available(), reason="Chromium and/or the websockets package is not available"
)

TRACKERS = ("clickup", "linear")


@pytest.fixture(scope="module")
def server():
    with StubAPIServer() as running:
        yield running


@pytest.fixture(scope="module")
def browser(server):
    with Browser() as running:
        running.goto(server.url)
        yield running


@pytest.fixture(autouse=True)
def fresh_page(browser, server):
    """Reload between tests so one test's state can't leak into the next."""
    browser.goto(server.url)
    return browser


# ===========================================================================
# Module: application shell and branding
# ===========================================================================


def test_shell_shows_the_metrica_jarvis_identity(browser):
    assert browser.eval("document.title") == "METRICA | JARVIS-AGENT"
    assert browser.eval("document.querySelector('.brand-org').textContent") == "METRICA"
    assert browser.eval("document.querySelector('.brand-product').textContent") == "JARVIS-AGENT"
    assert browser.visible(".brand-mark"), "the logo must render"


def test_shell_exposes_the_trackers_and_sources(browser):
    """Sources sits alongside the trackers rather than inside one: a source is
    a repository, and the same one feeds the module check on both."""
    assert browser.eval("[...document.querySelectorAll('.tab-button')].map(b => b.textContent)") == [
        "ClickUp",
        "Linear",
        "Sources",
    ]


def test_sources_tab_opens_and_offers_registration(browser):
    browser.click('.tab-button[data-tab="sources"]')

    assert browser.visible("#tab-sources"), "the Sources panel must open"
    assert browser.visible("#source-path"), "a repository path field is the point of the tab"
    assert browser.visible("#source-add-btn")


# ===========================================================================
# Module: theme
# ===========================================================================


def test_theme_resolves_before_paint(browser):
    # An unset attribute means the page rendered with no theme and then
    # flipped, which is the flash the inline script exists to prevent.
    assert browser.eval("document.documentElement.getAttribute('data-theme')") in ("dark", "light")


def test_theme_toggle_flips_and_persists(browser):
    before = browser.eval("document.documentElement.getAttribute('data-theme')")
    browser.click("#theme-toggle")
    after = browser.eval("document.documentElement.getAttribute('data-theme')")

    assert after != before
    assert browser.eval("localStorage.getItem('mh-theme')") == after


def test_theme_survives_a_reload(browser, server):
    browser.eval("localStorage.setItem('mh-theme', 'light'); return true;")
    browser.goto(server.url)

    assert browser.eval("document.documentElement.getAttribute('data-theme')") == "light"


def test_theme_actually_changes_the_rendered_colours(browser):
    browser.eval("document.documentElement.setAttribute('data-theme','light'); return true;")
    light = browser.eval("getComputedStyle(document.body).backgroundColor")
    browser.eval("document.documentElement.setAttribute('data-theme','dark'); return true;")
    dark = browser.eval("getComputedStyle(document.body).backgroundColor")

    assert light != dark, "the attribute must drive real CSS, not just sit in the DOM"


# ===========================================================================
# Module: tracker tabs
# ===========================================================================


def test_only_one_tracker_panel_is_visible_at_a_time(browser):
    assert browser.visible("#tab-clickup")
    assert not browser.visible("#tab-linear")

    browser.click('.tab-button[data-tab="linear"]')

    assert browser.visible("#tab-linear")
    assert not browser.visible("#tab-clickup")


# ===========================================================================
# Module: workflow sub-tabs
# ===========================================================================


@pytest.mark.parametrize("tracker", TRACKERS)
def test_every_tracker_has_the_same_five_workflows(browser, tracker):
    labels = browser.eval(
        f"[...document.querySelectorAll('.subtab[data-tracker=\"{tracker}\"]')].map(b => b.textContent)"
    )
    assert labels == ["Generate tickets", "QA review", "Module check", "Modify ticket", "Findings"]


@pytest.mark.parametrize("tracker", TRACKERS)
def test_generate_is_the_landing_workflow(browser, tracker):
    if tracker == "linear":
        browser.click('.tab-button[data-tab="linear"]')
    assert browser.visible(f"#{tracker}-sub-generate")
    for other in ("qa", "module", "modify", "findings"):
        assert not browser.visible(f"#{tracker}-sub-{other}")


@pytest.mark.parametrize("tracker", TRACKERS)
@pytest.mark.parametrize("sub", ["qa", "module", "modify", "findings"])
def test_choosing_a_workflow_shows_only_that_panel(browser, tracker, sub):
    if tracker == "linear":
        browser.click('.tab-button[data-tab="linear"]')
    browser.click(f"#{tracker}-subtab-{sub}")

    assert browser.visible(f"#{tracker}-sub-{sub}")
    others = [s for s in ("generate", "qa", "module", "modify", "findings") if s != sub]
    for other in others:
        assert not browser.visible(f"#{tracker}-sub-{other}"), f"{other} should be hidden"


@pytest.mark.parametrize("tracker", TRACKERS)
def test_the_selected_workflow_is_marked_for_assistive_tech(browser, tracker):
    if tracker == "linear":
        browser.click('.tab-button[data-tab="linear"]')
    browser.click(f"#{tracker}-subtab-qa")

    assert browser.eval(f"document.getElementById('{tracker}-subtab-qa').getAttribute('aria-selected')") == "true"
    assert browser.eval(f"document.getElementById('{tracker}-subtab-generate').getAttribute('aria-selected')") == "false"


@pytest.mark.parametrize("tracker", TRACKERS)
def test_the_destination_stays_pinned_above_the_workflows(browser, tracker):
    # Every workflow depends on it, so it must not be hidden behind a tab.
    if tracker == "linear":
        browser.click('.tab-button[data-tab="linear"]')
    browser.click(f"#{tracker}-subtab-findings")

    assert browser.visible(f"#{tracker}-scope"), "the destination picker must remain visible"


# ===========================================================================
# Module: destination picker
# ===========================================================================


def test_scopes_load_from_the_api(browser):
    browser.wait_for("document.querySelectorAll('#clickup-scope option').length > 1")
    names = browser.eval("[...document.querySelectorAll('#clickup-scope option')].map(o => o.textContent)")
    assert "gru-po" in names


def test_choosing_a_scope_loads_its_containers_and_statuses(browser):
    browser.wait_for("document.querySelectorAll('#clickup-scope option').length > 1")
    browser.type_into("#clickup-scope", "S1")
    browser.wait_for("!document.getElementById('clickup-container').disabled")

    containers = browser.eval("[...document.querySelectorAll('#clickup-container option')].map(o => o.textContent)")
    statuses = browser.eval("[...document.querySelectorAll('#clickup-qa-status-pass option')].map(o => o.textContent)")

    assert "Sprint backlog" in containers
    assert "open" in statuses and "done" in statuses


def test_the_destination_summary_reflects_the_choice(browser):
    browser.wait_for("document.querySelectorAll('#clickup-scope option').length > 1")
    browser.type_into("#clickup-scope", "S1")
    browser.wait_for("!document.getElementById('clickup-container').disabled")
    browser.type_into("#clickup-container", "L1")
    browser.wait_for("!document.getElementById('clickup-destination-summary').classList.contains('is-empty')")

    summary = browser.eval("document.getElementById('clickup-destination-summary').textContent")
    assert "gru-po" in summary and "Sprint backlog" in summary


# ===========================================================================
# Module: ticket pickers (typable, shared by QA / module / modify)
# ===========================================================================


def _pick_destination(browser):
    browser.wait_for("document.querySelectorAll('#clickup-scope option').length > 1")
    browser.type_into("#clickup-scope", "S1")
    browser.wait_for("!document.getElementById('clickup-container').disabled")
    browser.type_into("#clickup-container", "L1")
    browser.wait_for("!document.getElementById('clickup-qa-ticket').disabled")


@pytest.mark.parametrize("picker", ["qa-ticket", "module-ticket", "reformat-ticket"])
def test_every_ticket_picker_is_typable_and_populated(browser, picker):
    _pick_destination(browser)

    assert browser.eval(f"document.getElementById('clickup-{picker}').tagName") == "INPUT"
    options = browser.eval(
        f"[...document.querySelectorAll('#clickup-{picker}-options option')].map(o => o.value)"
    )
    assert any("Endpoint de personas" in o for o in options)
    assert browser.eval(f"document.getElementById('clickup-{picker}-status').textContent") != ""


def test_picking_a_ticket_by_its_label_enables_the_action(browser):
    _pick_destination(browser)
    label = browser.eval(
        "document.querySelector('#clickup-qa-ticket-options option').value"
    )
    browser.type_into("#clickup-qa-ticket", label)

    assert not browser.eval("document.getElementById('clickup-qa-analyze-btn').disabled")
    assert "Reviewing" in browser.eval("document.getElementById('clickup-qa-selected').textContent")


def test_text_that_matches_no_ticket_leaves_the_action_disabled(browser):
    _pick_destination(browser)
    browser.type_into("#clickup-qa-ticket", "no existe este ticket")

    assert browser.eval("document.getElementById('clickup-qa-analyze-btn').disabled")


# ===========================================================================
# Module: ticket generation — chat is the only entry point
# ===========================================================================


@pytest.mark.parametrize("tracker", TRACKERS)
def test_the_chat_is_shown_with_no_mode_to_choose_first(browser, tracker):
    if tracker == "linear":
        browser.click('.tab-button[data-tab="linear"]')

    assert browser.visible(f"#{tracker}-mode-chat")
    assert browser.eval(f"document.getElementById('{tracker}-mode-document') === null")
    assert browser.eval(f"document.getElementById('{tracker}-mode-switch') === null")


def test_the_composer_attach_control_accepts_only_supported_documents(browser):
    accept = browser.eval("document.getElementById('clickup-chat-file').accept")
    assert ".md" in accept and ".pdf" in accept and ".txt" in accept


def test_the_attach_control_is_a_labelled_icon_button(browser):
    assert browser.eval("document.getElementById('clickup-chat-attach').getAttribute('aria-label')")
    assert browser.visible("#clickup-chat-attach svg")


def test_no_attachment_chip_is_shown_before_a_document_is_picked(browser):
    assert not browser.visible("#clickup-chat-attachment")


# ===========================================================================
# Module: chat (usability heuristics)
# ===========================================================================


def _open_chat(browser, tracker="clickup"):
    # The chat is the generation surface now; there is no mode to switch to.
    if tracker == "linear":
        browser.click('.tab-button[data-tab="linear"]')


@pytest.mark.parametrize("tracker", TRACKERS)
def test_chat_offers_example_openers_when_empty(browser, tracker):
    # Recognition rather than recall: a blank box makes the user invent the
    # phrasing as well as the idea.
    _open_chat(browser, tracker)

    assert browser.visible(f"#{tracker}-chat-suggestions")
    count = browser.eval(f"document.querySelectorAll('#{tracker}-chat-suggestions .chat-suggestion').length")
    assert count >= 3


def test_clicking_a_suggestion_fills_the_composer(browser):
    _open_chat(browser)
    text = browser.eval("document.querySelector('#clickup-chat-suggestions .chat-suggestion').textContent")
    browser.click("#clickup-chat-suggestions .chat-suggestion")

    assert browser.eval("document.getElementById('clickup-chat-input').value") == text


def test_send_is_disabled_until_there_is_something_to_send(browser):
    # Error prevention: an empty message can't be submitted at all.
    _open_chat(browser)
    assert browser.eval("document.getElementById('clickup-chat-send').disabled")

    browser.type_into("#clickup-chat-input", "necesito exportar candidatos")
    assert not browser.eval("document.getElementById('clickup-chat-send').disabled")


def test_whitespace_alone_does_not_enable_send(browser):
    _open_chat(browser)
    browser.type_into("#clickup-chat-input", "    ")

    assert browser.eval("document.getElementById('clickup-chat-send').disabled")


def test_the_send_control_is_a_labelled_icon_button(browser):
    _open_chat(browser)
    assert browser.eval("document.getElementById('clickup-chat-send').getAttribute('aria-label')")
    assert browser.visible("#clickup-chat-send svg")


def test_enter_sends_and_shift_enter_does_not(browser):
    # Consistency with every other chat app; Shift+Enter must still newline.
    _open_chat(browser)
    browser.type_into("#clickup-chat-input", "una idea")

    browser.press_enter("#clickup-chat-input", shift=True)
    assert browser.eval("document.getElementById('clickup-chat-input').value") == "una idea", (
        "Shift+Enter must not submit"
    )

    browser.press_enter("#clickup-chat-input")
    browser.wait_for("document.getElementById('clickup-chat-input').value === ''")


def test_the_conversation_is_announced_to_assistive_tech(browser):
    _open_chat(browser)
    assert browser.eval("document.getElementById('clickup-chat-log').getAttribute('aria-live')") == "polite"


def test_the_sent_message_appears_in_the_conversation(browser):
    _open_chat(browser)
    browser.type_into("#clickup-chat-input", "necesito exportar candidatos a Excel")
    browser.press_enter("#clickup-chat-input")
    browser.wait_for("document.querySelectorAll('#clickup-chat-log .chat-msg-user').length === 1")

    assert "exportar candidatos" in browser.eval(
        "document.querySelector('#clickup-chat-log .chat-msg-user').textContent"
    )


def test_a_failed_generation_reports_inline_with_a_retry(browser):
    _open_chat(browser)
    browser.type_into("#clickup-chat-input", "FORCE_ERROR")
    browser.press_enter("#clickup-chat-input")
    browser.wait_for(
        "document.querySelectorAll('#clickup-chat-log .chat-msg-error').length === 1", timeout_s=20
    )

    assert browser.visible("#clickup-chat-log .chat-retry"), "an error must offer a way to recover"


def _attach_document(browser, tracker="clickup", filename="requisitos.md"):
    browser.attach_file(f"#{tracker}-chat-file", filename)


def test_attaching_a_document_names_it_and_enables_send_without_typing(browser):
    # A document alone is a complete request; requiring a message too would
    # be a made-up rule the user has to discover by being blocked.
    _open_chat(browser)
    _attach_document(browser, "clickup", "requisitos.md")

    assert browser.visible("#clickup-chat-attachment")
    assert "requisitos.md" in browser.eval(
        "document.querySelector('#clickup-chat-attachment .chat-attachment-name').textContent"
    )
    assert not browser.eval("document.getElementById('clickup-chat-send').disabled")


def test_removing_the_attachment_disables_send_again(browser):
    _open_chat(browser)
    _attach_document(browser, "clickup", "requisitos.md")
    browser.click("#clickup-chat-attach-clear")

    assert not browser.visible("#clickup-chat-attachment")
    assert browser.eval("document.getElementById('clickup-chat-send').disabled")


def test_a_sent_attachment_is_named_in_the_conversation(browser):
    _open_chat(browser)
    _attach_document(browser, "clickup", "requisitos.md")
    browser.click("#clickup-chat-send")
    browser.wait_for("document.querySelectorAll('#clickup-chat-log .chat-msg-user').length === 1")

    assert "requisitos.md" in browser.eval(
        "document.querySelector('#clickup-chat-log .chat-msg-user').textContent"
    )
    assert not browser.visible("#clickup-chat-attachment"), "the chip must clear once sent"

    # The chip in the transcript proves the UI believed it sent a file; only
    # the request body proves the file was actually sent.
    sent = browser.eval(
        "return fetch('/__last_post?path=/api/tickets/chat').then(r => r.json()).then(r => r.body);"
    )
    assert 'name="file"' in sent, "the attached document must be in the request"
    assert "requisitos.md" in sent, "the request must carry the real filename"


PNG_B64 = (
    # A real 4x4 PNG, so the browser builds a genuine image File rather than
    # a blob that merely claims to be one.
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAB7RxjAAAAFElEQVR4nGP8//8/AzZgYsAJ"
    "hqUkAAmpAglBpPO3AAAAAElFTkSuQmCC"
)


def _paste_image(browser, tracker="clickup"):
    browser.paste(f"#{tracker}-chat-input", image_b64=PNG_B64)


@pytest.mark.parametrize("tracker", TRACKERS)
def test_pasting_an_image_shows_it_in_the_composer(browser, tracker):
    _open_chat(browser, tracker)
    _paste_image(browser, tracker)

    assert browser.visible(f"#{tracker}-chat-images")
    assert browser.eval(f"document.querySelectorAll('#{tracker}-chat-images .chat-image img').length") == 1


def test_a_pasted_image_alone_is_enough_to_send(browser):
    # A mockup with no words is a complete request; demanding a sentence too
    # would be a rule the user only discovers by being blocked.
    _open_chat(browser)
    assert browser.eval("document.getElementById('clickup-chat-send').disabled")

    _paste_image(browser)

    assert not browser.eval("document.getElementById('clickup-chat-send').disabled")


def test_pasting_several_images_keeps_all_of_them(browser):
    _open_chat(browser)
    _paste_image(browser)
    _paste_image(browser)

    assert browser.eval("document.querySelectorAll('#clickup-chat-images .chat-image').length") == 2


def test_a_pasted_image_can_be_removed_again(browser):
    _open_chat(browser)
    _paste_image(browser)
    browser.click("#clickup-chat-images .chat-image-remove")

    assert not browser.visible("#clickup-chat-images")
    assert browser.eval("document.getElementById('clickup-chat-send').disabled")


def test_pasting_plain_text_is_left_to_the_browser(browser):
    # The negative control: claiming every paste would break ordinary typing,
    # so a text paste must come back uncancelled.
    _open_chat(browser)
    claimed = browser.paste("#clickup-chat-input", text="necesito exportar candidatos")

    assert claimed is False, "a text paste must not be swallowed"
    assert browser.eval("document.querySelectorAll('#clickup-chat-images .chat-image').length") == 0
    assert not browser.visible("#clickup-chat-images")


def test_pasting_an_image_is_claimed_by_the_app(browser):
    _open_chat(browser)

    assert browser.paste("#clickup-chat-input", image_b64=PNG_B64) is True


def test_pasting_more_images_than_allowed_is_refused_with_a_reason(browser):
    _open_chat(browser)
    for _ in range(7):
        _paste_image(browser)

    assert browser.eval("document.querySelectorAll('#clickup-chat-images .chat-image').length") == 6
    assert browser.visible("#clickup-generate-error"), "hitting the cap must say so"


def test_a_pasted_image_reaches_the_server_and_shows_in_the_transcript(browser):
    _open_chat(browser)
    _paste_image(browser)
    browser.click("#clickup-chat-send")
    browser.wait_for("document.querySelectorAll('#clickup-chat-log .chat-msg-user').length === 1")

    assert browser.eval(
        "document.querySelectorAll('#clickup-chat-log .chat-msg-images img').length"
    ) == 1, "the sent screenshot must stay visible in the conversation"
    assert not browser.visible("#clickup-chat-images"), "the pending row must clear once sent"

    sent = browser.eval(
        "return fetch('/__last_post?path=/api/tickets/chat').then(r => r.json()).then(r => r.body);"
    )
    assert 'name="images"' in sent, "the pasted image must actually be uploaded"


def test_a_large_batch_points_below_instead_of_flooding_the_conversation(browser, tracker="clickup"):
    _open_chat(browser)
    browser.type_into("#clickup-chat-input", "FORCE_OVERFLOW")
    browser.press_enter("#clickup-chat-input")
    browser.wait_for(
        "document.querySelectorAll('#clickup-chat-log .chat-msg-overflow').length === 1", timeout_s=20
    )

    bubble = browser.eval("document.querySelector('#clickup-chat-log .chat-msg-overflow').textContent")
    assert "moved below in the page" in bubble
    assert "scroll down" in bubble
    assert browser.eval("document.querySelectorAll('#clickup-chat-log .chat-msg-ticket').length") == 0, (
        "an overflowing batch must not also render inside the conversation"
    )


def test_an_overflowing_batch_is_reviewable_below_the_chat(browser):
    _open_chat(browser)
    browser.type_into("#clickup-chat-input", "FORCE_OVERFLOW")
    browser.press_enter("#clickup-chat-input")
    browser.wait_for("document.querySelectorAll('#clickup-tickets-list .ticket-card').length === 8", timeout_s=20)

    assert browser.visible("#clickup-generate-actions"), "the bulk-add controls must come with the cards"


def test_a_small_batch_still_renders_inside_the_conversation(browser):
    # The negative control for the overflow branch: two tickets stay put.
    _generate_from_chat(browser)

    assert browser.eval("document.querySelectorAll('#clickup-chat-log .chat-msg-overflow').length") == 0
    assert browser.eval("document.querySelectorAll('#clickup-tickets-list .ticket-card').length") == 0


# ===========================================================================
# Module: QA review
# ===========================================================================


def test_qa_actions_are_disabled_before_a_destination_is_chosen(browser):
    browser.click("#clickup-subtab-qa")

    assert browser.eval("document.getElementById('clickup-qa-analyze-btn').disabled")
    assert browser.eval("document.getElementById('clickup-qa-bulk-run-btn').disabled")


def test_qa_bulk_unlocks_once_tickets_are_loaded(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-qa")

    assert not browser.eval("document.getElementById('clickup-qa-bulk-run-btn').disabled")


def test_qa_offers_pass_and_fail_status_moves(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-qa")

    for select in ("clickup-qa-status-pass", "clickup-qa-status-fail"):
        options = browser.eval(f"[...document.querySelectorAll('#{select} option')].map(o => o.textContent)")
        assert options[0] == "Leave unchanged", "not moving the ticket must be the default"
        assert "done" in options


# ===========================================================================
# Module: module identification
# ===========================================================================


def test_module_check_requires_a_ticket_and_documentation(browser):
    browser.click("#clickup-subtab-module")
    browser.click("#clickup-module-check-btn")
    browser.wait_for("!document.getElementById('clickup-module-error').hidden")

    assert "Pick a" in browser.eval("document.getElementById('clickup-module-error').textContent")


def test_module_check_complains_about_missing_documentation(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-module")
    label = browser.eval("document.querySelector('#clickup-module-ticket-options option').value")
    browser.type_into("#clickup-module-ticket", label)
    browser.type_into("#clickup-module-name", "Personas")
    browser.click("#clickup-module-check-btn")
    browser.wait_for("!document.getElementById('clickup-module-error').hidden")

    assert "documentation" in browser.eval("document.getElementById('clickup-module-error').textContent")


def test_module_bulk_unlocks_with_a_destination(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-module")

    assert not browser.eval("document.getElementById('clickup-module-check-all-btn').disabled")


# ===========================================================================
# Module: modify ticket (reformat + revert)
# ===========================================================================


def test_modify_is_locked_until_a_destination_is_chosen(browser):
    browser.click("#clickup-subtab-modify")

    assert browser.eval("document.getElementById('clickup-reformat-btn').disabled")


def test_modify_unlocks_once_tickets_are_loaded(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-modify")

    assert not browser.eval("document.getElementById('clickup-reformat-btn').disabled")


def test_the_undo_bar_lists_revertible_tickets(browser):
    browser.click("#clickup-subtab-modify")
    browser.wait_for("!document.getElementById('clickup-revert-panel').hidden")

    options = browser.eval("[...document.querySelectorAll('#clickup-revert-select option')].map(o => o.textContent)")
    assert any("Endpoint de personas" in o for o in options)
    assert "restored" in browser.eval("document.getElementById('clickup-revert-note').textContent")


def test_reverting_without_choosing_a_ticket_is_refused(browser):
    browser.click("#clickup-subtab-modify")
    browser.wait_for("!document.getElementById('clickup-revert-panel').hidden")
    browser.click("#clickup-revert-btn")
    browser.wait_for("!document.getElementById('clickup-reformat-error').hidden")

    assert "Pick a" in browser.eval("document.getElementById('clickup-reformat-error').textContent")


# ===========================================================================
# Module: findings
# ===========================================================================


def test_findings_render_with_their_counts(browser):
    browser.click("#clickup-subtab-findings")
    browser.wait_for("document.querySelectorAll('#clickup-findings-table tbody tr').length > 0")

    row = browser.eval("document.querySelector('#clickup-findings-table tbody tr').textContent")
    assert "/login" in row and "major" in row

    counts = browser.eval("document.getElementById('clickup-findings-counts').textContent")
    assert "Total" in counts


def test_findings_offer_the_documented_filters(browser):
    browser.click("#clickup-subtab-findings")

    severities = browser.eval(
        "[...document.querySelectorAll('#clickup-filter-severity option')].map(o => o.value)"
    )
    statuses = browser.eval("[...document.querySelectorAll('#clickup-filter-status option')].map(o => o.value)")

    assert severities == ["", "minor", "major", "critical"]
    assert statuses == ["", "open", "acknowledged", "closed"]


def test_a_finding_links_back_to_its_tracker_item(browser):
    browser.click("#clickup-subtab-findings")
    browser.wait_for("document.querySelectorAll('#clickup-findings-table tbody tr').length > 0")

    assert "task-1" in browser.eval("document.querySelector('#clickup-findings-table tbody tr').textContent")


# ===========================================================================
# FLOW: ticket generation from a document, through to creation
# ===========================================================================


def _generate_from_chat(browser, tracker="clickup"):
    """Drive the chat all the way to a rendered ticket proposal."""
    _open_chat(browser, tracker)
    browser.type_into(f"#{tracker}-chat-input", "necesito registrar personas en el pool de talento")
    browser.press_enter(f"#{tracker}-chat-input")
    browser.wait_for(
        f"document.querySelectorAll('#{tracker}-chat-log .chat-msg-ticket').length === 1", timeout_s=20
    )


def test_flow_chat_generates_a_ticket_and_renders_it_in_the_conversation(browser):
    _generate_from_chat(browser)

    bubble = browser.eval("document.querySelector('#clickup-chat-log .chat-msg-ticket').textContent")
    assert "Endpoint de personas" in bubble
    assert "Como reclutador" in bubble, "the user story must be shown"
    assert "la persona queda registrada" in bubble, "acceptance criteria must be shown"


def test_flow_generated_ticket_renders_its_code_block_as_code(browser):
    _generate_from_chat(browser)

    assert browser.eval(
        "document.querySelectorAll('#clickup-chat-log .chat-msg-ticket .code-block').length"
    ) >= 1, "fenced technical notes must render as a code block, not raw backticks"


def test_flow_a_subticket_is_marked_as_such(browser):
    _generate_from_chat(browser)

    badges = browser.eval(
        "[...document.querySelectorAll('#clickup-chat-log .chat-msg-ticket .badge')].map(b => b.textContent)"
    )
    assert "subticket" in badges, "a ticket with a parent must be visibly nested"


def test_flow_chat_ticket_can_be_added_to_the_tracker(browser):
    _pick_destination(browser)
    _generate_from_chat(browser)
    browser.click("#clickup-chat-log .chat-ticket-actions .btn-success")
    browser.wait_for(
        "document.querySelectorAll('#clickup-chat-log .ticket-status-ok').length >= 1", timeout_s=20
    )

    status = browser.eval("document.querySelector('#clickup-chat-log .ticket-status-ok').textContent")
    assert "Added to ClickUp" in status and "new-1" in status


def test_flow_chat_ticket_can_be_discarded_instead(browser):
    _pick_destination(browser)
    _generate_from_chat(browser)
    browser.eval(
        "[...document.querySelectorAll('#clickup-chat-log .chat-ticket-actions button')]"
        ".find(b => b.textContent === 'Discard').click(); return true;"
    )
    browser.wait_for("document.querySelector('#clickup-chat-log .chat-msg-ticket').textContent.includes('Discarded')")


def test_flow_adding_without_a_destination_is_refused(browser):
    # Error prevention: creating into nowhere would fail server-side.
    _generate_from_chat(browser)
    browser.click("#clickup-chat-log .chat-ticket-actions .btn-success")
    browser.wait_for("!document.getElementById('clickup-generate-error').hidden")

    assert "step 1" in browser.eval("document.getElementById('clickup-generate-error').textContent")


@pytest.mark.parametrize("tracker", TRACKERS)
def test_flow_chat_works_identically_on_both_trackers(browser, tracker):
    _generate_from_chat(browser, tracker)

    assert "Endpoint de personas" in browser.eval(
        f"document.querySelector('#{tracker}-chat-log .chat-msg-ticket').textContent"
    )


# ===========================================================================
# FLOW: QA review — analyse, persist, bulk sweep
# ===========================================================================


def _run_qa_review(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-qa")
    label = browser.eval("document.querySelector('#clickup-qa-ticket-options option').value")
    browser.type_into("#clickup-qa-ticket", label)
    browser.click("#clickup-qa-analyze-btn")
    browser.wait_for("!document.getElementById('clickup-qa-result').hidden", timeout_s=20)


def test_flow_qa_review_renders_the_verdict_and_evidence(browser):
    _run_qa_review(browser)

    result = browser.eval("document.getElementById('clickup-qa-result').textContent")
    assert "major" in result and "FAIL" in result
    assert "Verificar validacion" in result
    assert "/erp/talent/people" in result, "the checked route is evidence and must be shown"
    assert "200" in result, "the HTTP status is evidence and must be shown"


def test_flow_qa_review_is_a_dry_run_until_saved(browser):
    _run_qa_review(browser)

    assert "Preview only" in browser.eval("document.getElementById('clickup-qa-result').textContent")
    assert browser.visible("#clickup-qa-result .btn-success"), "there must be a way to save it"


def test_flow_qa_result_can_be_saved_as_a_finding(browser):
    _run_qa_review(browser)
    browser.click("#clickup-qa-result .btn-success")
    browser.wait_for("!document.getElementById('clickup-qa-success').hidden", timeout_s=20)

    assert "Finding #1 saved" in browser.eval("document.getElementById('clickup-qa-success').textContent")


def test_flow_qa_bulk_sweep_lists_every_ticket_with_its_proposed_move(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-qa")
    browser.type_into("#clickup-qa-status-fail", "done")
    browser.click("#clickup-qa-bulk-run-btn")
    browser.wait_for("!document.getElementById('clickup-qa-bulk-results').hidden", timeout_s=25)

    row = browser.eval("document.querySelector('#clickup-qa-bulk-table tbody tr').textContent")
    assert "major" in row and "FAIL" in row
    assert "done" in row, "the status the ticket would move to must be previewed"


def test_flow_qa_bulk_results_can_be_committed(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-qa")
    browser.click("#clickup-qa-bulk-run-btn")
    browser.wait_for("!document.getElementById('clickup-qa-bulk-results').hidden", timeout_s=25)
    browser.click("#clickup-qa-bulk-confirm-btn")
    browser.wait_for("!document.getElementById('clickup-qa-success').hidden", timeout_s=20)

    assert "Saved 1" in browser.eval("document.getElementById('clickup-qa-success').textContent")


# ===========================================================================
# FLOW: module identification
# ===========================================================================


def _check_module(browser, bulk=False):
    _pick_destination(browser)
    browser.click("#clickup-subtab-module")
    if not bulk:
        label = browser.eval("document.querySelector('#clickup-module-ticket-options option').value")
        browser.type_into("#clickup-module-ticket", label)
    browser.type_into("#clickup-module-name", "Personas")
    browser.type_into("#clickup-module-context", "El modulo administra el registro maestro de personas.")
    browser.click(f"#clickup-module-check-{'all-' if bulk else ''}btn")


def test_flow_module_check_renders_a_reasoned_verdict(browser):
    _check_module(browser)
    browser.wait_for("!document.getElementById('clickup-module-result').hidden", timeout_s=20)

    result = browser.eval("document.getElementById('clickup-module-result').textContent")
    assert "Belongs to this module" in result
    assert "88%" in result, "confidence must be shown"
    assert "Modifica la ficha" in result, "the reasoning must be shown"
    assert "Ficha de persona" in result, "the overlap evidence must be shown"


def test_flow_module_sweep_answers_which_tickets_align(browser):
    _check_module(browser, bulk=True)
    browser.wait_for("!document.getElementById('clickup-module-bulk-results').hidden", timeout_s=25)

    aligned = browser.eval("document.getElementById('clickup-module-aligned').textContent")
    assert "Endpoint de personas" in aligned
    assert "Formulario de alta" not in aligned, "an unrelated ticket must not be listed as aligned"

    summary = browser.eval("document.getElementById('clickup-module-summary').textContent")
    assert "1 belong to this module" in summary and "1 do not" in summary


def test_flow_module_sweep_orders_aligned_tickets_first(browser):
    _check_module(browser, bulk=True)
    browser.wait_for("!document.getElementById('clickup-module-bulk-results').hidden", timeout_s=25)

    first = browser.eval("document.querySelector('#clickup-module-bulk-table tbody tr').textContent")
    assert "Endpoint de personas" in first


# ===========================================================================
# FLOW: modify ticket — reformat preview, apply, revert
# ===========================================================================


def _reformat(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-modify")
    label = browser.eval("document.querySelector('#clickup-reformat-ticket-options option').value")
    browser.type_into("#clickup-reformat-ticket", label)
    browser.click("#clickup-reformat-btn")
    browser.wait_for("!document.getElementById('clickup-reformat-result').hidden", timeout_s=20)


def test_flow_reformat_shows_before_and_after_side_by_side(browser):
    _reformat(browser)

    assert browser.eval("document.getElementById('clickup-reformat-old-title').textContent") == "endpoint personas"
    assert "hay que hacer el endpoint" in browser.eval(
        "document.getElementById('clickup-reformat-old-body').textContent"
    )
    assert browser.eval("document.getElementById('clickup-reformat-new-title').textContent") == (
        "TAB-01 | Endpoint de personas"
    )
    assert "USER STORY" in browser.eval("document.getElementById('clickup-reformat-new-body').textContent")


def test_flow_reformat_is_not_written_until_confirmed(browser):
    _reformat(browser)

    assert browser.visible("#clickup-reformat-apply-btn")
    assert browser.eval("document.getElementById('clickup-reformat-success').hidden"), (
        "nothing should be reported as saved before confirming"
    )


def test_flow_reformat_can_be_applied(browser):
    _reformat(browser)
    browser.click("#clickup-reformat-apply-btn")
    browser.wait_for("!document.getElementById('clickup-reformat-success').hidden", timeout_s=20)

    message = browser.eval("document.getElementById('clickup-reformat-success').textContent")
    assert "Updated" in message and "undo this below" in message


def test_flow_reformat_can_be_discarded(browser):
    _reformat(browser)
    browser.click("#clickup-reformat-discard-btn")

    assert not browser.visible("#clickup-reformat-result")


def test_flow_a_reformatted_ticket_can_be_reverted(browser):
    browser.click("#clickup-subtab-modify")
    browser.wait_for("!document.getElementById('clickup-revert-panel').hidden")
    browser.type_into("#clickup-revert-select", "task-1")
    browser.click("#clickup-revert-btn")
    browser.wait_for("!document.getElementById('clickup-reformat-success').hidden", timeout_s=20)

    assert "Restored" in browser.eval("document.getElementById('clickup-reformat-success').textContent")


# ===========================================================================
# FLOW: findings — report and close
# ===========================================================================


def test_flow_a_finding_can_be_reported_manually(browser):
    browser.click("#clickup-subtab-findings")
    browser.eval("document.querySelector('#clickup-sub-findings details.advanced').open = true; return true;")
    browser.wait_for("document.querySelectorAll('#clickup-report-project option').length > 1")
    browser.type_into("#clickup-report-project", "gru-po")
    browser.type_into("#clickup-report-route", "/erp/talent/people")
    browser.type_into("#clickup-report-observation", "El formulario no valida el email")
    browser.click("#clickup-report-submit")
    browser.wait_for("document.getElementById('clickup-report-status').textContent.includes('reported')")


def test_flow_reporting_a_finding_requires_every_field(browser):
    # Without a destination there is no default project, so the guard must fire
    # rather than posting an incomplete finding.
    browser.click("#clickup-subtab-findings")
    browser.eval("document.querySelector('#clickup-sub-findings details.advanced').open = true; return true;")
    browser.type_into("#clickup-report-route", "/erp/talent/people")
    browser.click("#clickup-report-submit")

    status = browser.eval("document.getElementById('clickup-report-status')")
    assert "required" in browser.eval("document.getElementById('clickup-report-status').textContent")
    assert browser.eval("document.getElementById('clickup-report-status').className") == "error-text"


def test_flow_a_finding_can_be_closed_with_a_correction_note(browser):
    browser.click("#clickup-subtab-findings")
    browser.wait_for("document.querySelectorAll('#clickup-findings-table tbody tr').length > 0")
    browser.type_into("#clickup-findings-table tbody tr td:last-child input", "corregido en el sprint 2")
    browser.eval(
        "document.querySelector('#clickup-findings-table tbody tr td:last-child button').click(); return true;"
    )
    browser.wait_for("document.querySelectorAll('#clickup-findings-table tbody tr').length > 0")


def test_flow_closing_requires_a_correction_note(browser):
    browser.click("#clickup-subtab-findings")
    browser.wait_for("document.querySelectorAll('#clickup-findings-table tbody tr').length > 0")
    browser.eval(
        "document.querySelector('#clickup-findings-table tbody tr td:last-child button').click(); return true;"
    )

    assert "Describe the correction" in browser.eval(
        "document.querySelector('#clickup-findings-table tbody tr td:last-child .error-text').textContent"
    )


# ===========================================================================
# FLOW: project base URL (drives real QA evidence capture)
# ===========================================================================


def test_flow_a_project_base_url_can_be_saved(browser):
    _pick_destination(browser)
    browser.click("#clickup-subtab-qa")
    browser.type_into("#clickup-qa-base-url", "https://app.example.com")
    browser.click("#clickup-qa-base-url-save")
    browser.wait_for("document.getElementById('clickup-qa-base-url-status').textContent === 'Saved.'")


def test_flow_saving_a_base_url_without_a_destination_is_refused(browser):
    browser.click("#clickup-subtab-qa")
    browser.type_into("#clickup-qa-base-url", "https://app.example.com")
    browser.click("#clickup-qa-base-url-save")

    assert "step 1" in browser.eval("document.getElementById('clickup-qa-base-url-status').textContent")
