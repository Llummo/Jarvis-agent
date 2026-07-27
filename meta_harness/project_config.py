"""Per-project configuration -- currently just each project's base URL,
used by the QA review flow to actually check a route instead of only
reasoning about ticket text.

JSON-backed with atomic writes, same pattern as run_archive.py.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional


def default_path() -> Path:
    return Path(__file__).resolve().parent.parent / "qa" / "project_config.json"


class ProjectConfigStore:
    """Maps a project name to its base URL."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path if path is not None else default_path()

    def _load(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2, sort_keys=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp", prefix=".project_config_")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    def get_base_url(self, project: str) -> Optional[str]:
        return self._load().get(project) or None

    def set_base_url(self, project: str, base_url: str) -> None:
        data = self._load()
        data[project] = base_url
        self._save(data)

    def list_all(self) -> Dict[str, str]:
        return self._load()
