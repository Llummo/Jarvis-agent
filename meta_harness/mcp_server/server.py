"""MCP server exposing QA findings tracking and screenshot capture as tools.

Reproduces Seyren's original toolkit (report_qa_issue, list_qa_issues,
screenshot_url, screenshot_device, screenshot_desktop, list_images,
read_image) plus close_qa_issue. Every tool is a thin wrapper around the
same functions the CLI and web UI use — this server owns no logic of its
own.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

from meta_harness.mcp_server.cdp_screenshot import capture_screenshot
from meta_harness.mcp_server.device_screenshot import screenshot_desktop as _screenshot_desktop
from meta_harness.mcp_server.device_screenshot import screenshot_device as _screenshot_device
from meta_harness.mcp_server.images import list_images as _list_images
from meta_harness.mcp_server.images import read_image as _read_image
from meta_harness.qa_findings import close_qa_issue as _close_qa_issue
from meta_harness.qa_findings import list_qa_issues as _list_qa_issues
from meta_harness.qa.session import SESSION as _SESSION
from meta_harness.qa_findings import report_qa_issue as _report_qa_issue

mcp = FastMCP("meta-harness-qa")


@mcp.tool()
def report_qa_issue(
    project: str,
    route: str,
    observation: str,
    severity: str,
    screenshot_path: Optional[str] = None,
    clickup_list_id: Optional[str] = None,
) -> dict:
    """Report a QA finding. Critical severity auto-creates a linked ClickUp ticket."""
    finding = _report_qa_issue(
        project, route, observation, severity,
        screenshot_path=screenshot_path, clickup_list_id=clickup_list_id,
    )
    return asdict(finding)


@mcp.tool()
def list_qa_issues(
    project: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
) -> list:
    """List QA findings, optionally filtered by project/severity/status."""
    return [asdict(finding) for finding in _list_qa_issues(project=project, severity=severity, status=status)]


@mcp.tool()
def close_qa_issue(finding_id: int, correction_note: str) -> dict:
    """Close a QA finding with a correction note describing the fix."""
    return asdict(_close_qa_issue(finding_id, correction_note))


@mcp.tool()
def screenshot_url(url: str, timeout_s: float = 20.0) -> dict:
    """Capture a full-page screenshot of a URL via the Chrome DevTools Protocol."""
    path = capture_screenshot(url, timeout_s=timeout_s)
    return {"path": str(path)}


@mcp.tool()
def screenshot_device() -> dict:
    """Capture a screenshot from a connected Android device via adb."""
    path = _screenshot_device()
    return {"path": str(path)}


@mcp.tool()
def screenshot_desktop() -> dict:
    """Capture a screenshot of the desktop (macOS only, via screencapture)."""
    path = _screenshot_desktop()
    return {"path": str(path)}


@mcp.tool()
def list_images() -> list:
    """List images captured under qa/screenshots/."""
    return [asdict(info) for info in _list_images()]


@mcp.tool()
def read_image(name: str) -> Image:
    """Read one captured image by filename."""
    data = _read_image(name)
    image_format = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
    return Image(data=data, format=image_format)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


# --- Live browser for QA exploration ----------------------------------------
# These let the model look at the app before writing a test plan, rather than
# guessing steps from the ticket text alone. The session persists across calls
# because a form cannot be filled in one.


@mcp.tool()
def qa_browser_open(environment: str = "local", ui_url: str = "") -> dict:
    """Open a browser against 'local' or 'qa'. Production is refused."""
    return _SESSION.start(environment, ui_url=ui_url or None)


@mcp.tool()
def qa_browser_goto(route: str) -> dict:
    """Navigate to a route on the open environment, e.g. '/erp/talent/people'."""
    return _SESSION.goto(route)


@mcp.tool()
def qa_browser_read() -> dict:
    """Read the visible text of the current page."""
    return _SESSION.read()


@mcp.tool()
def qa_browser_find(text: str) -> dict:
    """Check whether something with this visible text exists, and where."""
    return _SESSION.find(text)


@mcp.tool()
def qa_browser_click(text: str) -> dict:
    """Click the element whose visible text matches."""
    return _SESSION.click_text(text)


@mcp.tool()
def qa_browser_type(label: str, value: str) -> dict:
    """Type into the field with this visible label."""
    return _SESSION.type_label(label, value)


@mcp.tool()
def qa_browser_network() -> list:
    """The API calls the page has made since the last navigation.

    This is how a plan learns which endpoint a screen really uses.
    """
    return _SESSION.network()


@mcp.tool()
def qa_browser_screenshot() -> dict:
    """Capture the current page."""
    return _SESSION.screenshot()


@mcp.tool()
def qa_browser_close() -> dict:
    """Close the browser and free the session."""
    return _SESSION.stop()
