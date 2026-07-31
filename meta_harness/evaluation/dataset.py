"""Labelled task sets — the ground truth an outer loop needs to mean anything.

Comparing two prompts is only possible against answers someone has already
agreed on. Without that, a search over candidates optimises a number nobody
has checked, which is worse than not searching at all: it produces confident
rankings with nothing behind them.

Module relevance is the first capability with a dataset because its answer is
a closed three-way choice — `related`, `partially_related`, `unrelated` — so
two people labelling the same ticket will usually agree. "Was this a good
ticket?" has no such property, which is why ticket generation is not here yet.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from meta_harness.module_relevance import TRACKERS, VERDICTS

# Capabilities that can be scored. Each needs a task shape and a scorer, so
# adding one is a deliberate act rather than a free-text field.
CAPABILITY_MODULE_RELEVANCE = "module_relevance"
CAPABILITIES = (CAPABILITY_MODULE_RELEVANCE,)


class DatasetError(ValueError):
    """Raised when a dataset is missing, malformed, or internally inconsistent."""


@dataclass
class LabelledTask:
    """One ticket with the verdict a human decided is correct."""

    task_name: str
    ticket_id: str
    expected_verdict: str
    tracker: str = "clickup"
    # Why this label was chosen. Not used in scoring, but the first question
    # asked when a candidate "fails" a task is whether the label was right.
    rationale: str = ""

    def validate(self) -> None:
        if not self.task_name.strip():
            raise DatasetError("every task needs a task_name")
        if "," in self.task_name:
            # The run manifest records the task selection as a comma-separated
            # list, and its hash is what tells `comparison.py` that two runs
            # covered the same tasks. A comma inside a name would split it in
            # two and silently produce a hash that matches nothing.
            raise DatasetError(f"{self.task_name}: task_name cannot contain a comma")
        if not self.ticket_id.strip():
            raise DatasetError(f"{self.task_name}: ticket_id is required")
        if self.expected_verdict not in VERDICTS:
            raise DatasetError(
                f"{self.task_name}: expected_verdict {self.expected_verdict!r} "
                f"must be one of {VERDICTS}"
            )
        if self.tracker not in TRACKERS:
            raise DatasetError(f"{self.task_name}: tracker must be one of {TRACKERS}")


@dataclass
class Dataset:
    """A capability, the module under test, and the labelled tasks."""

    name: str
    capability: str
    module_name: str
    tasks: List[LabelledTask] = field(default_factory=list)
    # Where the module's context comes from. A registered source is preferred
    # over pasted text: it is the path the harness actually uses in anger, so
    # scoring against pasted docs would measure something nobody runs.
    repo: Optional[str] = None
    module_context: str = ""
    description: str = ""

    def validate(self) -> None:
        if not self.name.strip():
            raise DatasetError("a dataset needs a name")
        if self.capability not in CAPABILITIES:
            raise DatasetError(f"capability must be one of {CAPABILITIES}, got {self.capability!r}")
        if not self.module_name.strip():
            raise DatasetError("module_name is required")
        if not self.repo and not self.module_context.strip():
            raise DatasetError(
                "give either repo (a registered source) or module_context (pasted docs) — "
                "there is nothing to judge against otherwise"
            )
        if not self.tasks:
            raise DatasetError("a dataset with no tasks cannot measure anything")

        seen = set()
        for task in self.tasks:
            task.validate()
            if task.task_name in seen:
                raise DatasetError(f"duplicate task_name: {task.task_name}")
            seen.add(task.task_name)

    @property
    def label_counts(self) -> Dict[str, int]:
        counts = {verdict: 0 for verdict in VERDICTS}
        for task in self.tasks:
            counts[task.expected_verdict] += 1
        return counts

    def imbalance_warning(self) -> Optional[str]:
        """Whether one label dominates enough to make accuracy misleading.

        A set that is 90% `unrelated` is passed by a candidate that answers
        `unrelated` every time, which is the classic way to build a benchmark
        that rewards doing nothing.
        """
        counts = self.label_counts
        total = sum(counts.values())
        if total == 0:
            return None
        top_label, top_count = max(counts.items(), key=lambda item: item[1])
        share = top_count / total
        if share >= 0.7:
            return (
                f"{share:.0%} of tasks are labelled '{top_label}'. A candidate that always "
                f"answers '{top_label}' would score {share:.0%} — compare against that floor, "
                "not against zero."
            )
        return None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["tasks"] = [asdict(task) for task in self.tasks]
        return payload

    def save(self, path: Path) -> Path:
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Dataset":
        if not isinstance(payload, dict):
            raise DatasetError(f"expected a JSON object, got {type(payload).__name__}")
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list):
            raise DatasetError("'tasks' must be a list")

        tasks = []
        for index, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise DatasetError(f"task {index} is not an object")
            unknown = set(raw) - {"task_name", "ticket_id", "expected_verdict", "tracker", "rationale"}
            if unknown:
                raise DatasetError(f"task {raw.get('task_name', index)}: unknown fields {sorted(unknown)}")
            tasks.append(
                LabelledTask(
                    task_name=str(raw.get("task_name", "")),
                    ticket_id=str(raw.get("ticket_id", "")),
                    expected_verdict=str(raw.get("expected_verdict", "")),
                    tracker=str(raw.get("tracker", "clickup")),
                    rationale=str(raw.get("rationale", "")),
                )
            )

        dataset = cls(
            name=str(payload.get("name", "")),
            capability=str(payload.get("capability", CAPABILITY_MODULE_RELEVANCE)),
            module_name=str(payload.get("module_name", "")),
            repo=payload.get("repo") or None,
            module_context=str(payload.get("module_context", "")),
            description=str(payload.get("description", "")),
            tasks=tasks,
        )
        dataset.validate()
        return dataset

    @classmethod
    def load(cls, path: Path) -> "Dataset":
        path = Path(path)
        if not path.is_file():
            raise DatasetError(f"No dataset at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(payload)


def datasets_dir() -> Path:
    """Where datasets live by default.

    Under the gitignored state directory: a labelled set contains real ticket
    ids and someone's judgement about client work.
    """
    return Path(__file__).resolve().parent.parent.parent / "qa" / "datasets"


def template(name: str, module_name: str, *, repo: Optional[str] = None) -> Dataset:
    """A starting dataset with one example task, to be edited by hand.

    Labelling is the part nobody can automate: the value of the whole loop
    rests on these answers being right.
    """
    return Dataset(
        name=name,
        capability=CAPABILITY_MODULE_RELEVANCE,
        module_name=module_name,
        repo=repo,
        description=f"Hand-labelled tickets for the '{module_name}' module.",
        tasks=[
            LabelledTask(
                task_name="example-1",
                ticket_id="REPLACE-WITH-A-REAL-TICKET-ID",
                expected_verdict="related",
                rationale="Why you decided this ticket belongs to the module.",
            )
        ],
    )
