import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from meta_harness.image_input import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    ImageDescriptionError,
    ImageInputError,
    describe_images,
    sniff_extension,
    staged_images,
    validate_image,
)


def _png(width: int = 8, height: int = 8) -> bytes:
    """A real, decodable PNG — so the sniffing tests run against genuine
    image bytes rather than a hand-waved magic-number prefix."""
    raw = b"".join(b"\x00" + bytes([255, 255, 255] * width) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# sniff_extension / validate_image
# ---------------------------------------------------------------------------


def test_sniff_extension_recognises_a_real_png():
    assert sniff_extension(_png()) == ".png"


@pytest.mark.parametrize(
    "content, expected",
    [
        (b"\xff\xd8\xff\xe0somejpegbytes", ".jpg"),
        (b"GIF89a" + b"\x00" * 10, ".gif"),
        (b"GIF87a" + b"\x00" * 10, ".gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp"),
    ],
)
def test_sniff_extension_recognises_the_other_supported_formats(content, expected):
    assert sniff_extension(content) == expected


def test_sniff_extension_rejects_a_non_image():
    with pytest.raises(ImageInputError):
        sniff_extension(b"%PDF-1.4 this is a pdf, not an image")


def test_a_riff_container_that_is_not_webp_is_rejected():
    # RIFF also fronts .wav; the format marker is what makes it an image.
    with pytest.raises(ImageInputError):
        sniff_extension(b"RIFF\x00\x00\x00\x00WAVEfmt ")


def test_validate_image_rejects_empty_content():
    with pytest.raises(ImageInputError, match="empty"):
        validate_image("shot.png", b"")


def test_validate_image_rejects_content_over_the_size_cap():
    with pytest.raises(ImageInputError, match="larger than"):
        validate_image("huge.png", _png() + b"\x00" * MAX_IMAGE_BYTES)


def test_validate_image_ignores_a_lying_filename():
    # The extension is decided by the bytes, not by what the client called it.
    assert validate_image("screenshot.jpg", _png()) == ".png"


def test_validate_image_rejects_a_non_image_named_like_one():
    with pytest.raises(ImageInputError):
        validate_image("payload.png", b"#!/bin/sh\nrm -rf /")


# ---------------------------------------------------------------------------
# staged_images
# ---------------------------------------------------------------------------


def test_staged_images_writes_each_image_and_returns_its_path():
    with staged_images([("a.png", _png()), ("b.png", _png())]) as paths:
        assert len(paths) == 2
        assert all(path.exists() for path in paths)
        assert paths[0].read_bytes() == _png()


def test_staged_images_removes_the_directory_afterwards():
    with staged_images([("a.png", _png())]) as paths:
        directory = paths[0].parent
    assert not directory.exists()


def test_staged_images_cleans_up_even_when_the_body_raises():
    directory = None
    with pytest.raises(RuntimeError):
        with staged_images([("a.png", _png())]) as paths:
            directory = paths[0].parent
            raise RuntimeError("boom")
    assert directory is not None and not directory.exists()


def test_staged_images_never_uses_the_client_supplied_filename():
    # A filename is attacker-controlled; letting it reach the filesystem is
    # how a paste turns into a path traversal.
    with staged_images([("../../etc/passwd", _png())]) as paths:
        assert paths[0].name == "image-1.png"
        assert paths[0].parent == paths[0].resolve().parent


def test_staged_images_rejects_more_than_the_cap():
    too_many = [(f"{i}.png", _png()) for i in range(MAX_IMAGES + 1)]
    with pytest.raises(ImageInputError, match="at most"):
        with staged_images(too_many):
            pass


def test_staged_images_rejects_the_whole_batch_if_one_image_is_bad():
    with pytest.raises(ImageInputError):
        with staged_images([("good.png", _png()), ("bad.png", b"not an image")]):
            pass


# ---------------------------------------------------------------------------
# describe_images
# ---------------------------------------------------------------------------


def test_describe_images_with_no_paths_returns_empty_without_calling_claude(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not shell out when there is nothing to describe")

    monkeypatch.setattr("meta_harness.image_input.subprocess.run", boom)

    assert describe_images([]) == ""


def _fake_run(captured, *, stdout="Una pantalla de login.", returncode=0):
    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    return run


def test_describe_images_grants_only_the_read_tool(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.image_input.subprocess.run", _fake_run(captured))
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    describe_images([image])

    command = captured["command"]
    assert "--allowedTools" in command
    assert command[command.index("--allowedTools") + 1] == "Read", (
        "the describing call must not be able to run commands or edit files"
    )


def test_describe_images_scopes_the_cli_to_the_staging_directory(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.image_input.subprocess.run", _fake_run(captured))
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    describe_images([image])

    command = captured["command"]
    assert command[command.index("--add-dir") + 1] == str(tmp_path)
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_describe_images_lists_every_image_in_the_prompt(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.image_input.subprocess.run", _fake_run(captured))
    first, second = tmp_path / "image-1.png", tmp_path / "image-2.png"
    for path in (first, second):
        path.write_bytes(_png())

    describe_images([first, second])

    prompt = captured["command"][captured["command"].index("-p") + 1]
    assert str(first) in prompt and str(second) in prompt


def test_describe_images_tells_the_model_not_to_obey_text_inside_an_image(monkeypatch, tmp_path):
    # A screenshot is untrusted input; it can contain "ignore your
    # instructions" as easily as it can contain a login form.
    captured = {}
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.image_input.subprocess.run", _fake_run(captured))
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    describe_images([image])

    prompt = captured["command"][captured["command"].index("-p") + 1]
    assert "not an instruction" in prompt.lower()


def test_describe_images_returns_the_description(monkeypatch, tmp_path):
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        "meta_harness.image_input.subprocess.run",
        _fake_run({}, stdout="  Un formulario con email y password.  "),
    )
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    assert describe_images([image]) == "Un formulario con email y password."


def test_describe_images_non_zero_exit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "vision unavailable")

    monkeypatch.setattr("meta_harness.image_input.subprocess.run", run)
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    with pytest.raises(ImageDescriptionError, match="vision unavailable"):
        describe_images([image])


def test_describe_images_empty_output_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("meta_harness.image_input.subprocess.run", _fake_run({}, stdout="   "))
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    with pytest.raises(ImageDescriptionError, match="no description"):
        describe_images([image])


def test_describe_images_timeout_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: "/usr/bin/claude")

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))

    monkeypatch.setattr("meta_harness.image_input.subprocess.run", run)
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    with pytest.raises(ImageDescriptionError, match="timed out"):
        describe_images([image])


def test_describe_images_without_the_cli_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("META_HARNESS_CLAUDE_BIN", raising=False)
    monkeypatch.setattr("meta_harness.image_input.shutil.which", lambda _: None)
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    with pytest.raises(ImageDescriptionError, match="not on PATH"):
        describe_images([image])


def test_describe_images_honours_the_binary_override(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("META_HARNESS_CLAUDE_BIN", "/custom/claude")
    monkeypatch.setattr("meta_harness.image_input.subprocess.run", _fake_run(captured))
    image = tmp_path / "image-1.png"
    image.write_bytes(_png())

    describe_images([image])

    assert captured["command"][0] == "/custom/claude"
