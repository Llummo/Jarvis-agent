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
./run.sh
```

`run.sh` creates the virtualenv, installs everything, runs preflight checks
and serves the web UI on `http://127.0.0.1:8877`. It is safe to re-run — it
only does the work still outstanding. `./run.sh --check` runs the checks and
starts nothing; `meta-harness doctor` reports the same at any time.

For a manual install, `pip install -e ".[dev]"` still works.

### Shipping it to someone else

```bash
./scripts/build-release.sh          # -> dist/jarvis-agent-<version>.tar.gz
```

The archive carries the application plus every dependency as a wheel, so the
recipient extracts it, runs `./run.sh`, and needs no network — and no `sudo`,
since pip is bootstrapped from the bundle when the host lacks `ensurepip`
(the default on Debian and Ubuntu without `python3-venv`).

Two things cannot be bundled, and preflight reports both if absent:
**Python 3.9+**, and the **`claude` CLI**, which installs and authenticates
separately under your own account. Everything except ticket generation, QA
review and module checks works without it.

### Tracker credentials

The ClickUp and Linear features (web UI, QA flow, ticket generation, and the
`clickup`/`linear` CLI groups) read their credentials from a `.env` in the
repo root — gitignored, so tokens never land in a commit:

```bash
cp .env.example .env   # then fill in the tokens
```

| Variable | Needed for | Notes |
|---|---|---|
| `CLICKUP_API_TOKEN` | every ClickUp operation | personal token from ClickUp Settings -> Apps |
| `CLICKUP_TEAM_ID` | optional | default for `--team-id` |
| `CLICKUP_LIST_ID` | optional | default for `--list-id` |
| `LINEAR_API_KEY` | every Linear operation | personal key from Linear Settings -> Security & access |

Real environment variables take precedence over the file. The two trackers
are configured independently: a missing Linear key never blocks the ClickUp
commands, or vice versa. Hermes benchmark evaluation needs none of them.

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
├── clickup_bridge.py     # ClickUp operations for the QA/ticket/webapp layers
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
├── embeddings/           # repository indexing + semantic retrieval
├── search.py
├── ticket_generator.py
├── trackers/             # ClickUp + Linear API clients and their credentials
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
meta-harness playbook init <agent>                      # setup only
meta-harness playbook run <agent> --subject <item-id>    # setup, then the complete flow
meta-harness playbook runs <agent>                       # list recorded runs
meta-harness playbook replay <agent> <run-id>            # repeat a recorded run
```

Every `run` against a subject (e.g. a ticket) is archived and replayable
(see the next section for the actual QA flow built on this mechanism).

No playbooks ship with the repo — ClickUp and Linear used to need one each
to reach a sibling checkout that owned the API clients, and no longer do.
See [`agents/README.md`](agents/README.md) for the config format.

## QA Findings

A SQLite-backed QA Review layer for tracking findings (project, route,
observation, severity, status, correction note) — the persistence/triage
half of a deprecated internal tool ("Seyren"), reproduced here. Critical
findings auto-escalate into a linked ClickUp correction ticket; a failed
ClickUp call never blocks a finding from being saved.

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
ClickUp account). The "Linear" tab offers the same workflows against a
Linear workspace.

### Generating tickets

Ticket generation happens entirely in the chat. One message can carry any
combination of:

- **Text** — describe the feature in your own words.
- **A document** — attach a `.md`, `.pdf` or `.txt` with the requirements.
- **Images** — paste screenshots or mockups straight into the composer with
  <kbd>Ctrl</kbd>+<kbd>V</kbd> (up to 6 per message).

Pasted images are described in words by a separate, narrowly-scoped model
call before generation runs, so ticket drafting itself keeps its no-tool
access boundary (see `meta_harness/image_input.py`). That extra step costs
roughly 40–60s on top of a normal generation, so a message with a mockup
takes noticeably longer than one without.

Drafts are always reviewed before anything is created. A batch too large to
read inside the conversation is rendered as cards below the chat instead.

## Tracker CLI

ClickUp and Linear are also driven directly from the CLI — the same
in-process clients the web UI uses, emitting JSON on stdout:

```bash
meta-harness clickup teams
meta-harness clickup lists --folder-id <id>
meta-harness clickup tasks --list-id <id>
meta-harness clickup create-task --name "Fix login" --priority high --list-id <id>
meta-harness clickup set-status --task-id <id> --status done

