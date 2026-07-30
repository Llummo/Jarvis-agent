"""Preflight checks.

These matter more than most tests: `doctor` is the first thing a new install
runs, and a check that reports OK when something is actually broken is worse
than no check at all — it sends someone looking in the wrong place.
"""

from __future__ import annotations

import sys

import pytest

from meta_harness import doctor


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for variable in ("CLICKUP_API_TOKEN", "LINEAR_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)


def test_check_python_passes_on_a_supported_interpreter():
    check = doctor.check_python()

    assert check.status == doctor.OK
    assert check.detail.startswith(f"{sys.version_info[0]}.")


def test_required_packages_are_installed():
    """If this fails the environment is incomplete, which is the point."""
    assert all(check.status == doctor.OK for check in doctor.check_packages())


def test_claude_cli_honours_the_path_override(monkeypatch, tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("META_HARNESS_CLAUDE_PATH", str(binary))

    assert doctor.check_claude_cli().status == doctor.OK


def test_missing_claude_cli_blocks_and_says_why(monkeypatch):
    monkeypatch.delenv("META_HARNESS_CLAUDE_PATH", raising=False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

    check = doctor.check_claude_cli()

    assert check.blocking
    assert "ticket generation" in check.detail
    assert check.fix


def test_missing_git_degrades_rather_than_blocks(monkeypatch):
    """Indexing still works without git; it just cannot honour .gitignore."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

    check = doctor.check_git()

    assert check.status == doctor.DEGRADED
    assert not check.blocking


def test_unset_credential_is_reported(monkeypatch):
    check = doctor._env_check("Gemini", "GEMINI_API_KEY", "retrieval")

    assert check.status == doctor.DEGRADED
    assert "not set" in check.detail


def test_placeholder_credential_is_not_mistaken_for_configuration(monkeypatch):
    """First run copies .env.example to .env, so every key is 'set' to a
    dummy. Reporting that as OK would send a new user chasing 401s."""
    placeholder = doctor._example_values().get("CLICKUP_API_TOKEN")
    assert placeholder, ".env.example should carry a placeholder to test against"
    monkeypatch.setenv("CLICKUP_API_TOKEN", placeholder)

    check = doctor._env_check("ClickUp", "CLICKUP_API_TOKEN", "the ClickUp tab")

    assert check.status == doctor.DEGRADED
    assert "placeholder" in check.detail


def test_real_credential_passes(monkeypatch):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_a_real_looking_token")

    assert doctor._env_check("ClickUp", "CLICKUP_API_TOKEN", "the ClickUp tab").status == doctor.OK


def test_static_assets_present():
    assert doctor.check_static_assets().status == doctor.OK


def test_writable_state_directory():
    assert doctor.check_writable_state().status == doctor.OK


def test_run_all_covers_every_area():
    names = {check.name for check in doctor.run_all()}

    assert "Python" in names
    assert "claude CLI" in names
    assert "Gemini" in names
    assert "Embedding index" in names


def test_blocking_failures_filters_to_blockers():
    checks = [
        doctor.Check("a", doctor.OK, ""),
        doctor.Check("b", doctor.DEGRADED, ""),
        doctor.Check("c", doctor.MISSING, ""),
    ]

    assert [check.name for check in doctor.blocking_failures(checks)] == ["c"]
