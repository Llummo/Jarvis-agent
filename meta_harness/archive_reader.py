"""Read Hermes Meta-Harness archives."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from meta_harness.models import RunSummary

logger = logging.getLogger(__name__)


def _load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_object(path: Path, *, label: str) -> Dict:
    """Load a JSON object and raise a descriptive error when it is malformed."""
    try:
        payload = _load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed {label} in {path.parent}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"{label} in {path.parent} is not a JSON object (got {type(payload).__name__})"
        )

    return payload


def find_run_dirs(archive_root: Path) -> List[Path]:
    """Return run directories containing summary.json."""
    archive_root = archive_root.expanduser().resolve()
    if not archive_root.exists():
        return []
    return sorted(
        [path for path in archive_root.iterdir() if path.is_dir() and (path / "summary.json").exists()],
        key=lambda path: path.stat().st_mtime,
    )


def find_latest_run_dir(archive_root: Path) -> Path:
    """Return the newest run directory in an archive root."""
    run_dirs = find_run_dirs(archive_root)
    if not run_dirs:
        raise FileNotFoundError(f"No Meta-Harness run summaries found in {archive_root}")
    return run_dirs[-1]


def load_manifest(run_dir: Path) -> Dict:
    """Load manifest.json if present."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    return _load_json_object(manifest_path, label="manifest.json")


def load_task_records(run_dir: Path) -> List[Dict]:
    """Load all per-task JSON records for a run."""
    tasks_dir = run_dir / "tasks"
    if not tasks_dir.exists():
        return []

    records = []
    for task_file in sorted(tasks_dir.glob("*.json")):
        try:
            payload = _load_json(task_file)
            if not isinstance(payload, dict):
                logger.warning("Skipping non-object task file: %s", task_file)
                continue
            payload["_task_record_path"] = str(task_file)
            records.append(payload)
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted task file: %s", task_file)
            continue
    return records


NestedPath = Tuple[str, ...]

TASK_NAME_PATHS: Tuple[NestedPath, ...] = (
    ("task_name",),
    ("task_id",),
    ("name",),
    ("id",),
    ("task", "task_name"),
    ("task", "task_id"),
    ("task", "name"),
    ("task", "id"),
    ("result", "task_name"),
    ("result", "task_id"),
    ("summary", "task_name"),
)

PASSED_PATHS: Tuple[NestedPath, ...] = (
    ("passed",),
    ("success",),
    ("is_success",),
    ("result", "passed"),
    ("result", "success"),
    ("outcome", "passed"),
    ("outcome", "success"),
    ("evaluation", "passed"),
    ("evaluation", "success"),
    ("eval", "passed"),
)

REWARD_PATHS: Tuple[NestedPath, ...] = (
    ("reward",),
    ("score",),
    ("result", "reward"),
    ("result", "score"),
    ("outcome", "reward"),
    ("outcome", "score"),
    ("evaluation", "reward"),
    ("evaluation", "score"),
    ("eval", "reward"),
    ("metrics", "reward"),
)

ERROR_PATHS: Tuple[NestedPath, ...] = (
    ("error_summary",),
    ("failure_reason",),
    ("error",),
    ("exception",),
    ("result", "error_summary"),
    ("result", "failure_reason"),
    ("result", "error"),
    ("outcome", "error_summary"),
    ("outcome", "failure_reason"),
    ("outcome", "error"),
    ("outcome", "tool_errors"),
    ("evaluation", "error_summary"),
    ("evaluation", "failure_reason"),
    ("diagnostics", "error_summary"),
    ("diagnostics", "failure_reason"),
)

TRACE_PATHS: Tuple[NestedPath, ...] = (
    ("trace_path",),
    ("trajectory_path",),
    ("log_path",),
    ("transcript_path",),
    ("result", "trace_path"),
    ("result", "trajectory_path"),
    ("diagnostics", "trace_path"),
    ("diagnostics", "log_path"),
    ("artifacts", "trace_path"),
    ("artifacts", "trajectory_path"),
    ("_task_record_path",),
)


