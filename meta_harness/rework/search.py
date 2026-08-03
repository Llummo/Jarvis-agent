"""A deterministic search over matcher thresholds.

This is the outer loop: take a seed setting, generate one-axis variants, score
each against hand-labelled cases, and report which one to keep. Upstream's
`search.py` does the same shape of thing by launching hermes-agent subprocesses
against TBLite; this runs in-process against our own matcher.

Every run is written to the archive in the layout the existing tooling reads, so
`show-run`, `compare-runs` and `show-frontier` all work on the output untouched.

## Why the winner is not simply the highest pass rate

The frontier ranks by pass rate, because that is Hermes's rule and it is not
this module's place to change it. But pass rate treats every wrong answer the
same, and here they are not the same:

    a miss        — the row stays unticked, a human notices, nothing breaks
    a false match — the wrong ticket gets rewritten, and nobody is told

So the search ranks candidates by pass rate *penalised* for false matches, and
says so in its output. A setting that trades three misses for one false match
climbs the frontier and loses the search — which is the correct outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from meta_harness.rework.benchmark import (
    BenchmarkResult,
    CaseSet,
    record_in_frontier,
    run_benchmark,
)
from meta_harness.rework.matching import Thresholds
from meta_harness.rework.mutations import (
    DEFAULT_SEED,
    ThresholdMutation,
    generate_variants,
)

OnStep = Optional[Callable[[str], None]]

# One false match costs as much as two correct answers. Deliberately a round,
# arguable number: the point is that the penalty is explicit and tunable, not
# that 2.0 is provably right.
FALSE_MATCH_PENALTY = 2.0

# A variant has to beat the seed by more than noise to be worth adopting. With
# a few dozen cases, a single flipped case is worth more than this, so the guard
# only suppresses genuinely flat results.
MIN_IMPROVEMENT = 1e-9

SEED_NAME = "seed"


def quality(result: BenchmarkResult) -> float:
    """Pass rate, penalised for false matches.

    Bounded below by nothing in particular — a setting that false-matches
    everything scores negative, which is the honest answer.
    """
    total = len(result.outcomes)
    if not total:
        return 0.0
    false_rate = len(result.false_matches) / total
    return round(result.pass_rate - FALSE_MATCH_PENALTY * false_rate, 6)


@dataclass
class Trial:
    """One setting, scored."""

    name: str
    thresholds: Thresholds
    result: BenchmarkResult
    mutation_slug: str = ""
    mutation_description: str = ""

    @property
    def quality(self) -> float:
        return quality(self.result)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "mutation": self.mutation_slug,
            "description": self.mutation_description,
            "thresholds": self.thresholds.describe(),
            "quality": self.quality,
            "pass_rate": self.result.pass_rate,
            "false_matches": len(self.result.false_matches),
            "misses": len(self.result.misses),
            "run_dir": str(self.result.run_dir),
        }


@dataclass
class SearchResult:
    """Everything a search tried, and what it recommends."""

    case_set_name: str
    seed: Trial
    trials: List[Trial] = field(default_factory=list)
    frontier_path: Optional[Path] = None

    @property
    def ranked(self) -> List[Trial]:
        """Best first. Ties break toward the seed, then by name, so the same
        search always recommends the same thing."""
        everything = [self.seed] + self.trials
        return sorted(
            everything,
            key=lambda trial: (-trial.quality, trial.name != SEED_NAME, trial.name),
        )

    @property
    def best(self) -> Trial:
        return self.ranked[0]

    @property
    def improved(self) -> bool:
        return self.best.name != SEED_NAME and self.best.quality - self.seed.quality > MIN_IMPROVEMENT

    def regressions(self) -> List[Trial]:
        """Variants that made things worse — worth reading, not just discarding."""
        return [t for t in self.trials if t.quality < self.seed.quality]

    def summary_line(self) -> str:
        if not self.improved:
            return (
                f"No variant beat the seed ({self.seed.quality:.3f}). "
                f"Keep the current thresholds."
            )
        return (
            f"Best: {self.best.name} ({self.best.quality:.3f} vs seed {self.seed.quality:.3f}) "
            f"— {self.best.mutation_description}"
        )

    def to_dict(self) -> Dict:
        return {
            "case_set": self.case_set_name,
            "summary": self.summary_line(),
            "improved": self.improved,
            "seed": self.seed.to_dict(),
            "best": self.best.to_dict(),
            "trials": [trial.to_dict() for trial in self.ranked],
            "frontier_path": str(self.frontier_path) if self.frontier_path else None,
            "false_match_penalty": FALSE_MATCH_PENALTY,
        }


def _report(on_step: OnStep, message: str) -> None:
    if on_step is not None:
        on_step(message)


def _score(
    case_set: CaseSet,
    thresholds: Thresholds,
    *,
    name: str,
    archive_root: Path,
    frontier_path: Optional[Path],
    mutation: Optional[ThresholdMutation] = None,
) -> Trial:
    result = run_benchmark(
        case_set,
        archive_root=archive_root,
        thresholds=thresholds,
        candidate_name=name,
        run_name=name,
    )
    if frontier_path is not None:
        record_in_frontier(result, frontier_path, notes=mutation.slug if mutation else SEED_NAME)
    return Trial(
        name=name,
        thresholds=thresholds,
        result=result,
        mutation_slug=mutation.slug if mutation else "",
        mutation_description=mutation.description if mutation else "the current settings",
    )


def search_thresholds(
    case_set: CaseSet,
    *,
    archive_root: Path,
    seed: Thresholds = DEFAULT_SEED,
    mutations: Optional[Sequence[str]] = None,
    frontier_path: Optional[Path] = None,
    on_step: OnStep = None,
) -> SearchResult:
    """Score the seed and each one-axis variant of it.

    Single round by design. A greedy multi-round search over a few dozen
    labelled cases overfits them quickly, and the resulting thresholds describe
    the sample rather than the problem. One round, read the table, decide.
    """
    case_set.validate()
    seed.validate()

    _report(on_step, f"Scoring the seed on {len(case_set.cases)} case(s)…")
    seed_trial = _score(
        case_set,
        seed,
        name=SEED_NAME,
        archive_root=archive_root,
        frontier_path=frontier_path,
    )
    _report(on_step, f"seed: {seed_trial.result.summary_line()}")

    variants = generate_variants(seed, seed_name=SEED_NAME, selected=mutations)
    if not variants:
        _report(on_step, "No variant differs from the seed — every mutation hit a rail.")

    trials: List[Trial] = []
    for index, (name, thresholds, mutation) in enumerate(variants, start=1):
        _report(on_step, f"[{index}/{len(variants)}] {mutation.slug}…")
        trial = _score(
            case_set,
            thresholds,
            name=name,
            archive_root=archive_root,
            frontier_path=frontier_path,
            mutation=mutation,
        )
        trials.append(trial)
        _report(on_step, f"  {mutation.slug}: {trial.result.summary_line()}")

    result = SearchResult(
        case_set_name=case_set.name,
        seed=seed_trial,
        trials=trials,
        frontier_path=Path(frontier_path) if frontier_path else None,
    )
    _report(on_step, result.summary_line())
    return result
