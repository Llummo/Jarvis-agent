"""What a ticket looked like before the harness reformatted it.

Reformatting overwrites a ticket's title and description in place. That is
destructive to text a person wrote, so the previous version is saved first
and can be restored — the harness should never be a one-way door over
someone else's work.

JSON-backed with atomic writes, same pattern as project_config.py.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


def default_path() -> Path:
    return Path(__file__).resolve().parent.parent / "qa" / "reformat_history.json"


def _key(tracker: str, ticket_id: str) -> str:
    return f"{tracker}:{ticket_id}"


class ReformatHistoryStore:
    """One saved previous version per ticket.

    Only the most recent pre-reformat state is kept: reverting is meant to
    undo the change the user just previewed and applied, not to be a full
    version history.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path if path is not None else default_path()

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A corrupt history must never block a reformat; losing undo is
            # bad, refusing to work is worse.
            return {}

    def _save(self, data: Dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp", prefix=".reformat_")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, self.path)
        except BaseException:
            os.unlink(tmp_path)
            raise

    def remember(
        self,
        ticket_id: str,
        tracker: str,
        *,
        title: str,
        description: str,
        new_title: str = "",
    ) -> dict:
        """Record the version that is about to be overwritten."""
        entry = {
            "ticket_id": ticket_id,
            "tracker": tracker,
            "title": title,
            "description": description,
            "new_title": new_title,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        data = self._load()
        data[_key(tracker, ticket_id)] = entry
        self._save(data)
        return entry

    def get(self, ticket_id: str, tracker: str) -> Optional[dict]:
        return self._load().get(_key(tracker, ticket_id))

    def forget(self, ticket_id: str, tracker: str) -> None:
        """Drop a saved version — called once it has been restored, so the
        UI stops offering to undo something already undone."""
        data = self._load()
        if data.pop(_key(tracker, ticket_id), None) is not None:
            self._save(data)

    def list_for(self, tracker: Optional[str] = None) -> List[dict]:
        """Every ticket that can currently be reverted, newest first."""
        entries = [
            entry
            for entry in self._load().values()
            if tracker is None or entry.get("tracker") == tracker
        ]
        return sorted(entries, key=lambda e: e.get("saved_at", ""), reverse=True)
