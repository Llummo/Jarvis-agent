import pytest

from meta_harness.mcp_server.images import ImageNotFoundError, list_images, read_image


def test_list_images_empty_when_dir_missing(tmp_path):
    assert list_images(directory=tmp_path / "does-not-exist") == []


def test_list_images_lists_files_sorted(tmp_path):
    (tmp_path / "b.png").write_bytes(b"bbb")
    (tmp_path / "a.png").write_bytes(b"aa")

    infos = list_images(directory=tmp_path)

    assert [info.name for info in infos] == ["a.png", "b.png"]
    assert infos[0].size_bytes == 2
    assert infos[1].size_bytes == 3


def test_list_images_ignores_subdirectories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "shot.png").write_bytes(b"x")

    infos = list_images(directory=tmp_path)

    assert [info.name for info in infos] == ["shot.png"]


def test_read_image_returns_bytes(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\ndata")

    data = read_image("shot.png", directory=tmp_path)

    assert data == b"\x89PNG\r\n\x1a\ndata"


def test_read_image_missing_raises(tmp_path):
    with pytest.raises(ImageNotFoundError, match="No image named"):
        read_image("missing.png", directory=tmp_path)


def test_read_image_rejects_path_traversal(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("do not read me")

    with pytest.raises(ImageNotFoundError, match="outside the screenshots directory"):
        read_image("../secret.txt", directory=tmp_path)


def test_read_image_rejects_absolute_path_outside_dir(tmp_path):
    secret = tmp_path.parent / "secret2.txt"
    secret.write_text("do not read me either")

    with pytest.raises(ImageNotFoundError, match="outside the screenshots directory"):
        read_image(str(secret), directory=tmp_path)
