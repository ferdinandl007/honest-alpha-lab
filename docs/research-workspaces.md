# Research CLI workspaces

Research execution is an explicit opt-in. The public supervisor integration is:

```python
from honest_alpha_lab.cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore

spec = CliAgentSpec.codex_research(timeout_seconds=900)
worker = CliSubagentWorker(kind, spec, FileTaskStore(task_package_root))
result = worker.run(task, prompt, tools)
```

For durable artifacts, pass `workspace_root="/dedicated/research-scratch"` to the
factory. Each run creates a unique `hal-research-*` directory under that root;
the default uses the system temporary directory. Scratch is retained after both
success and failure, but the operating system may clean its temporary directory.
The running trace records the absolute scratch path; completed state also includes
`details.research_workspace`. Archive artifacts before retention cleanup. Reusing
an existing task ID is rejected, preserving its original task package.

`CliAgentSpec.codex()` keeps the existing read-only sandbox and package-directory
working directory. Generic compatible CLI specs can opt in with
`research_mode=True, sandbox="workspace-write"`; their own executable must provide
sandbox enforcement. Generic research CLIs write `result.json` in their scratch cwd.
Research mode rejects an explicit `working_directory` and scratch roots inside
the task store. Use a dedicated scratch root outside the repository and `sources/`.

## Research freedom and admission

Researchers may collect permitted public or already licensed sources, write and
execute research code, and retain data, notebooks, scripts and findings in any
format. The catalog is a starting point. Discovery does not require a supported
formula, an admitted data source, or approved lineage. The final JSON envelope
remains compatible with the existing runner: `alpha_candidates`, `output`
(`summary` and `notes`), and `usage`. Put artifact paths and unresolved leads in
the notes. Emit `alpha_candidates: []` for discovery-only work.

Only emitted candidates require nonempty lineage IDs drawn from the immutable
task context. New sources do not become approved merely by being collected.
The existing snapshot binding and downstream numerical admission remain in
force. Researchers do not gain validator, registry, sealed-test or trading
authority. Continuous scheduling, stable job IDs, retries and archive policy
belong to the supervisor; this factory executes one bounded research task.

## Codex configuration

Verified against official OpenAI documentation on 2026-09-05:

- `--sandbox workspace-write` selects workspace-scoped writes, as described in
  the [CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli).
- `-c sandbox_workspace_write.network_access=true` enables outbound networking.
  Extra writable roots are cleared, and `/tmp` and the inherited temporary
  directory are excluded using the documented sandbox settings in the
  [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
- `-c approval_policy='"never"'` allows unattended execution without interactive
  approval prompts. The existing ephemeral, ignore-user-config and ignore-rules
  flags are retained. Administrative restrictions still apply.

The process cwd is scratch, with temporary environment paths pointing to its
`tmp/` subdirectory. Codex receives an absolute schema path in the task package
and an absolute final-output path in scratch. The parent parses and checks the
response before copying it into the task package. Existing task, prompt, schema,
state and trace bytes are checked after execution, including failure/timeout;
detected changes fail the run, with ordinary file modifications restored.
Unexpected additions to a research task package also fail the run.

## Environment and isolation limits

Both modes use an environment allowlist rather than inheriting the full parent
environment. Runtime/path/locale/home/temp variables and explicit CLI auth
(`OPENAI_API_KEY`, `CODEX_API_KEY`, `ANTHROPIC_API_KEY`, `CODEX_HOME`) are retained.
Database DSNs, `HAL_*`, validator/numerical credentials, cloud secrets and arbitrary
parent variables are not inherited. Supply only researcher CLI credentials under
those auth names. Do not put secrets in task context or command arguments.

This is **not full OS or read isolation**. Home and Codex configuration paths
remain available for CLI authentication; readable files can still contain secrets.
An arbitrary compatible executable is not sandboxed by this Python runner.
Integrity checks detect final changes, not transient writes or reads, and cannot
undo hostile filesystem restructuring or eliminate concurrent descendant races.
For a stronger boundary, deploy the supervisor worker under a separate OS user
or container with only research inputs and researcher credentials available.
Network access does not itself enforce spending or source licensing rules.

The CLI child inherits the outer worker's process group/session: this runner does
not call `start_new_session` or create a separate process group. A supervisor
that owns the worker group can terminate that group. Standalone subprocess
timeouts kill only the direct CLI child; descendant cleanup is not guaranteed,
and descendants retaining stdout/stderr can delay pipe completion. Detached
descendants may escape a group kill, so stronger cleanup requires supervisor OS
containment. Retained scratch files are not deleted by a timeout.

## Focused verification

```sh
.venv/bin/python -m pytest -q tests/test_cli_research_workspace.py tests/test_cli_agents_and_rewards.py
```

The research tests run a genuine Python CLI fixture, fetch observations from a
local HTTP server, execute generated research code, inspect child environment
and process-group inheritance, and exercise lineage, integrity and failure paths.
They make no real LLM calls and do not verify installed Codex sandbox enforcement.