meta-harness linear viewer                       # cheap credential check
meta-harness linear issues --team-id <id>
meta-harness linear create-issue --team-id <id> --title "Fix login" --priority high
meta-harness linear set-state --issue-id <id> --state-id <id>
```

Priority is a word (`urgent`/`high`/`normal`/`low`) in both, mapped to each
tracker's native integer field. `--parent` (ClickUp) and `--parent-id`
(Linear) nest a new item under an existing one.

## Repository Context (embeddings)

The harness can read a codebase instead of being handed one. Point it at a
repository once and it indexes the source; from then on it retrieves the
relevant spans on demand.

```bash
meta-harness source add --repo ../sigo           # register + index in one step
meta-harness source list                         # what is trusted, and is it current
meta-harness source verify                       # has anything drifted since indexing
meta-harness index search "user permissions" --repo sigo --verify
```

### Trusted sources

A source is a repository the harness is *allowed* to draw context from. It is
registered by name, with the git revision it was indexed at, and every
retrieved span carries a citation back to it — `sigo/internal/people.go:88-140`.

That provenance is only worth something if it can be checked, so
`--verify` re-reads each span from disk and compares it to what the index
holds. Three outcomes: `verified` (matches the working tree), `drifted` (the
file changed since indexing), `missing` (it is gone). Module-relevance
retrieval **drops anything that is not verified**, so the context the model
judges against is the code as it is now, not a stale copy.

This matters because an index is a copy, and a copy goes stale silently — a
file edited after indexing still returns its old contents, and nothing about
the result looks wrong.

### In the web UI

The **Sources** tab registers three kinds of source — a folder on this
machine, a repository URL (cloned shallowly into a local cache), or a single
document (`.md`, `.txt`, `.pdf`; PDF text is extracted automatically). It
shows whether each index is current or stale, and re-indexes or removes them. It also has a retrieval
preview, so you can see what a source actually returns before trusting it to
drive a verdict — each result carries its citation, how it was found
(semantic / lexical / hybrid) and whether it still matches the working tree.

Indexing runs as a background job because it outlives an HTTP request; the
tab polls it and reports progress.

In **Module check** on either tracker, the documentation textarea is now
optional: pick a registered source instead and the harness reads the module's
real code itself.

### Symbol search

Embeddings are weakest at exactly what a symbol name is: an exact string.
Searching `_call_with_retry` semantically returns files *about* retrying;
searching it lexically returns the declaration.

So search runs both and fuses them by reciprocal rank. A lexical match inside
an indexed span is evidence *for* that span, so an exact hit promotes the
chunk that declares it rather than competing with it:

```
_call_with_retry
  semantic only  ->  test_ticket_generator.py, ticket_generator.py   (all wrong)
  hybrid         ->  embeddings/embedder.py:136   [hybrid, verified]  (the definition)
```

The lexical pass runs automatically when the query looks like an identifier,
and is skipped for prose — searching a sentence literally finds nothing.
Force it either way with `--lexical` / `--no-lexical`.

ripgrep is used when installed; otherwise `git grep`, which is always present
because indexing already needs git. `meta-harness doctor` reports which.

### Backends

Default is **local**: `voyageai/voyage-4-nano`, Apache 2.0, running on your
machine. No API key, no per-token cost, and no source code leaving the host —
which matters when the thing being indexed is a client's codebase. Weights
(~700 MB) download once on first use; after that indexing is fully offline.

Its runtime is an opt-in extra because it pulls torch (~5 GB installed):

```bash
./run.sh --with-embeddings          # or: pip install -e ".[local-embeddings]"
```

The hosted alternative is Gemini (`gemini-embedding-001`), selected with
`META_HARNESS_EMBEDDING_BACKEND=gemini` and a `GEMINI_API_KEY` in `.env`.

**The two produce different vectors and cannot share an index.** Switching
backends means rebuilding: `meta-harness index build --repo <path> --rebuild`.
The store detects the mismatch and says so rather than returning meaningless
scores.

`meta-harness doctor` reports which backend is active and whether it can run.

### Cost of the first index

Local embedding is CPU-bound. Measured on a 16-core machine with no GPU, at
**~1.9s per chunk**:

| Repository | Chunks | First index |
|---|---:|---:|
| this repo | 257 | ~8 min |
| a 2,788-file project | 4,875 | ~2.5 h |

That is a one-time cost — re-indexing only touches files whose contents
changed — but it is worth starting a large repository before you need it. A
GPU or the Gemini backend both cut it dramatically.

The index is a SQLite file under the gitignored `qa/` directory. It contains
verbatim source and must never be committed.

**What it changes.** Module relevance no longer requires a human to paste the
module's documentation into a form field. Supply `repo` instead and the
context is retrieved from the index, with each span labelled `file:line` so
the resulting verdict cites evidence you can check:

In the "Module check" tab, the module-context textarea becomes optional: name
an indexed repository instead and the harness retrieves the context itself.
The same applies to the API and the bulk sweep:

```python
analyze_module_relevance(ticket_id, module_name="People", repo="sigo")
analyze_modules_bulk(ticket_ids, module_name="People", repo="sigo")
```

Pasted documentation still wins when both are supplied — a human who typed
something meant it. The bulk sweep retrieves once for the whole run, so every
ticket in it is judged against the same text.

**How it indexes.** File discovery goes through `git ls-files` (tracked *and*
uncommitted-but-not-ignored files), so a project's own `.gitignore` does the
filtering and `node_modules`, build output and images never enter the index.
Files are split at declaration boundaries rather than fixed windows, so a
retrieved chunk is a whole function rather than half of two. Re-indexing is
incremental on a content hash per file.

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
