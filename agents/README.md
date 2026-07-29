# Agent Playbooks

This directory holds one JSON file per agent. Each file is that agent's
config, kept separate from every other agent's — nothing here is shared
globally.

No playbooks ship with the repo right now. ClickUp and Linear used to have
one each, pointing at a sibling `p-harness` checkout that owned the API
clients; those clients now live in `meta_harness/trackers/`, so driving
either tracker no longer needs a playbook — use `meta-harness clickup ...`
and `meta-harness linear ...` instead. The mechanism below stays for
projects that genuinely live in another repo.

A playbook describes:

- where the agent's target project lives (`project_env_var` for an explicit
  override, `project_sibling` for a checkout next to this repo)
- `setup`: steps to initialize that project (venv, install, env file, ...)
- `flow`: steps that make up the agent's complete end-to-end run

```json
{
  "name": "my-agent",
  "description": "...",
  "project_env_var": "MY_AGENT_PROJECT_REPO",
  "project_sibling": "my-project",
  "setup": [["python3", "-m", "venv", ".venv"]],
  "env_example": ".env.example",
  "env_file": ".env",
  "flow": [[".venv/bin/my-cli", "process", "--id", "{subject_id}"]]
}
```

A flow step can reference `{subject_id}` — a placeholder for whatever single
item the agent is processing (a ticket id, an issue id, ...). If any flow
step uses it, `playbook run` requires `--subject`.

Use it from the CLI:

```bash
python -m meta_harness playbook list
python -m meta_harness playbook init my-agent
python -m meta_harness playbook run my-agent --subject <item-id>
```

`playbook init` only runs `setup`. `playbook run` runs `setup` then `flow` —
the complete flow — and records the result.

## Replay

Every `playbook run --subject ...` is archived (per agent, under `runs/`,
gitignored — it can contain real ticket content). List and repeat past runs:

```bash
python -m meta_harness playbook runs my-agent      # list recorded runs
python -m meta_harness playbook replay my-agent <run-id>
```

`replay` looks up the recorded run's subject, re-runs the flow against that
same subject, and records the replay as a new run — so you can check
whether a QA flow still reproduces the same result after changing a
prompt, a heuristic, or the underlying code.
