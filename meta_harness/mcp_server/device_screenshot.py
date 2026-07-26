"""Capture screenshots from a connected Android device or a macOS desktop.

Both are real, subprocess-driven implementations, but neither can be
verified end-to-end on this Linux dev machine (no adb, not macOS). They
fail loudly and clearly when the required binary/platform isn't available,
rather than silently no-op'ing.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from meta_harness.mcp_server.images import screenshots_dir


class ScreenshotDeviceError(RuntimeError):
    """Raised when device/desktop screenshot capture fails or is unsupported here."""


def _default_output_path(prefix: str) -> Path:
    directory = screenshots_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{prefix}-{uuid.uuid4().hex[:8]}.png"


def screenshot_device(*, output_path: Optional[Path] = None) -> Path:
    """Capture a screenshot from a connected Android device via adb."""
    adb = shutil.which("adb")
    if not adb:
        raise ScreenshotDeviceError("adb not found on PATH; install Android platform-tools.")

    resolved_output = output_path if output_path is not None else _default_output_path("device")
    completed = subprocess.run([adb, "exec-out", "screencap", "-p"], capture_output=True)
    if completed.returncode != 0:
        raise ScreenshotDeviceError(
            f"adb screencap failed ({completed.returncode}): "
            f"{completed.stderr.decode(errors='replace').strip()}"
        )
    if not completed.stdout:
        raise ScreenshotDeviceError("adb screencap produced no output — is a device connected?")
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_bytes(completed.stdout)
    return resolved_output


def screenshot_desktop(*, output_path: Optional[Path] = None) -> Path:
    """Capture a screenshot of the desktop via macOS's screencapture."""
    if platform.system() != "Darwin":
        raise ScreenshotDeviceError(
            "screenshot_desktop is macOS-only (screencapture); this host is not macOS."
        )

    screencapture = shutil.which("screencapture")
    if not screencapture:
        raise ScreenshotDeviceError("screencapture not found on PATH.")

    resolved_output = output_path if output_path is not None else _default_output_path("desktop")
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run([screencapture, "-x", str(resolved_output)], capture_output=True)
    if completed.returncode != 0:
        raise ScreenshotDeviceError(
            f"screencapture failed ({completed.returncode}): "
            f"{completed.stderr.decode(errors='replace').strip()}"
        )
    return resolved_output
