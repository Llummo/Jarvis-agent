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


def test_shell_exposes_both_trackers(browser):
    assert browser.eval("[...document.querySelectorAll('.tab-button')].map(b => b.textContent)") == [
        "ClickUp",
        "Linear",
    ]


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
# Module: ticket generation — document vs chat
# ===========================================================================


def test_document_mode_is_the_default(browser):
    assert browser.visible("#clickup-mode-document")
    assert not browser.visible("#clickup-mode-chat")


def test_switching_to_chat_swaps_the_panels(browser):
    browser.click('#clickup-mode-switch button[data-mode="chat"]')

    assert browser.visible("#clickup-mode-chat")
    assert not browser.visible("#clickup-mode-document")


def test_the_document_form_accepts_only_supported_files(browser):
    accept = browser.eval("document.getElementById('clickup-file-input').accept")
    assert ".md" in accept and ".pdf" in accept


# ===========================================================================
# Module: chat (usability heuristics)
# ===========================================================================


def _open_chat(browser, tracker="clickup"):
    if tracker == "linear":
        browser.click('.tab-button[data-tab="linear"]')
    browser.click(f'#{tracker}-mode-switch button[data-mode="chat"]')


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
    # The stub has no POST route, so the request fails -- which is exactly the
    # recovery path worth testing.
    _open_chat(browser)
    browser.type_into("#clickup-chat-input", "algo que va a fallar")
    browser.press_enter("#clickup-chat-input")
    browser.wait_for(
        "document.querySelectorAll('#clickup-chat-log .chat-msg-error').length === 1", timeout_s=20
    )

    assert browser.visible("#clickup-chat-log .chat-retry"), "an error must offer a way to recover"


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
