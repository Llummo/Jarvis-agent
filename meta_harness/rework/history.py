"""What a rework run did, kept so it can be undone.

A run moves real tickets: originals go to Cancelled, replacements appear under
a parent. Undoing that needs facts the board no longer carries — above all,
which column each original came from, which is lost the moment it is moved.

So the run is recorded before and during execution rather than reconstructed
afterwards. Same shape as reformat_history.py: JSON, atomic writes, under the
gitignored state directory because it holds real ticket ids.

Only completed runs are stored, newest first, and a run is dropped once it has
been undone — the UI must not offer to undo something already undone.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Undo is for the run you just did and immediately regretted. Keeping every
# run forever would grow without bound and offer to revert work from weeks ago
# that other people have since built on.
MAX_RUNS = 20


def default_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "qa" / "rework_history.json"


class ReworkHistoryStore:
    """Completed rework runs that can still be undone."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path if path is not None else default_path()

    def _load(self) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A corrupt history must never block a rework; losing undo is bad,
            # refusing to work is worse.
            return []
        return data if isinstance(data, list) else []

    def _save(self, runs: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(runs, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp", prefix=".rework_")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, self.path)
        except BaseException:
            os.unlink(tmp_path)
            raise

    def remember(self, run: dict) -> dict:
        """Record a completed run, newest first."""
        runs = [existing for existing in self._load() if existing.get("run_id") != run.get("run_id")]
        runs.insert(0, run)
        self._save(runs[:MAX_RUNS])
        return run

    def get(self, run_id: str) -> Optional[dict]:
        for run in self._load():
            if run.get("run_id") == run_id:
                return run
        return None

    def forget(self, run_id: str) -> None:
        runs = self._load()
        remaining = [run for run in runs if run.get("run_id") != run_id]
        if len(remaining) != len(runs):
            self._save(remaining)

    def list_runs(self, *, team_id: Optional[str] = None) -> List[dict]:
        """Runs that can still be undone, newest first."""
        return [
            run
            for run in self._load()
            if team_id is None or run.get("team_id") == team_id
        ]


def new_run_id(started_at: Optional[datetime] = None) -> str:
    moment = started_at or datetime.now(timezone.utc)
    return moment.strftime("rework-%Y%m%dT%H%M%S%fZ")


def build_run_record(
    *,
    run_id: str,
    team_id: str,
    parent_issue_id: str,
    outcomes: List,
    cancelled_state_id: str,
) -> Dict:
    """The minimum needed to reverse a run.

    Only items that actually produced a replacement are recorded: an item that
    failed changed nothing, so there is nothing about it to undo.
    """
    return {
        "run_id": run_id,
        "team_id": team_id,
        "parent_issue_id": parent_issue_id,
        "cancelled_state_id": cancelled_state_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "items": [
            {
                "issue_id": outcome.issue_id,
                "identifier": outcome.identifier,
                "original_title": outcome.original_title,
                "created_issue_id": outcome.created_issue_id,
                "created_title": outcome.created_title,
                "cancelled": outcome.cancelled,
                # The column the original sat in before it was cancelled.
                # Without this there is nothing to restore it to.
                "previous_state_id": outcome.previous_state_id,
                "previous_state_name": outcome.previous_state_name,
            }
            for outcome in outcomes
            if outcome.created_issue_id
        ],
    }
