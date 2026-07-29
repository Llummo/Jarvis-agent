"""Tracker credentials, read from the environment or the repo-root `.env`.

ClickUp and Linear get separate config objects so a missing key for one
tracker never blocks the other's commands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


# Real environment variables win over the file, so a shell export or a
# systemd unit can override the checked-out `.env` without editing it.
load_dotenv(_repo_root() / ".env", override=False)


@dataclass(frozen=True)
class ClickUpConfig:
    clickup_api_token: str
    clickup_team_id: Optional[str]
    clickup_list_id: Optional[str]

    @classmethod
    def from_env(cls) -> "ClickUpConfig":
        token = os.environ.get("CLICKUP_API_TOKEN")
        if not token:
            raise RuntimeError(
                "CLICKUP_API_TOKEN is not set. Copy .env.example to .env and fill it in."
            )
        return cls(
            clickup_api_token=token,
            clickup_team_id=os.environ.get("CLICKUP_TEAM_ID") or None,
            clickup_list_id=os.environ.get("CLICKUP_LIST_ID") or None,
        )


@dataclass(frozen=True)
class LinearConfig:
    linear_api_key: str

    @classmethod
    def from_env(cls) -> "LinearConfig":
        key = os.environ.get("LINEAR_API_KEY")
        if not key:
            raise RuntimeError(
                "LINEAR_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        return cls(linear_api_key=key)
