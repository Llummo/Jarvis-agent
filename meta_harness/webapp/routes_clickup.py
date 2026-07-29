"""Live ClickUp ticket browser endpoints (read-only): teams -> spaces -> folders/lists -> tasks."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from meta_harness.clickup_bridge import (
    list_clickup_folders,
    list_clickup_lists,
    list_clickup_spaces,
    list_clickup_tasks,
    list_clickup_teams,
)

router = APIRouter()


@router.get("/teams")
def get_teams() -> list:
    return list_clickup_teams()


@router.get("/spaces")
def get_spaces(team_id: str = Query(...)) -> list:
    return list_clickup_spaces(team_id)


@router.get("/folders")
def get_folders(space_id: str = Query(...)) -> list:
    return list_clickup_folders(space_id)


@router.get("/lists")
def get_lists(space_id: Optional[str] = None, folder_id: Optional[str] = None) -> list:
    if not space_id and not folder_id:
        raise HTTPException(status_code=400, detail="Provide space_id or folder_id")
    return list_clickup_lists(space_id, folder_id=folder_id)


@router.get("/tasks")
def get_tasks(list_id: str = Query(...)) -> list:
    return list_clickup_tasks(list_id)
