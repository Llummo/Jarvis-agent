"""Generic per-project playbooks: init a project and run its agent-defined flow.

Each agent gets its own playbook definition under `agents/<name>.json`, so
config stays scoped to one agent instead of a shared global config. A
playbook describes where its target project lives, how to set it up, and
what "the complete flow" means for that project.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence


def agents_dir() -> Path:
    """Return the directory containing agent playbook definitions."""
    return Path(__file__).resolve().parent.parent / "agents"


def _default_search_root() -> Path:
    """Directory whose siblings are searched for a playbook's target project."""
    return Path(__file__).resolve().parent.parent.parent


@dataclass
class AgentPlaybook:
    """Per-agent playbook: where its project lives and how to run it."""

    name: str
    description: str
    project_env_var: str
    project_sibling: str
    setup: List[List[str]] = field(default_factory=list)
    flow: List[List[str]] = field(default_factory=list)
    env_example: Optional[str] = None
    env_file: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "AgentPlaybook":
        """Load an agent playbook definition from a JSON file."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=payload["name"],
            description=payload.get("description", ""),
            project_env_var=payload["project_env_var"],
            project_sibling=payload["project_sibling"],
            setup=payload.get("setup", []),
            flow=payload.get("flow", []),
            env_example=payload.get("env_example"),
            env_file=payload.get("env_file"),
        )


def list_agent_playbooks() -> List[AgentPlaybook]:
    """Load every agent playbook defined in the agents/ directory."""
    directory = agents_dir()
    if not directory.exists():
        return []
    return [AgentPlaybook.load(path) for path in sorted(directory.glob("*.json"))]


def load_agent_playbook(name: str) -> AgentPlaybook:
    """Load a single agent playbook by name."""
    path = agents_dir() / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in agents_dir().glob("*.json")) if agents_dir().exists() else []
        raise FileNotFoundError(
            f"No playbook found for agent '{name}' at {path}. Available: {available}"
        )
    return AgentPlaybook.load(path)


def resolve_project_path(playbook: AgentPlaybook, *, search_root: Optional[Path] = None) -> Path:
    """Resolve the target project directory for an agent playbook.

    Resolution order: the agent's env var override, then a sibling checkout
    next to this repo named after the playbook's `project_sibling`.
    """
    env_value = os.getenv(playbook.project_env_var)
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.exists():
            return candidate.resolve()

    root = search_root if search_root is not None else _default_search_root()
    sibling = root / playbook.project_sibling
    if sibling.exists():
        return sibling.resolve()

    raise FileNotFoundError(
        f"Could not locate project for agent '{playbook.name}'. "
        f"Set {playbook.project_env_var} or place a checkout at ../{playbook.project_sibling}."
    )


@dataclass
class StepResult:
    """Result of a single playbook step (setup or flow)."""

    command: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run_step(command: Sequence[str], cwd: Path) -> StepResult:
    completed = subprocess.run(list(command), cwd=cwd, capture_output=True, text=True)
    return StepResult(
        command=list(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _run_steps(commands: Sequence[Sequence[str]], cwd: Path) -> List[StepResult]:
    results: List[StepResult] = []
    for command in commands:
        result = _run_step(command, cwd)
        results.append(result)
        if not result.ok:
            break
    return results


def init_project(playbook: AgentPlaybook, *, project_path: Optional[Path] = None) -> List[StepResult]:
    """Run an agent playbook's setup steps against its resolved project."""
    project_path = project_path if project_path is not None else resolve_project_path(playbook)
    results = _run_steps(playbook.setup, project_path)
    if results and not results[-1].ok:
        return results

    if playbook.env_example and playbook.env_file:
        example_path = project_path / playbook.env_example
        env_path = project_path / playbook.env_file
        if example_path.exists() and not env_path.exists():
            env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")

    return results


def run_flow(playbook: AgentPlaybook, *, project_path: Optional[Path] = None) -> List[StepResult]:
    """Run an agent playbook's flow steps against its resolved project."""
    project_path = project_path if project_path is not None else resolve_project_path(playbook)
    return _run_steps(playbook.flow, project_path)


def run_complete(playbook: AgentPlaybook) -> "tuple[List[StepResult], List[StepResult]]":
    """Initialize an agent's project, then run its complete flow.

    Flow steps only run if setup succeeded (or had nothing to do).
    """
    project_path = resolve_project_path(playbook)
    setup_results = init_project(playbook, project_path=project_path)
    if setup_results and not setup_results[-1].ok:
        return setup_results, []
    flow_results = run_flow(playbook, project_path=project_path)
    return setup_results, flow_results
