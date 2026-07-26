"""Live ClickUp ticket browser endpoints (read-only): teams -> spaces -> folders/lists -> tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from meta_harness.clickup_bridge import (
    list_clickup_folders,
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


@router.get("/folders")
def get_folders(
    space_id: str = Query(...), project_path: Optional[Path] = Depends(get_clickup_project_path)
) -> list:
    return list_clickup_folders(space_id, project_path=project_path)


@router.get("/lists")
def get_lists(
    space_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    project_path: Optional[Path] = Depends(get_clickup_project_path),
) -> list:
    if not space_id and not folder_id:
        raise HTTPException(status_code=400, detail="Provide space_id or folder_id")
    return list_clickup_lists(space_id, folder_id=folder_id, project_path=project_path)


@router.get("/tasks")
def get_tasks(
    list_id: str = Query(...), project_path: Optional[Path] = Depends(get_clickup_project_path)
) -> list:
    return list_clickup_tasks(list_id, project_path=project_path)
