import asyncio

from meta_harness.mcp_server.server import (
    close_qa_issue,
    list_qa_issues,
    mcp,
    report_qa_issue,
)
from meta_harness.qa_findings import QAFindingStore

# Seyren's original toolkit. Kept as its own set so adding tools can never
# quietly remove one of these.
ORIGINAL_TOOL_NAMES = {
    "report_qa_issue",
    "list_qa_issues",
    "close_qa_issue",
    "screenshot_url",
    "screenshot_device",
    "screenshot_desktop",
    "list_images",
    "read_image",
}

# The live browser session, added for ticket-driven browser tests.
BROWSER_TOOL_NAMES = {
    "qa_browser_open",
    "qa_browser_goto",
    "qa_browser_read",
    "qa_browser_find",
    "qa_browser_click",
    "qa_browser_type",
    "qa_browser_network",
    "qa_browser_screenshot",
    "qa_browser_close",
}

EXPECTED_TOOL_NAMES = ORIGINAL_TOOL_NAMES | BROWSER_TOOL_NAMES


def test_the_original_toolkit_is_still_complete():
    """Adding tools must never drop one of the originals."""
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert ORIGINAL_TOOL_NAMES <= names


def test_exactly_the_expected_tools_are_registered():
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == EXPECTED_TOOL_NAMES


def test_the_browser_session_is_exposed():
    """Without these the model can only read a ticket, never look at the app."""
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert BROWSER_TOOL_NAMES <= names


def test_report_and_list_qa_issue_tools_round_trip(tmp_path, monkeypatch):
    # qa_findings.report_qa_issue/list_qa_issues instantiate QAFindingStore()
    # themselves when no store is passed in — patch that constructor so the
    # MCP tools (which don't expose a store parameter) hit a tmp_path DB.
    store = QAFindingStore(tmp_path / "findings.db")
    monkeypatch.setattr("meta_harness.qa_findings.QAFindingStore", lambda *a, **kw: store)

    reported = report_qa_issue("sigo-front", "/checkout", "misaligned button", "minor")
    listed = list_qa_issues()

    assert reported["project"] == "sigo-front"
    assert reported["severity"] == "minor"
    assert len(listed) == 1
    assert listed[0]["route"] == "/checkout"


def test_close_qa_issue_tool(tmp_path, monkeypatch):
    store = QAFindingStore(tmp_path / "findings.db")
    monkeypatch.setattr("meta_harness.qa_findings.QAFindingStore", lambda *a, **kw: store)

    reported = report_qa_issue("p", "/r", "obs", "minor")
    closed = close_qa_issue(reported["id"], "fixed it")

    assert closed["status"] == "closed"
    assert closed["correction_note"] == "fixed it"
