"""Deterministic variations of the matcher's thresholds.

The upstream `mutation.py` generates variants of hermes-agent — a prompt
prelude, a reordered tool list, a turn cap. None of that exists here. What this
harness actually has to tune is three numbers in `Thresholds`, so these are the
mutations: small, named, reversible moves along one axis at a time.

One axis at a time is the point. A variant that changes two thresholds together
may score better without telling you which change earned it, and the next
search step then has nothing to build on.

Deterministic by construction: same seed in, same variants out. A search that
cannot be re-run is not evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from meta_harness.rework.matching import (
    MAX_CANDIDATES,
    MIN_CANDIDATE,
    STRONG_MATCH,
    Thresholds,
)

# Step sizes. Large enough to move an outcome, small enough that a win can be
# attributed to the axis rather than to having jumped somewhere else entirely.
MATCH_STEP = 0.06
CANDIDATE_STEP = 0.10
MARGIN_STEP = 0.04

# Hard rails. A variant outside these is not a tuning question, it is a broken
# matcher: below the floor everything matches, above the ceiling nothing does.
MATCH_FLOOR = 0.55
MATCH_CEILING = 0.98
CANDIDATE_FLOOR = 0.15
CANDIDATE_CEILING = 0.70
MARGIN_CEILING = 0.30


def _clamp(value: float, low: float, high: float) -> float:
    return round(max(low, min(high, value)), 4)


@dataclass(frozen=True)
class ThresholdMutation:
    """One named move away from a seed setting."""

    slug: str
    description: str
    # Why you would want this — the trade being made, not the arithmetic.
    rationale: str
    apply: Callable[[Thresholds], Thresholds]

    def __call__(self, seed: Thresholds) -> Thresholds:
        return self.apply(seed)


def _stricter_match(seed: Thresholds) -> Thresholds:
    return Thresholds(
        strong_match=_clamp(seed.strong_match + MATCH_STEP, MATCH_FLOOR, MATCH_CEILING),
        min_candidate=seed.min_candidate,
        ambiguity_margin=seed.ambiguity_margin,
        max_candidates=seed.max_candidates,
    )


def _looser_match(seed: Thresholds) -> Thresholds:
    # Never allowed below min_candidate, or the two bars cross and every
    # candidate is simultaneously too weak to consider and good enough to accept.
    floor = max(MATCH_FLOOR, seed.min_candidate + 0.05)
    return Thresholds(
        strong_match=_clamp(seed.strong_match - MATCH_STEP, floor, MATCH_CEILING),
        min_candidate=seed.min_candidate,
        ambiguity_margin=seed.ambiguity_margin,
        max_candidates=seed.max_candidates,
    )


def _wider_net(seed: Thresholds) -> Thresholds:
    return Thresholds(
        strong_match=seed.strong_match,
        min_candidate=_clamp(
            seed.min_candidate - CANDIDATE_STEP, CANDIDATE_FLOOR, seed.strong_match - 0.05
        ),
        ambiguity_margin=seed.ambiguity_margin,
        max_candidates=seed.max_candidates,
    )


def _narrower_net(seed: Thresholds) -> Thresholds:
    return Thresholds(
        strong_match=seed.strong_match,
        min_candidate=_clamp(
            seed.min_candidate + CANDIDATE_STEP, CANDIDATE_FLOOR, seed.strong_match - 0.05
        ),
        ambiguity_margin=seed.ambiguity_margin,
        max_candidates=seed.max_candidates,
    )


def _decisive(seed: Thresholds) -> Thresholds:
    return Thresholds(
        strong_match=seed.strong_match,
        min_candidate=seed.min_candidate,
        ambiguity_margin=_clamp(seed.ambiguity_margin - MARGIN_STEP, 0.0, MARGIN_CEILING),
        max_candidates=seed.max_candidates,
    )


def _cautious(seed: Thresholds) -> Thresholds:
    return Thresholds(
        strong_match=seed.strong_match,
        min_candidate=seed.min_candidate,
        ambiguity_margin=_clamp(seed.ambiguity_margin + MARGIN_STEP, 0.0, MARGIN_CEILING),
        max_candidates=seed.max_candidates,
    )


def builtin_mutations() -> Dict[str, ThresholdMutation]:
    """The deterministic moves a search may make."""
    return {
        "stricter_match": ThresholdMutation(
            slug="stricter_match",
            description=f"Raise strong_match by {MATCH_STEP}",
            rationale="Fewer automatic matches, more rows left for a human. Trades misses for safety.",
            apply=_stricter_match,
        ),
        "looser_match": ThresholdMutation(
            slug="looser_match",
            description=f"Lower strong_match by {MATCH_STEP}",
            rationale="More names resolve unattended. Risks accepting a name that should have been queried.",
            apply=_looser_match,
        ),
        "wider_net": ThresholdMutation(
            slug="wider_net",
            description=f"Lower min_candidate by {CANDIDATE_STEP}",
            rationale="Weak candidates still get offered, so an ambiguous row has something to choose from.",
            apply=_wider_net,
        ),
        "narrower_net": ThresholdMutation(
            slug="narrower_net",
            description=f"Raise min_candidate by {CANDIDATE_STEP}",
            rationale="Drops long-shot candidates. Cleaner dropdowns, but a real match can fall off the list.",
            apply=_narrower_net,
        ),
        "decisive": ThresholdMutation(
            slug="decisive",
            description=f"Lower ambiguity_margin by {MARGIN_STEP}",
            rationale="Accepts a narrower win over the runner-up. More resolved, more chance of the wrong one.",
            apply=_decisive,
        ),
        "cautious": ThresholdMutation(
            slug="cautious",
            description=f"Raise ambiguity_margin by {MARGIN_STEP}",
            rationale="Demands a clearer win. The safest single change against false matches.",
            apply=_cautious,
        ),
    }


def resolve_mutations(selected: Optional[Sequence[str]] = None) -> List[ThresholdMutation]:
    """The mutations to run, in a stable order.

    Sorted by slug rather than dict order so two searches over the same
    selection explore in the same sequence.
    """
    available = builtin_mutations()
    if not selected:
        return [available[slug] for slug in sorted(available)]

    unknown = [slug for slug in selected if slug not in available]
    if unknown:
        raise ValueError(
            f"Unknown mutation(s): {sorted(unknown)}. Available: {sorted(available)}"
        )
    return [available[slug] for slug in sorted(dict.fromkeys(selected))]


def variant_name(seed_name: str, mutation: ThresholdMutation) -> str:
    return f"{seed_name}-{mutation.slug}"


def generate_variants(
    seed: Thresholds,
    *,
    seed_name: str = "seed",
    selected: Optional[Sequence[str]] = None,
) -> List[Tuple[str, Thresholds, ThresholdMutation]]:
    """Apply each mutation to the seed, dropping the ones that change nothing.

    A mutation clamped flat against a rail produces the seed again. Scoring that
    would waste a run and, worse, put a duplicate of the seed on the frontier
    under a name suggesting it was a different setting.
    """
    variants = []
    for mutation in resolve_mutations(selected):
        candidate = mutation(seed)
        candidate.validate()
        if candidate == seed:
            continue
        variants.append((variant_name(seed_name, mutation), candidate, mutation))
    return variants


DEFAULT_SEED = Thresholds(
    strong_match=STRONG_MATCH, min_candidate=MIN_CANDIDATE, max_candidates=MAX_CANDIDATES
)
