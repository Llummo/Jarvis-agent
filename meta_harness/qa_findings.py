"""QA Review findings store: report, list, and close QA findings.

Reproduces the tracking layer of a deprecated internal QA tool (Seyren) on
this repo's own architecture: findings are persisted in SQLite, and a
critical-severity finding auto-escalates into a ClickUp correction ticket
linked back to the finding.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import meta_harness.clickup_bridge as clickup_bridge
import meta_harness.linear_bridge as linear_bridge

logger = logging.getLogger(__name__)

SEVERITIES = ("minor", "major", "critical")
STATUSES = ("open", "acknowledged", "closed")
TRACKERS = ("clickup", "linear")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_findings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project          TEXT NOT NULL,
    route            TEXT NOT NULL,
    observation      TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('minor', 'major', 'critical')),
    status           TEXT NOT NULL CHECK (status IN ('open', 'acknowledged', 'closed')) DEFAULT 'open',
    screenshot_path  TEXT,
    checked_route    TEXT,
    status_code      INTEGER,
    http_error       TEXT,
    correction_note  TEXT,
    clickup_task_id  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_findings_project  ON qa_findings(project);
CREATE INDEX IF NOT EXISTS idx_qa_findings_severity ON qa_findings(severity);
CREATE INDEX IF NOT EXISTS idx_qa_findings_status   ON qa_findings(status);
"""

# Columns added after the original schema — applied via ALTER TABLE for
# on-disk databases created before they existed, since CREATE TABLE IF NOT
# EXISTS above only takes effect for a brand-new file.
_MIGRATION_COLUMNS = {
    "checked_route": "TEXT",
    "status_code": "INTEGER",
    "http_error": "TEXT",
    "linear_issue_id": "TEXT",
}


class QAFindingNotFoundError(LookupError):
    """No finding with the given id."""


