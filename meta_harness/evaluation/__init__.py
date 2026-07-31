"""Measuring the harness against answers a human already agreed on.

The outer loop in this repository was built to score hermes-agent on TBLite and
TerminalBench. Those benchmarks say nothing about whether *this* harness reads a
ticket correctly, so the loop had nothing real to optimise here.

This package supplies the missing half: labelled task sets (`dataset`), the
settings being compared (`candidates`), and a runner that writes the same run
directory the existing `archive_reader`, `comparison`, and `frontier` modules
already consume. Those three are domain-agnostic and are reused unchanged — only
the part that knew about Hermes needed replacing.
"""

from meta_harness.evaluation.candidates import Candidate, baseline
from meta_harness.evaluation.dataset import (
    CAPABILITIES,
    CAPABILITY_MODULE_RELEVANCE,
    Dataset,
    DatasetError,
    LabelledTask,
    datasets_dir,
    template,
)
from meta_harness.evaluation.runner import (
    EvaluationResult,
    TaskOutcome,
    evaluate_candidate,
)

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_MODULE_RELEVANCE",
    "Candidate",
    "Dataset",
    "DatasetError",
    "EvaluationResult",
    "LabelledTask",
    "TaskOutcome",
    "baseline",
    "datasets_dir",
    "evaluate_candidate",
    "template",
]
