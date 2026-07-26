# Agent Playbooks

This directory holds one JSON file per agent. Each file is that agent's
config, kept separate from every other agent's — nothing here is shared
globally.

A playbook describes:

- where the agent's target project lives (`project_env_var` for an explicit
  override, `project_sibling` for a checkout next to this repo)
- `setup`: steps to initialize that project (venv, install, env file, ...)
- `flow`: steps that make up the agent's complete end-to-end run

```json
{
  "name": "clickup",
  "description": "...",
  "project_env_var": "CLICKUP_PROJECT_REPO",
  "project_sibling": "p-harness",
  "setup": [["python3", "-m", "venv", ".venv"]],
  "env_example": ".env.example",
  "env_file": ".env",
  "flow": [[".venv/bin/harness", "clickup", "teams"]]
}
```

Use it from the CLI:

```bash
python -m meta_harness playbook list
python -m meta_harness playbook init clickup
python -m meta_harness playbook run clickup
```

`playbook init` only runs `setup`. `playbook run` runs `setup` then `flow` —
the complete flow.
