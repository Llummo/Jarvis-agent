"""Preflight checks for a fresh install.

Most of what can go wrong on someone else's machine is a missing external
dependency or an unset key, and each one otherwise surfaces much later as a
confusing failure deep inside a feature. This reports all of them at once,
before anything is run, and says what to do about each.

Checks are grouped by what they gate rather than by what they are: a missing
Gemini key costs you repository retrieval and nothing else, and that is the
useful thing to tell someone.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

REQUIRED_PYTHON = (3, 9)

# (import name, what stops working without it)
REQUIRED_PACKAGES = [
    ("fastapi", "the web UI"),
    ("uvicorn", "the web UI"),
    ("click", "the CLI"),
    ("rich", "CLI output"),
    ("requests", "ClickUp and Linear"),
    ("dotenv", "reading .env"),
    ("numpy", "embedding search"),
    ("google.genai", "repository embeddings"),
    ("pypdf", "reading PDF requirements documents"),
    ("fpdf", "PDF QA reports"),
]

OK = "ok"
MISSING = "missing"
DEGRADED = "degraded"


@dataclass
class Check:
    """One preflight result."""

    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def blocking(self) -> bool:
        """Whether this stops the app starting at all."""
        return self.status == MISSING


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def check_python() -> Check:
    current = sys.version_info[:2]
    if current >= REQUIRED_PYTHON:
        return Check("Python", OK, f"{current[0]}.{current[1]}")
    return Check(
        "Python",
        MISSING,
        f"{current[0]}.{current[1]} is too old",
        f"Install Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} or newer.",
    )


def check_packages() -> List[Check]:
    results: List[Check] = []
    for module_name, purpose in REQUIRED_PACKAGES:
        try:
            importlib.import_module(module_name)
        except ImportError:
            results.append(
                Check(
                    f"package: {module_name}",
                    MISSING,
                    f"not installed — {purpose} will not work",
                    'Run: pip install -e ".[dev]"',
                )
            )
    if not results:
        return [Check("Python packages", OK, f"all {len(REQUIRED_PACKAGES)} present")]
    return results


def check_claude_cli() -> Check:
    """The CLI cannot be bundled — it installs and authenticates separately."""
    override = os.getenv("META_HARNESS_CLAUDE_PATH")
    if override and Path(override).expanduser().exists():
        return Check("claude CLI", OK, str(Path(override).expanduser()))
    found = shutil.which("claude")
    if found:
        return Check("claude CLI", OK, found)
    return Check(
        "claude CLI",
        MISSING,
        "not on PATH — ticket generation, QA review and module checks all need it",
        "Install Claude Code and sign in, or set META_HARNESS_CLAUDE_PATH.",
    )


def check_git() -> Check:
    found = shutil.which("git")
    if found:
        return Check("git", OK, found)
    return Check(
        "git",
        DEGRADED,
        "not on PATH — repository indexing falls back to walking the tree, "
        "which cannot honour .gitignore",
        "Install git.",
    )


def _example_values() -> dict:
    """The placeholder values shipped in .env.example.

    First run copies that file to .env, so every key is technically "set" —
    to a dummy. Comparing against the source of those dummies catches it
    without hard-coding a list that would drift as keys are added.
    """
    example = _repo_root() / ".env.example"
    values = {}
    if example.is_file():
        for line in example.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _env_check(name: str, variable: str, purpose: str, *, blocking: bool = False) -> Check:
    value = os.getenv(variable)
    placeholder = _example_values().get(variable)

    if value and placeholder and value == placeholder:
        return Check(
            name,
            MISSING if blocking else DEGRADED,
            f"{variable} is still the placeholder from .env.example — {purpose} will not work",
            f"Put a real {variable} in .env.",
        )
    if value:
        return Check(name, OK, f"{variable} is set")
    return Check(
        name,
        MISSING if blocking else DEGRADED,
        f"{variable} is not set — {purpose} will not work",
        "Copy .env.example to .env and fill it in.",
    )


def check_credentials() -> List[Check]:
    # Importing the tracker config for its side effect — it loads the
    # repo-root .env — so this report reflects the environment the app will
    # actually see, rather than only what the shell exported.
    try:
        importlib.import_module("meta_harness.trackers.config")
    except Exception:
        pass
    return [
        _env_check("ClickUp", "CLICKUP_API_TOKEN", "the ClickUp tab and ticket creation"),
        _env_check("Linear", "LINEAR_API_KEY", "the Linear tab"),
        _env_check("Gemini", "GEMINI_API_KEY", "repository indexing and retrieval"),
    ]


def check_static_assets() -> Check:
    static = _repo_root() / "meta_harness" / "webapp" / "static"
    missing = [
        name for name in ("index.html", "app.js", "style.css") if not (static / name).is_file()
    ]
    if missing:
        return Check(
            "Web UI assets",
            MISSING,
            f"missing from {static}: {', '.join(missing)}",
            "Reinstall the package — the release archive is incomplete.",
        )
    return Check("Web UI assets", OK, str(static))


def check_writable_state() -> Check:
    """The app writes a QA database, run archives and the embedding index."""
    target = _repo_root() / "qa"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "Writable state directory",
            MISSING,
            f"cannot write to {target}: {exc}",
            "Fix the directory permissions, or run from a writable location.",
        )
    return Check("Writable state directory", OK, str(target))


def check_embedding_index() -> Check:
    from meta_harness.embeddings.store import default_db_path

    path = default_db_path()
    if not path.exists():
        return Check(
            "Embedding index",
            DEGRADED,
            "no repository indexed yet",
            "Run: meta-harness index build --repo <path>",
        )
    try:
        connection = sqlite3.connect(str(path))
        try:
            row = connection.execute(
                "SELECT COUNT(DISTINCT repo), COUNT(*) FROM chunks"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return Check("Embedding index", DEGRADED, f"unreadable: {exc}", "Rebuild it.")
    repos, chunks = row or (0, 0)
    if not chunks:
        return Check(
            "Embedding index",
            DEGRADED,
            "empty",
            "Run: meta-harness index build --repo <path>",
        )
    return Check("Embedding index", OK, f"{repos} repo(s), {chunks} chunk(s)")


def run_all() -> List[Check]:
    """Every check, in the order a new install should read them."""
    checks: List[Check] = [check_python()]
    checks.extend(check_packages())
    checks.append(check_static_assets())
    checks.append(check_writable_state())
    checks.append(check_claude_cli())
    checks.append(check_git())
    checks.extend(check_credentials())
    checks.append(check_embedding_index())
    return checks


def blocking_failures(checks: Optional[List[Check]] = None) -> List[Check]:
    return [check for check in (checks if checks is not None else run_all()) if check.blocking]