def _nested_value(payload: Dict[str, Any], path: NestedPath) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _first_nested_value(payload: Dict[str, Any], paths: Sequence[NestedPath]) -> Any:
    for path in paths:
        value = _nested_value(payload, path)
        if value not in (None, ""):
            return value
    return None


def _string_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if value == [] or value == {}:
        return None
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, sort_keys=True)
        except TypeError:
            text = str(value)
    else:
        text = str(value)
    text = text.strip()
    return text or None


def _bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"pass", "passed", "success", "succeeded", "true", "yes", "1"}:
            return True
        if normalized in {"fail", "failed", "failure", "false", "no", "0"}:
            return False
    return None


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _normalize_task_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    task_name = _string_or_none(_first_nested_value(record, TASK_NAME_PATHS))
    if task_name is None:
        return None

    task_result: Dict[str, Any] = {"task_name": task_name}
    passed = _bool_or_none(_first_nested_value(record, PASSED_PATHS))
    reward = _float_or_none(_first_nested_value(record, REWARD_PATHS))
    error_summary = _string_or_none(_first_nested_value(record, ERROR_PATHS))
    trace_path = _string_or_none(_first_nested_value(record, TRACE_PATHS))

    if passed is not None:
        task_result["passed"] = passed
    if reward is not None:
        task_result["reward"] = reward
    if error_summary is not None:
        task_result["error_summary"] = error_summary
    if trace_path is not None:
        task_result["trace_path"] = trace_path

    return task_result


def _task_records_to_results(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    task_results = []
    for record in records:
        task_result = _normalize_task_record(record)
        if task_result is not None:
            task_results.append(task_result)
    return task_results


def _merge_task_results(
    summary_task_results: List[Any],
    archive_task_results: List[Dict[str, Any]],
) -> List[Any]:
    """Merge task archive diagnostics into summary task results by task name."""
    if not archive_task_results:
        return summary_task_results

    archive_by_name = {
        str(task["task_name"]): task
        for task in archive_task_results
        if task.get("task_name")
    }
    merged: List[Any] = []

    for task in summary_task_results:
        if not isinstance(task, dict) or not task.get("task_name"):
            merged.append(task)
            continue

        task_name = str(task["task_name"])
        archive_task = archive_by_name.pop(task_name, None)
        if archive_task is None:
            merged.append(task)
            continue

        combined = dict(archive_task)
        for key, value in task.items():
            if value is not None:
                combined[key] = value
        merged.append(combined)

    merged.extend(archive_by_name[name] for name in sorted(archive_by_name))
    return merged


def load_run_summary(run_dir: Path) -> RunSummary:
    """Load one run summary and its manifest."""
    run_dir = run_dir.expanduser().resolve()
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"Missing summary.json in {run_dir}")

    payload = _load_json_object(summary_path, label="summary.json")
    eval_metrics = payload.get("eval_metrics", {})
    task_results = payload.get("task_results", [])
    if not isinstance(eval_metrics, dict):
        raise ValueError(f"summary.json in {run_dir} has non-object eval_metrics")
    if not isinstance(task_results, list):
        raise ValueError(f"summary.json in {run_dir} has non-list task_results")

    archive_task_results = _task_records_to_results(load_task_records(run_dir))
    task_results = _merge_task_results(task_results, archive_task_results)

    return RunSummary(
        benchmark_name=payload.get("benchmark_name", ""),
        candidate_name=payload.get("candidate_name", ""),
        candidate_path=payload.get("candidate_path", ""),
        run_dir=run_dir,
        eval_metrics=eval_metrics,
        task_results=task_results,
        manifest=load_manifest(run_dir),
    )


def load_latest_run_summary(archive_root: Path) -> RunSummary:
    """Load the latest run summary from an archive root."""
    return load_run_summary(find_latest_run_dir(archive_root))
