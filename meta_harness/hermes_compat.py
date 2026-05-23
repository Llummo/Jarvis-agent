"""Compatibility checks for Hermes Meta-Harness integration points."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from meta_harness.candidate_registry import builtin_candidates_dir, list_builtin_candidates

LEGACY_BENCHMARK_SCRIPTS = {
    "tblite": Path("environments/benchmarks/tblite/tblite_env.py"),
    "tb2": Path("environments/benchmarks/terminalbench_2/terminalbench2_env.py"),
}

LEGACY_RUNTIME_DIR = Path("environments/meta_harness")
LEGACY_RUNTIME_FILES = (
    Path("candidate.py"),
    Path("loader.py"),
    Path("types.py"),
)


@dataclass(frozen=True)
class HermesCompatibilityIssue:
    """One missing or incompatible Hermes integration point."""

    code: str
    path: str
    message: str
    severity: str = "error"

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class HermesCompatibilityReport:
    """Compatibility report for one Hermes checkout."""

    hermes_agent_path: Path
    git_head: Optional[str] = None
    remote_url: Optional[str] = None
    benchmark_scripts: Dict[str, str] = field(default_factory=dict)
    runtime_files: Dict[str, str] = field(default_factory=dict)
    builtin_candidates_dir: str = ""
    builtin_candidates: List[str] = field(default_factory=list)
    issues: List[HermesCompatibilityIssue] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "compatible": self.compatible,
            "hermes_agent_path": str(self.hermes_agent_path),
            "git_head": self.git_head,
            "remote_url": self.remote_url,
            "benchmark_scripts": dict(self.benchmark_scripts),
            "runtime_files": dict(self.runtime_files),
            "builtin_candidates_dir": self.builtin_candidates_dir,
            "builtin_candidates": list(self.builtin_candidates),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _git_value(repo: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def legacy_surface_hint() -> str:
    """Return a concise hint for missing legacy Hermes Meta-Harness support."""
    return (
        "This checkout does not expose the legacy Hermes Meta-Harness benchmark "
        "surface expected by hermes-agent-metaharness. Run "
        "`python -m meta_harness check-hermes --hermes-repo /path/to/hermes-agent` "
        "for the full compatibility report."
    )


def inspect_hermes_compatibility(hermes_agent_path: Path) -> HermesCompatibilityReport:
    """Inspect whether a Hermes checkout exposes the required legacy surface."""
    root = hermes_agent_path.expanduser().resolve()
    issues: List[HermesCompatibilityIssue] = []
    benchmark_scripts: Dict[str, str] = {}
    runtime_files: Dict[str, str] = {}

    if not root.exists():
        issues.append(
            HermesCompatibilityIssue(
                code="missing_repo",
                path=str(root),
                message="Hermes checkout path does not exist.",
            )
        )
        return HermesCompatibilityReport(hermes_agent_path=root, issues=issues)

    if not root.is_dir():
        issues.append(
            HermesCompatibilityIssue(
                code="repo_not_directory",
                path=str(root),
                message="Hermes checkout path is not a directory.",
            )
        )
        return HermesCompatibilityReport(hermes_agent_path=root, issues=issues)

    for benchmark, relative_path in LEGACY_BENCHMARK_SCRIPTS.items():
        absolute_path = root / relative_path
        benchmark_scripts[benchmark] = str(absolute_path)
        if not absolute_path.is_file():
            issues.append(
                HermesCompatibilityIssue(
                    code="missing_benchmark_script",
                    path=str(relative_path),
                    message=f"Missing benchmark entrypoint for {benchmark}.",
                )
            )

    for relative_path in LEGACY_RUNTIME_FILES:
        absolute_path = root / LEGACY_RUNTIME_DIR / relative_path
        runtime_files[relative_path.name] = str(absolute_path)
        if not absolute_path.is_file():
            issues.append(
                HermesCompatibilityIssue(
                    code="missing_runtime_file",
                    path=str(LEGACY_RUNTIME_DIR / relative_path),
                    message="Missing candidate runtime module required by generated candidates.",
                )
            )

    builtins_dir = builtin_candidates_dir(root)
    builtin_candidates = list_builtin_candidates(root)
    if not builtins_dir.is_dir():
        issues.append(
            HermesCompatibilityIssue(
                code="missing_builtin_candidates_dir",
                path=str(LEGACY_RUNTIME_DIR / "candidates"),
                message="Built-in candidates are unavailable; default baselines such as snapshot_baseline will not resolve.",
                severity="warning",
            )
        )
    elif not builtin_candidates:
        issues.append(
            HermesCompatibilityIssue(
                code="empty_builtin_candidates_dir",
                path=str(LEGACY_RUNTIME_DIR / "candidates"),
                message="Built-in candidate directory exists but contains no candidate modules.",
                severity="warning",
            )
        )

    return HermesCompatibilityReport(
        hermes_agent_path=root,
        git_head=_git_value(root, "rev-parse", "HEAD"),
        remote_url=_git_value(root, "remote", "get-url", "origin"),
        benchmark_scripts=benchmark_scripts,
        runtime_files=runtime_files,
        builtin_candidates_dir=str(builtins_dir),
        builtin_candidates=builtin_candidates,
        issues=issues,
    )