@dataclass
class QAFinding:
    """One QA review finding."""

    id: Optional[int]
    project: str
    route: str
    observation: str
    severity: str
    status: str = "open"
    screenshot_path: Optional[str] = None
    checked_route: Optional[str] = None
    status_code: Optional[int] = None
    http_error: Optional[str] = None
    correction_note: Optional[str] = None
    clickup_task_id: Optional[str] = None
    linear_issue_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "QAFinding":
        return cls(
            id=row["id"],
            project=row["project"],
            route=row["route"],
            observation=row["observation"],
            severity=row["severity"],
            status=row["status"],
            screenshot_path=row["screenshot_path"],
            checked_route=row["checked_route"],
            status_code=row["status_code"],
            http_error=row["http_error"],
            correction_note=row["correction_note"],
            clickup_task_id=row["clickup_task_id"],
            linear_issue_id=row["linear_issue_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def qa_dir() -> Path:
    """Return the directory where the QA findings database lives."""
    return Path(__file__).resolve().parent.parent / "qa"


def default_db_path() -> Path:
    """Default location of the QA findings database."""
    return qa_dir() / "findings.db"


class QAFindingStore:
    """SQLite-backed store of QA findings."""

    def __init__(self, db_path: Optional[Path] = None):
        self.path = db_path if db_path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(qa_findings)")}
        for column, coltype in _MIGRATION_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE qa_findings ADD COLUMN {column} {coltype}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, finding: QAFinding) -> QAFinding:
        """Insert a new finding, filling in id/created_at/updated_at."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO qa_findings
                    (project, route, observation, severity, status, screenshot_path,
                     checked_route, status_code, http_error, correction_note, clickup_task_id,
                     linear_issue_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding.project,
                    finding.route,
                    finding.observation,
                    finding.severity,
                    finding.status,
                    finding.screenshot_path,
                    finding.checked_route,
                    finding.status_code,
                    finding.http_error,
                    finding.correction_note,
                    finding.clickup_task_id,
                    finding.linear_issue_id,
                    now,
                    now,
                ),
            )
            return self.get(cursor.lastrowid, _conn=conn)

    def get(self, finding_id: int, *, _conn: Optional[sqlite3.Connection] = None) -> QAFinding:
        """Look up one finding by id."""
        conn = _conn if _conn is not None else self._connect()
        try:
            row = conn.execute("SELECT * FROM qa_findings WHERE id = ?", (finding_id,)).fetchone()
        finally:
            if _conn is None:
                conn.close()
        if row is None:
            raise QAFindingNotFoundError(f"No QA finding with id {finding_id}")
        return QAFinding.from_row(row)

    def list(
        self,
        *,
        project: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[QAFinding]:
        """List findings, optionally filtered by project/severity/status."""
        clauses = []
        params: list = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        query = "SELECT * FROM qa_findings"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [QAFinding.from_row(row) for row in rows]

    def update(self, finding_id: int, **fields) -> QAFinding:
        """Partially update a finding, bumping updated_at."""
        if not fields:
            return self.get(finding_id)
        now = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE qa_findings SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), now, finding_id),
            )
            return self.get(finding_id, _conn=conn)


def report_qa_issue(
    project: str,
    route: str,
    observation: str,
    severity: str,
    *,
    screenshot_path: Optional[str] = None,
    checked_route: Optional[str] = None,
    status_code: Optional[int] = None,
    http_error: Optional[str] = None,
    tracker: str = "clickup",
    clickup_list_id: Optional[str] = None,
    linear_team_id: Optional[str] = None,
    auto_escalate: bool = True,
    store: Optional[QAFindingStore] = None,
) -> QAFinding:
    """Register a QA finding.

    Critical findings are auto-acknowledged and, unless auto_escalate is
    False, get a correction ticket created in the same tracker the finding
    came from (`tracker`: "clickup" or "linear") and linked back to them.
    Escalating into Linear needs a `linear_team_id` (Linear issues always
    belong to a team) the same way ClickUp escalation can optionally take a
    `clickup_list_id`. A tracker failure never blocks the finding from
    being persisted.
    """
    if severity not in SEVERITIES:
        raise ValueError(f"Invalid severity '{severity}'; must be one of {SEVERITIES}")
    if tracker not in TRACKERS:
        raise ValueError(f"Invalid tracker '{tracker}'; must be one of {TRACKERS}")

    store = store if store is not None else QAFindingStore()
    status = "acknowledged" if severity == "critical" else "open"
    finding = store.create(
        QAFinding(
            id=None,
            project=project,
            route=route,
            observation=observation,
            severity=severity,
            status=status,
            screenshot_path=screenshot_path,
            checked_route=checked_route,
            status_code=status_code,
            http_error=http_error,
        )
    )

    if severity == "critical" and auto_escalate:
        escalation_title = f"[QA #{finding.id}] {project}: {route}"
        escalation_description = f"Severity: critical\nRoute: {route}\nObservation: {observation}"
        if tracker == "linear":
            if linear_team_id:
                try:
                    issue_id = linear_bridge.create_linear_issue(
                        linear_team_id, escalation_title, escalation_description,
                    )
                except linear_bridge.LinearIssueError as exc:
                    logger.warning("Linear issue creation failed for finding %s: %s", finding.id, exc)
                else:
                    finding = store.update(finding.id, linear_issue_id=issue_id)
        else:
            try:
                task_id = clickup_bridge.create_clickup_ticket(
                    name=escalation_title, description=escalation_description, list_id=clickup_list_id,
                )
            except clickup_bridge.ClickUpTicketError as exc:
                logger.warning("ClickUp ticket creation failed for finding %s: %s", finding.id, exc)
            else:
                finding = store.update(finding.id, clickup_task_id=task_id)

    return finding


def list_qa_issues(
    *,
    project: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    store: Optional[QAFindingStore] = None,
) -> List[QAFinding]:
    """List QA findings, optionally filtered by project/severity/status."""
    store = store if store is not None else QAFindingStore()
    return store.list(project=project, severity=severity, status=status)


def close_qa_issue(
    finding_id: int,
    correction_note: str,
    *,
    store: Optional[QAFindingStore] = None,
) -> QAFinding:
    """Close a finding with a correction note."""
    store = store if store is not None else QAFindingStore()
    finding = store.get(finding_id)
    if finding.status == "closed":
        raise ValueError(f"QA finding {finding_id} is already closed")
    return store.update(finding_id, status="closed", correction_note=correction_note)
