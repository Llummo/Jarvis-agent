"""Live ClickUp ticket browser endpoints (read-only): teams -> spaces -> lists -> tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query

from meta_harness.clickup_bridge import (
    list_clickup_lists,
    list_clickup_spaces,
    list_clickup_tasks,
    list_clickup_teams,
)
from meta_harness.webapp.deps import get_clickup_project_path

router = APIRouter()


@router.get("/teams")
def get_teams(project_path: Optional[Path] = Depends(get_clickup_project_path)) -> list:
    return list_clickup_teams(project_path=project_path)


@router.get("/spaces")
def get_spaces(
    team_id: str = Query(...), project_path: Optional[Path] = Depends(get_clickup_project_path)
) -> list:
    return list_clickup_spaces(team_id, project_path=project_path)


@router.get("/lists")
def get_lists(
    space_id: str = Query(...), project_path: Optional[Path] = Depends(get_clickup_project_path)
) -> list:
    return list_clickup_lists(space_id, project_path=project_path)


@router.get("/tasks")
def get_tasks(
    list_id: str = Query(...), project_path: Optional[Path] = Depends(get_clickup_project_path)
) -> list:
    return list_clickup_tasks(list_id, project_path=project_path)
