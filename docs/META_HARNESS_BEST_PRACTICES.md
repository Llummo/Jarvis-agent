# Meta-Harness Best Practices Notes

Last reviewed: 2026-05-23.

This repo is intentionally a conservative outer loop around `hermes-agent`.
The notes below summarize recent harness-optimization and agent-benchmarking
work that is directly applicable to this codebase.

## Recent Developments

- Meta-Harness introduced an outer-loop optimizer that searches over harness
  code while giving the proposer filesystem access to prior candidate source,
  scores, and execution traces. The direct lesson for Hermes is to archive
  enough source, trace, score, and provenance evidence for later proposer or
  reviewer agents to diagnose why a candidate changed outcomes.
  Source: https://arxiv.org/abs/2603.28052

- HARBOR frames harness optimization as constrained, noisy, cost-aware search
  over a mixed configuration space. For this repo, deterministic wrappers are a
  good safe first layer, but search summaries should keep enough trial metadata
  to support later cost-aware ranking and safety constraints.
  Source: https://arxiv.org/abs/2604.20938

- Recent executable benchmark-suite work separates workloads, drivers, admitted
  evidence, diagnostics, replay policy, and provenance. For Hermes, baseline
  reuse and frontier promotion should depend on admitted metrics and comparable
  task selections, while trace files and diagnostics remain available for audit.
  Source: https://arxiv.org/abs/2605.11030

- Priority-ranking work argues that harness optimizers should be evaluated not
  only by final score improvements but also by whether they can identify which
  harness components are worth changing. A future Hermes search planner should
  record why a mutation was selected, not just the final pass rate.
  Source: https://arxiv.org/abs/2605.22505

## Repository Implications

The practical standard for this repo should be:

- never compare baseline and candidate runs unless their task set is explicit
- record content hashes for candidate and config files when available
- preserve the exact launcher and command used for each benchmark run
- keep failed searches auditable by writing partial summaries
- keep frontier entries separated by task-selection identity
- prefer deterministic admitted metrics for promotion decisions
- use traces and per-task archives to explain results, not to rescue an
  otherwise failing candidate

## Current Implementation Status

Implemented in this repo:

- outer-loop manifest schema versioning
- task-selection hashes in run manifests and frontier entries
- candidate/config content hashes when files are available
- exact command, launcher prefix, Python executable, and Hermes repo path in
  provenance
- explicit `baseline_only` and `candidate_only` task statuses
- candidate promotion gating on a comparable task set
- partial search-summary persistence when a fresh baseline run fails
- CI coverage for tests plus installed-wheel smoke checks

Recommended next steps:

1. Load `tasks/*.json` records into comparison reports when `summary.json` lacks
   enough per-task detail.
2. Add trace pointers or error summaries to task-level deltas.
3. Add a planner interface that ranks candidate mutation targets before search.
4. Add repeated-run or bootstrap confidence intervals before frontier promotion.
5. Extend comparability metadata to include dataset/config identifiers exposed
   by `hermes-agent`.
