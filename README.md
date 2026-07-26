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
├── clickup_bridge.py
├── comparison.py
├── config.py
├── frontier.py
├── hermes_compat.py
├── mcp_server/          # real MCP server: QA findings + CDP/adb/macOS screenshot tools
├── models.py
├── mutation.py
├── playbook.py
├── qa_findings.py
├── run_archive.py
├── search.py
├── ticket_generator.py
├── webapp/               # localhost UI: FastAPI + static vanilla-JS frontend
└── __main__.py
```

Candidate files can live in `candidates/`, with an example in `candidates/template_candidate.py`.

Two local benchmark configs are also included in `configs/` for smoke-testing
against an Ollama OpenAI-compatible endpoint on `http://localhost:11434/v1`.

## Agent Playbooks

Beyond Hermes candidate evaluation, this repo also hosts generic per-project
playbooks — one JSON config per agent under `agents/`, each scoped to its own
project instead of sharing a global config. A playbook can initialize its
target project (venv, install, env file) and run that project's complete
end-to-end flow:

```bash
meta-harness playbook list
meta-harness playbook init clickup                      # setup only
meta-harness playbook run clickup --subject <ticket-id>  # setup, then the complete flow
meta-harness playbook runs clickup                       # list recorded runs
meta-harness playbook replay clickup <run-id>             # repeat a recorded run
```

Every `run` against a subject (e.g. a ticket) is archived, so a QA flow can
be replayed later against the same subject to check it still reproduces.

See [`agents/README.md`](agents/README.md) for the config format.

## QA Findings

A SQLite-backed QA Review layer for tracking findings (project, route,
observation, severity, status, correction note) — the persistence/triage
half of a deprecated internal tool ("Seyren"), reproduced here. Critical
findings auto-escalate into a linked ClickUp correction ticket via the
existing `clickup` playbook; a failed ClickUp call never blocks a finding
from being saved.

```bash
meta-harness qa report-issue --project sigo-front --route /checkout \
    --observation "500 on submit" --severity critical
meta-harness qa list-issues --severity critical --status open
meta-harness qa close-issue <id> --note "fixed the null check"
```

`--severity` is one of `minor`/`major`/`critical`. The database defaults to
`qa/findings.db` (gitignored); override with `--db-path`.

## Localhost UI

```bash
meta-harness ui --host 127.0.0.1 --port 8877
```

Opens a browser dashboard on `http://127.0.0.1:8877` (FastAPI + a static,
vanilla-JS frontend — no build step). The "ClickUp" tab shows the QA
findings dashboard (filter/report/close, same data as the CLI) alongside a
live ClickUp ticket browser (teams → spaces → lists, hitting your real
ClickUp account). A "Linear" tab is present but disabled — coming once
Linear access is available.

## MCP Server

```bash
meta-harness mcp-server
```

Runs an MCP server over stdio exposing Seyren's original toolkit as real
MCP tools: `report_qa_issue`, `list_qa_issues`, `close_qa_issue`,
`screenshot_url` (captures a URL screenshot by driving a headless Chromium
binary directly over the Chrome DevTools Protocol — no Playwright
dependency), `screenshot_device` (Android via `adb`), `screenshot_desktop`
(macOS via `screencapture`), `list_images`, and `read_image`. Every tool is
a thin wrapper around the same functions the CLI and web UI use.

`screenshot_device`/`screenshot_desktop` are implemented and unit-tested
via mocks, but require hardware/an OS this repo isn't developed on to run
for real (no `adb`, not macOS) — they fail with a clear, typed error when
unavailable rather than silently no-op'ing.

## Generate Tickets

Upload a requirements document (PDF, `.txt`, or `.md`) and get back a
reviewed, organized batch of proposed tickets, created in ClickUp
individually or all at once — the "Generate Tickets" tab in `meta-harness
ui`. An LLM (the local, already-authenticated Claude Code CLI — no
separate API key) analyzes the document and proposes tickets in a fixed
shape (title, description, acceptance criteria, priority); nothing is
created until you review and confirm.

```bash
meta-harness tickets generate --file requirements.pdf --prefix TAM --start-number 2 --json-output proposed.json
meta-harness tickets create-all --from-json proposed.json --list-id <id>
```

`--prefix`/`--start-number` (or the matching fields in the web UI) name each
ticket in order as `"{prefix}-{NN} | {title}"` — e.g. `TAM-02 | Build login
page`, continuing a project's existing numbering. Omit `--prefix` to keep
the raw generated titles.

The generation call is harnessed for reliability, not a single blind
attempt: if Claude's output fails JSON/shape validation, the specific
error is fed back into a follow-up prompt asking it to self-correct, up
to 3 attempts total, before giving up. The web UI shows a visible
"thinking" indicator while analysis/creation requests are in flight, and
surfaces errors with a status-code-specific label (e.g. "Invalid
request", "Upstream service failed", "Service unavailable") alongside the
backend's detail message.

Priority (`urgent`/`high`/`normal`/`low`) is set as ClickUp's native
priority field, not just text in the description. Bulk creation reports a
per-ticket result — one failure never aborts the rest of the batch.

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
