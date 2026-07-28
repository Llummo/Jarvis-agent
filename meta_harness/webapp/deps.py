"""FastAPI dependency providers — overridable in tests via app.dependency_overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from meta_harness.project_config import ProjectConfigStore
from meta_harness.qa_findings import QAFindingStore


def get_qa_store() -> QAFindingStore:
    db_path = os.getenv("META_HARNESS_QA_DB_PATH")
    return QAFindingStore(Path(db_path).expanduser().resolve() if db_path else None)


def get_project_config_store() -> ProjectConfigStore:
    config_path = os.getenv("META_HARNESS_PROJECT_CONFIG_PATH")
    return ProjectConfigStore(Path(config_path).expanduser().resolve() if config_path else None)


def get_clickup_project_path() -> Optional[Path]:
    """None lets clickup_bridge resolve the p-harness checkout via the playbook."""
    return None


def get_linear_project_path() -> Optional[Path]:
    """None lets linear_bridge resolve the p-harness checkout via the playbook."""
    return None
