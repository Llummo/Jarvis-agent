# Jarvis-agent

`Jarvis-agent` is the standalone outer-loop Meta-Harness repo for Hermes.

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
- `Jarvis-agent` owns the outer loop: candidate evaluation, archive analysis, baseline reuse, frontier tracking, and search.
- The current target is verifiable coding benchmarks such as TBLite and TB2, not general production chat behavior.
- Candidate search is intentionally conservative today: this repo generates deterministic wrapper candidates around a seed candidate instead of rewriting Hermes core.

In other words, the project applies Meta-Harness to Hermes by optimizing how Hermes is run on benchmarks, not by changing model weights and not by letting the production runtime self-modify.

## Boundary

`hermes-agent` owns the inner Meta-Harness runtime:

- candidate protocol
- TB2/TBLite integration
- optional loop hooks
- per-task archive writing

`Jarvis-agent` owns the outer loop:

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
git clone https://github.com/Llummo/Jarvis-agent.git
cd Jarvis-agent
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

## Running the Project

The Quick Start above covers the Hermes candidate-evaluation side. This section
covers running the ticket-generation and QA product — the localhost UI.

### 1. Prerequisites

| Requirement | Needed for | Notes |
| --- | --- | --- |
| Python 3.10+ | everything | |
| The `claude` CLI, logged in | generation, QA review, module checks, reformatting | already-authenticated Claude Code install; no API key is stored by this repo |
| A `p-harness` checkout | all ClickUp/Linear reads and writes | sibling directory, see step 3 |
| Chromium or Chrome | QA screenshot evidence, and the browser e2e tests | optional — QA still records the HTTP status without it |

### 2. Install the harness

```bash
git clone https://github.com/Llummo/Jarvis-agent.git
cd Jarvis-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Install and configure `p-harness`

This repo never holds tracker credentials — they live in the sibling CLI it
shells out to. Clone it *next to* this checkout (or point at it with
`CLICKUP_PROJECT_REPO` / `LINEAR_PROJECT_REPO`):

```bash
cd ..
git clone https://github.com/Llummo/personal-harness.git p-harness
cd p-harness
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env      # then fill it in
```

`p-harness/.env`:

```bash
CLICKUP_API_TOKEN=pk_xxx_xxx   # ClickUp personal token
CLICKUP_TEAM_ID=              # optional
CLICKUP_LIST_ID=              # optional default list
LINEAR_API_KEY=lin_api_xxx     # Linear personal API key
```

Verify the credentials before starting the UI — a failure here is much easier
to read from the CLI than from a dropdown that silently stays empty:

```bash
.venv/bin/harness clickup teams
.venv/bin/harness linear teams
```

### 4. Start the UI

```bash
cd ../Jarvis-agent
source .venv/bin/activate
meta-harness ui                    # http://127.0.0.1:8877
meta-harness ui --port 8879        # or any other port
```

Then open the URL and work top to bottom:

1. **Choose where to work** — space + list (ClickUp) or team + project (Linear).
   Everything below depends on this, which is why it stays pinned above the tabs.
2. **Generate tickets** — upload a `.md`/`.pdf`/`.txt`, or describe an idea in
   the chat. Review the drafts, then add them.
3. **QA review** — pick a ticket, analyze it, optionally capture real evidence
   (set a base URL first), then save the finding and move the ticket.
4. **Modify ticket** — reformat an existing ticket into the house format;
   applying it saves an undo point you can revert from the same panel.
5. **Module check** — decide whether a ticket belongs to a module, given that
   module's documentation.
6. **Findings** — everything QA has recorded.

> The UI does not hot-reload Python. Static assets (HTML/CSS/JS) are re-read per
> request, but after changing any `.py` file you must restart `meta-harness ui`
> or you will be looking at the previous build.

### 5. Run the tests

```bash
pytest                                  # everything
pytest tests/test_e2e_ui.py             # browser end-to-end only
pytest --ignore=tests/test_e2e_ui.py    # skip the browser suite
```

The e2e suite drives the real UI in headless Chromium against a stubbed API,
and skips itself automatically when Chromium is unavailable.

### Environment variables

All optional; sensible defaults apply.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLICKUP_PROJECT_REPO` / `LINEAR_PROJECT_REPO` | sibling `../p-harness` | where the tracker CLI lives |
| `META_HARNESS_CLAUDE_PATH` | `claude` on `PATH` | Claude CLI binary |
| `META_HARNESS_CHROMIUM_PATH` | Playwright cache, then `PATH` | Chromium binary for screenshots |
| `META_HARNESS_CLAUDE_TIMEOUT_S` | `1800` | ticket-generation timeout |
| `META_HARNESS_QA_REVIEW_TIMEOUT_S` | `120` | QA review timeout |
| `META_HARNESS_MODULE_CHECK_TIMEOUT_S` | `180` | module-relevance timeout |
| `META_HARNESS_QA_DB_PATH` | `qa/findings.db` | findings database |
| `META_HARNESS_PROJECT_CONFIG_PATH` | `qa/project_config.json` | per-project base URLs |

