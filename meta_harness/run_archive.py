"""Persist playbook flow runs so they can be replayed later.

Mirrors frontier.py's JSON-backed, atomic-write persistence, applied to
playbook flow runs instead of benchmark candidates.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


def runs_dir() -> Path:
    """Return the directory where per-agent run archives are stored."""
    return Path(__file__).resolve().parent.parent / "runs"


@dataclass
class RunStepRecord:
    """A single recorded flow step."""

    command: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class RunRecord:
    """One recorded execution of an agent's flow against a subject."""

    run_id: str
    agent: str
    subject_id: Optional[str]
    started_at: str
    ok: bool
    steps: List[RunStepRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "subject_id": self.subject_id,
            "started_at": self.started_at,
            "ok": self.ok,
            "steps": [asdict(step) for step in self.steps],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RunRecord":
        return cls(
            run_id=payload["run_id"],
            agent=payload["agent"],
            subject_id=payload.get("subject_id"),
            started_at=payload["started_at"],
            ok=payload["ok"],
            steps=[RunStepRecord(**step) for step in payload.get("steps", [])],
        )


class RunArchive:
    """JSON-backed archive of flow runs for one agent."""

    def __init__(self, agent: str, *, directory: Optional[Path] = None):
        self.agent = agent
        self.path = (directory if directory is not None else runs_dir()) / f"{agent}.json"

    def load(self) -> List[RunRecord]:
        """Load every recorded run for this agent."""
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return [RunRecord.from_dict(entry) for entry in payload]

    def save(self, records: List[RunRecord]) -> None:
        """Save all records atomically via temp file + rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps([record.to_dict() for record in records], indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp", prefix=".runs_")
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

    def append(self, record: RunRecord) -> RunRecord:
        """Add one run record to the archive."""
        records = self.load()
        records.append(record)
        self.save(records)
        return record

    def get(self, run_id: str) -> RunRecord:
        """Look up one recorded run by id."""
        for record in self.load():
            if record.run_id == run_id:
                return record
        raise FileNotFoundError(f"No recorded run '{run_id}' for agent '{self.agent}'.")
