# Hermes Agent Meta-Harness

`hermes-agent-metaharness` is the standalone outer-loop Meta-Harness repo for Hermes.

It treats `hermes-agent` as the execution backend for benchmark harness candidates and focuses on:

- candidate resolution
- benchmark evaluation orchestration
- archive reading
- run comparison
- richer baseline-vs-candidate reporting
- frontier tracking
- structured candidate mutation and search

## Origin

This project is directly inspired by the paper [Meta-Harness: End-to-End Optimization of Model Harnesses](https://arxiv.org/abs/2603.28052) and the companion [project page](https://yoonholee.com/meta-harness/).

The paper’s core argument is that LLM system quality depends not only on model weights, but also on the harness: the surrounding code that decides what context to collect, store, retrieve, and show to the model. Instead of hand-tuning that harness, Meta-Harness proposes an outer-loop optimizer that searches over harness code. Its proposer has access to the source code, scores, and execution traces of prior candidates through a filesystem, which gives it much richer diagnostic context than methods that only optimize from scores or short summaries. The paper reports gains on online text classification, retrieval-augmented math reasoning, and agentic coding, including improved TerminalBench-2 harnesses.

## How Hermes Adapts Meta-Harness

Hermes uses the same high-level idea, but adapts it to a research-safe benchmark workflow:

- `hermes-agent` owns the inner runtime: candidate protocol, benchmark integration, loop hooks, and archive writing.
- `hermes-agent-metaharness` owns the outer loop: candidate evaluation, archive analysis, baseline reuse, frontier tracking, and search.
- The current target is verifiable coding benchmarks such as TBLite and TB2, not general production chat behavior.
- Candidate search is intentionally conservative today: this repo generates deterministic wrapper candidates around a seed candidate instead of rewriting Hermes core.

In other words, the project applies Meta-Harness to Hermes by optimizing how Hermes is run on benchmarks, not by changing model weights and not by letting the production runtime self-modify.

## Boundary

`hermes-agent` owns the inner Meta-Harness runtime:

- candidate protocol
- TB2/TBLite integration
- optional loop hooks
- per-task archive writing

`hermes-agent-metaharness` owns the outer loop:

- candidate evaluation and comparison
- archive analysis
- baseline helpers
- frontier management
- mutation and search

## Current Scope

The current release provides:

- candidate resolution by explicit path or Hermes built-in candidate name
- TBLite and TB2 benchmark orchestration through Hermes
- archive parsing for `manifest.json`, `summary.json`, and `tasks/*.json`
- paired baseline-vs-candidate evaluation and reporting
- baseline reuse from an existing run or the current frontier-best entry
- task-selection comparability metadata for reused baselines
- outer-loop provenance metadata with candidate/config hashes and launcher details
- explicit task-set comparability checks for baseline-vs-candidate reports
- task-record fallback plus trace/error diagnostics in comparison reports
- a simple JSON-backed frontier with cross-platform locking
- deterministic wrapper-mutation search over generated candidate variants
- persisted dry-run search summaries for later review

## Quick Start

```bash
git clone https://github.com/howdymary/hermes-agent-metaharness.git
cd hermes-agent-metaharness
pip install -e ".[dev]"
```

Point it at Hermes with either:

- `HERMES_AGENT_REPO=/path/to/hermes-agent`
- a sibling checkout at `../hermes-agent`
- or `~/.hermes/hermes-agent`

Check that the Hermes checkout exposes the Meta-Harness benchmark/runtime
surface before running evaluations:

```bash
python -m meta_harness check-hermes --hermes-repo /path/to/hermes-agent
```

This repo currently targets the legacy Hermes Meta-Harness surface:

- `environments/benchmarks/tblite/tblite_env.py`
- `environments/benchmarks/terminalbench_2/terminalbench2_env.py`
- `environments/meta_harness/{candidate.py,loader.py,types.py}`

As of 2026-05-23, the latest published upstream release
`NousResearch/hermes-agent` v0.14.0 (`v2026.5.16`) and current `main` no longer
ship that legacy surface. Use `check-hermes` against the exact Hermes checkout
you intend to run; if it reports missing benchmark/runtime files, a Hermes-side
port or restoration of that inner runtime is required before TBLite/TB2
evaluations can execute.

If Hermes needs to run inside a managed environment, Meta-Harness can launch it
through a shell-style prefix such as:

- `--launcher-prefix "uv run --python 3.12 --extra rl"`
- `--python-executable /path/to/hermes-agent/.mh-venv/bin/python`

## Choosing a Backend

Users should choose the strongest coding backend available in their Hermes
benchmark config. Meta-Harness does not hardcode a model provider; it delegates
backend choice to Hermes through `--hermes-config-path`.

Common options:

- OpenRouter or other OpenAI-compatible hosted backends via a Hermes YAML config
- local vLLM servers for stronger self-hosted coding models
- local Ollama endpoints for smoke tests and low-cost local iteration

For example, you can point Meta-Harness at any Hermes benchmark config that
defines a stronger coding model:

```bash
python -m meta_harness evaluate-candidate \
  --candidate snapshot_baseline \
  --benchmark tblite \
  --hermes-repo /path/to/hermes-agent \
  --python-executable /path/to/hermes-agent/.mh-venv/bin/python \
  --hermes-config-path /path/to/your_stronger_backend.yaml
```

Dry-run a built-in Hermes candidate on TBLite:

```bash
python -m meta_harness evaluate-candidate \
  --candidate snapshot_baseline \
  --benchmark tblite \
  --hermes-repo /path/to/hermes-agent \
  --launcher-prefix "uv run --python 3.12 --extra rl" \
  --dry-run
```

Compare two Hermes Meta-Harness run directories:

```bash
python -m meta_harness compare-runs \
  --baseline-run /path/to/baseline-run \
  --candidate-run /path/to/candidate-run
```

Run a candidate directly against a baseline and emit a richer report:

```bash
python -m meta_harness evaluate-vs-baseline \
  --candidate candidates/template_candidate.py \
  --baseline-candidate snapshot_baseline \
  --benchmark tblite \
  --hermes-repo /path/to/hermes-agent \
  --launcher-prefix "uv run --python 3.12 --extra rl"
```

Reuse an existing baseline run instead of rerunning baseline:

```bash
python -m meta_harness evaluate-vs-baseline \
  --candidate candidates/template_candidate.py \
  --baseline-run /path/to/baseline-run \
  --benchmark tblite \
  --hermes-repo /path/to/hermes-agent
```

Run a small deterministic search over generated wrapper candidates:

```bash
python -m meta_harness search-candidates \
  --seed-candidate candidates/template_candidate.py \
  --baseline-candidate snapshot_baseline \
  --benchmark tblite \
  --hermes-repo /path/to/hermes-agent \
  --launcher-prefix "uv run --python 3.12 --extra rl"
```

Inspect the current frontier for a benchmark:

```bash
python -m meta_harness show-frontier \
  --frontier-path output/frontier.json \
  --benchmark tblite
```

## Repo Layout

```text
meta_harness/
├── archive_reader.py
├── baseline.py
├── benchmark_runner.py
├── candidate_registry.py
├── cli.py
├── comparison.py
├── config.py
├── frontier.py
├── hermes_compat.py
├── models.py
├── mutation.py
├── search.py
└── __main__.py
```

Candidate files can live in `candidates/`, with an example in `candidates/template_candidate.py`.

Two local benchmark configs are also included in `configs/` for smoke-testing
against an Ollama OpenAI-compatible endpoint on `http://localhost:11434/v1`.

## Best Practices Notes

See [`docs/META_HARNESS_BEST_PRACTICES.md`](docs/META_HARNESS_BEST_PRACTICES.md)
for a May 2026 scan of recent Meta-Harness and agent-benchmarking developments
and how they map onto this repo's next steps.

## Release Notes

This repo is intentionally research-oriented:

- it optimizes harness procedure, not model weights
- it is designed around verifiable benchmark feedback
- it keeps Hermes core stable by treating Hermes as the execution backend
- reused baselines are validated against the same task-selection hash before comparison

## Near-Term Roadmap

1. Better ranking/reporting and frontier-backed baseline policies
2. Evidence-corpus summaries and failure-taxonomy tags over archived traces
3. More expressive mutation spaces and composition
4. Trace-driven reflective candidate improvement
5. Frontier-aware search strategies
6. Stronger benchmark-aware candidate generation
