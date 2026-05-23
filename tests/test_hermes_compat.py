from click.testing import CliRunner

from meta_harness.cli import main
from meta_harness.hermes_compat import inspect_hermes_compatibility


def _make_legacy_hermes_surface(tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    for relative in (
        "environments/benchmarks/tblite/tblite_env.py",
        "environments/benchmarks/terminalbench_2/terminalbench2_env.py",
        "environments/meta_harness/candidate.py",
        "environments/meta_harness/loader.py",
        "environments/meta_harness/types.py",
        "environments/meta_harness/candidates/snapshot_baseline.py",
    ):
        path = hermes_repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test\n", encoding="utf-8")
    return hermes_repo


def test_inspect_hermes_compatibility_accepts_legacy_surface(tmp_path):
    hermes_repo = _make_legacy_hermes_surface(tmp_path)

    report = inspect_hermes_compatibility(hermes_repo)

    assert report.compatible is True
    assert report.builtin_candidates == ["snapshot_baseline"]
    assert report.issues == []


def test_inspect_hermes_compatibility_reports_latest_like_checkout(tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    (hermes_repo / "tools" / "environments").mkdir(parents=True)

    report = inspect_hermes_compatibility(hermes_repo)

    assert report.compatible is False
    issue_codes = {issue.code for issue in report.issues}
    assert "missing_benchmark_script" in issue_codes
    assert "missing_runtime_file" in issue_codes
    assert "missing_builtin_candidates_dir" in issue_codes


def test_check_hermes_cli_succeeds_for_legacy_surface(tmp_path):
    hermes_repo = _make_legacy_hermes_surface(tmp_path)

    result = CliRunner().invoke(main, ["check-hermes", "--hermes-repo", str(hermes_repo)])

    assert result.exit_code == 0
    assert "Compatible" in result.output
    assert "yes" in result.output


def test_check_hermes_cli_fails_for_latest_like_checkout(tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    (hermes_repo / "agent").mkdir(parents=True)

    result = CliRunner().invoke(main, ["check-hermes", "--hermes-repo", str(hermes_repo)])

    assert result.exit_code != 0
    assert "missing_benchmark_script" in result.output
    assert "missing_runtime_file" in result.output
    assert "legacy Meta-Harness benchmark/runtime surface" in result.output