### Troubleshooting

| Symptom | Cause |
| --- | --- |
| Dropdowns stay empty | `p-harness/.env` missing or invalid — run `harness clickup teams` to see the real error |
| Generation returns 502 | the Claude CLI failed or timed out; the message now includes its output |
| Changes to a `.py` file have no effect | the UI does not hot-reload Python — restart it |
| Screenshots are blank | the target app had not rendered yet; the capture waits for paint, but a page that never renders yields a blank image |
| e2e tests skip | Chromium or `websockets` unavailable |

## Technology Stack

No ORM, no message broker, no task queue, and no frontend build step. The
whole system is a synchronous FastAPI app plus two subprocess boundaries.

### Backend — Python

| Package | Used for |
| --- | --- |
| `fastapi` + `uvicorn` | the HTTP API and the localhost UI server |
| `click` + `rich` | the `meta-harness` CLI |
| `pypdf` | extracting text from uploaded PDF requirement documents |
| `fpdf2` | rendering QA findings as downloadable PDF reports |
| `websockets` | driving Chromium over raw CDP to capture screenshots |
| `python-multipart` | file uploads |
| `mcp` | the MCP server exposing captured screenshots |
| `filelock` | cross-platform locking for the run archive and frontier |

The sibling `p-harness` CLI, which owns all tracker credentials, uses
`requests` (ClickUp REST, and Linear's GraphQL as plain POSTs),
`python-dotenv` and `click`.

### Frontend — none, deliberately

Vanilla JavaScript, HTML and CSS served straight from
`meta_harness/webapp/static/`. There is no `package.json`, no bundler and no
transpilation: what is on disk is what the browser runs. Node is used only as
a dev-time syntax checker (`node --check app.js`).

The design system is plain CSS custom properties, and the ClickUp and Linear
tabs are rendered by one tracker-parameterized layer rather than duplicated
markup.

### Storage — SQLite and JSON files

| Store | Backing | Holds |
| --- | --- | --- |
| `qa/findings.db` | SQLite (WAL, stdlib `sqlite3`) | QA findings |
| `qa/project_config.json` | JSON, atomic writes | per-project base URLs |
| `qa/reformat_history.json` | JSON, atomic writes | pre-reformat versions, for undo |
| `runs/*.json` | JSON, file-locked | replayable run archive |

Schema changes are hand-rolled `ALTER TABLE` guards; there is no migration
framework.

**Operational note.** Everything is single-node and on local disk beside the
checkout. SQLite in WAL mode tolerates several UI processes sharing one `qa/`
directory, but the JSON stores rely on atomic `os.replace` — concurrent
writers lose a write rather than corrupt the file. Fine for internal use;
revisit before running behind multiple workers.

### External processes

Two subprocess boundaries, and the reason the app holds no API keys of its own:

- `claude -p "<prompt>"` — all model calls. No tool access, text in and text
  out, so a bad response cannot mutate anything.
- `p-harness/.venv/bin/harness <args>` — all ClickUp and Linear I/O.

## Repo Layout

```text
meta_harness/
├── cli.py                # meta-harness CLI entry point
├── webapp/               # localhost UI: FastAPI routers + static vanilla-JS frontend
│   ├── app.py
│   ├── routes_clickup.py  routes_linear.py  routes_tickets.py
│   ├── routes_qa.py       routes_qa_flow.py routes_progress.py
│   └── static/           # index.html, app.js, style.css — no build step
├── mcp_server/           # MCP server: QA findings + CDP/adb/macOS screenshot tools
│
│   # tracker boundaries (shell out to p-harness, never hold credentials)
├── clickup_bridge.py     linear_bridge.py
│
│   # ticket generation and formatting
├── ticket_generator.py   # document/idea -> proposed tickets, chunked + parallel
├── ticket_format.py      # per-tracker description rendering
├── ticket_reformat.py    # rewrite an existing ticket into the house format
├── reformat_history.py   # pre-reformat versions, for undo
├── team_assignment.py    # roster verification + random assignment
│
│   # QA and analysis
├── qa_flow.py            # fetch -> analyze -> evidence -> persist -> status move
├── qa_findings.py        # SQLite-backed findings store
├── qa_report.py          # Markdown/PDF report rendering
├── module_relevance.py   # does this ticket belong to a module?
├── project_config.py     # per-project base URLs
│
│   # Hermes candidate evaluation (the original outer loop)
├── archive_reader.py     baseline.py       benchmark_runner.py
├── candidate_registry.py comparability.py  comparison.py
├── frontier.py           hermes_compat.py  mutation.py
├── playbook.py           run_archive.py    search.py
├── config.py             models.py
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

Every `run` against a subject (e.g. a ticket) is archived and replayable
(see the next section for the actual QA flow built on this mechanism).

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

## QA Ticket Review Flow (replayable)

The actual "QA flow": fetch a ClickUp ticket, have Claude analyze it, and
propose an observation + severity. **Dry-run by default** — nothing is
saved or created in ClickUp until you pass `--persist`, so replaying the
same ticket repeatedly never piles up duplicate findings.

```bash
meta-harness qa review-ticket --ticket-id <id> --project sigo-front            # dry-run
meta-harness qa review-ticket --ticket-id <id> --project sigo-front --persist  # reports a real finding
meta-harness qa review-runs                                                    # list recorded reviews
meta-harness qa replay-review <run-id>                                         # re-fetch + re-analyze, dry-run
meta-harness qa replay-review <run-id> --persist                               # replay and report for real
```

Every review (dry-run or persisted) is recorded via the same
`RunArchive`/`RunRecord` mechanism the agent playbooks use, so `replay-review`
re-runs the exact same ticket through a fresh live analysis and reports
whether it reproduces. The analysis call is harnessed the same way ticket
generation is (bounded retry with the specific validation error fed back
on a malformed response).

**Also available in the web UI** — the "Review Ticket (QA Flow)" panel
under the ClickUp tab. No typing required: click a task in the ClickUp
browser above it to select it, pick a project from the dropdown (prefilled
with the currently-browsed ClickUp space plus any project already used in
existing findings), then "Analyze". Persisting from the UI saves *exactly*
what was previewed — it doesn't silently re-run the analysis, so what you
review is what gets saved. Past reviews are listed in a dropdown with a
"Replay" button.

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
shape (title, description, acceptance criteria, priority, category);
nothing is created until you review and confirm.

Each ticket is auto-classified into one of four categories, each with its
own naming sequence:

| Category | Prefix |
|---|---|
| mundane (general/cross-cutting/planning) | `TAM` |
| backend | `TAB` |
| frontend | `TAF` |
| deployment | `TAD` |

```bash
meta-harness tickets generate --file requirements.pdf --start-mundane 2 --json-output proposed.json
meta-harness tickets create-all --from-json proposed.json --list-id <id>
```

Titles come out as `"{PREFIX}-{NN} | {title}"` — e.g. `TAM-02 | Build login
page` — numbered independently per category in the order tickets are
proposed, so `--start-mundane`/`--start-backend`/`--start-frontend`/
`--start-deployment` (or the matching web UI fields) let you continue an
existing sequence in just one category without disturbing the others.

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
