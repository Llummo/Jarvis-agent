"""List and read images captured under qa/screenshots/."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


class ImageNotFoundError(LookupError):
    """No image with the given name, or the name escapes the screenshots directory."""


def screenshots_dir() -> Path:
    """Directory where captured screenshots are stored."""
    return Path(__file__).resolve().parent.parent.parent / "qa" / "screenshots"


@dataclass
class ImageInfo:
    """Metadata for one captured image."""

    name: str
    path: str
    size_bytes: int
    modified_at: float


def list_images(*, directory: Optional[Path] = None) -> List[ImageInfo]:
    """List images captured under the screenshots directory."""
    directory = directory if directory is not None else screenshots_dir()
    if not directory.exists():
        return []
    infos = []
    for path in sorted(directory.iterdir()):
        if path.is_file():
            stat = path.stat()
            infos.append(
                ImageInfo(name=path.name, path=str(path), size_bytes=stat.st_size, modified_at=stat.st_mtime)
            )
    return infos


def read_image(name: str, *, directory: Optional[Path] = None) -> bytes:
    """Read one image's bytes by filename, guarded against path traversal."""
    directory = (directory if directory is not None else screenshots_dir()).resolve()
    candidate = (directory / name).resolve()
    if not candidate.is_relative_to(directory):
        raise ImageNotFoundError(f"'{name}' is outside the screenshots directory")
    if not candidate.exists() or not candidate.is_file():
        raise ImageNotFoundError(f"No image named '{name}' in {directory}")
    return candidate.read_bytes()
