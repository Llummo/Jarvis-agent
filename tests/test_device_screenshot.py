import pytest

from meta_harness.mcp_server.device_screenshot import (
    ScreenshotDeviceError,
    screenshot_desktop,
    screenshot_device,
)


class Result:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_screenshot_device_raises_when_adb_missing(monkeypatch):
    monkeypatch.setattr("meta_harness.mcp_server.device_screenshot.shutil.which", lambda name: None)

    with pytest.raises(ScreenshotDeviceError, match="adb not found on PATH"):
        screenshot_device()


def test_screenshot_device_calls_adb_correctly_when_present(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        "meta_harness.mcp_server.device_screenshot.shutil.which",
        lambda name: "/usr/bin/adb" if name == "adb" else None,
    )

    def fake_run(command, capture_output):
        calls.append(list(command))
        return Result(stdout=b"\x89PNGdata")

    monkeypatch.setattr("meta_harness.mcp_server.device_screenshot.subprocess.run", fake_run)

    output = tmp_path / "device.png"
    result = screenshot_device(output_path=output)

    assert calls == [["/usr/bin/adb", "exec-out", "screencap", "-p"]]
    assert result == output
    assert output.read_bytes() == b"\x89PNGdata"


def test_screenshot_device_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.mcp_server.device_screenshot.shutil.which", lambda name: "/usr/bin/adb"
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.device_screenshot.subprocess.run",
        lambda *a, **k: Result(returncode=1, stderr=b"no device"),
    )

    with pytest.raises(ScreenshotDeviceError, match="no device"):
        screenshot_device(output_path=tmp_path / "device.png")


def test_screenshot_device_raises_on_empty_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_harness.mcp_server.device_screenshot.shutil.which", lambda name: "/usr/bin/adb"
    )
    monkeypatch.setattr(
        "meta_harness.mcp_server.device_screenshot.subprocess.run", lambda *a, **k: Result(stdout=b"")
    )

    with pytest.raises(ScreenshotDeviceError, match="is a device connected"):
        screenshot_device(output_path=tmp_path / "device.png")


def test_screenshot_desktop_raises_when_not_macos(monkeypatch):
    monkeypatch.setattr("meta_harness.mcp_server.device_screenshot.platform.system", lambda: "Linux")

    with pytest.raises(ScreenshotDeviceError, match="macOS-only"):
        screenshot_desktop()


def test_screenshot_desktop_raises_when_screencapture_missing(monkeypatch):
    monkeypatch.setattr("meta_harness.mcp_server.device_screenshot.platform.system", lambda: "Darwin")
    monkeypatch.setattr("meta_harness.mcp_server.device_screenshot.shutil.which", lambda name: None)

    with pytest.raises(ScreenshotDeviceError, match="screencapture not found"):
        screenshot_desktop()


def test_screenshot_desktop_calls_screencapture_when_macos(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr("meta_harness.mcp_server.device_screenshot.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "meta_harness.mcp_server.device_screenshot.shutil.which",
        lambda name: "/usr/sbin/screencapture" if name == "screencapture" else None,
    )

    def fake_run(command, capture_output):
        calls.append(list(command))
        return Result()

    monkeypatch.setattr("meta_harness.mcp_server.device_screenshot.subprocess.run", fake_run)

    output = tmp_path / "desktop.png"
    result = screenshot_desktop(output_path=output)

    assert calls == [["/usr/sbin/screencapture", "-x", str(output)]]
    assert result == output
