# CLI-agent orchestration

CLI launches fail closed unless trusted configuration explicitly sets
`allow_unmetered_provider: true`. This acknowledges possible provider billing;
it does not verify or enforce a token or dollar cap. The runner limits wall time
to the smaller job/spec timeout and checks returned proposal counts and usage,
but provider usage in the final response is self-reported. A zero data-cost
budget does not mean model execution is free.

For `run-symbolic` and `build-strategies`, use `--allow-unmetered-provider` only
when accepting that risk. Campaigns use `payload.allow_unmetered_provider` in
their immutable configuration; existing campaigns require a new version when
changing it. Python launchers pass the same boolean to `CliAgentSpec.codex`,
`CliAgentSpec.codex_research`, or `run_strategy_agent`. Examples default to false.

Tool registration requires `max_cost_usd`, a trusted per-call upper bound
including failure charges; audited free adapters explicitly pass zero. The
router reserves it atomically before execution, retains uncertain charges,
and refunds only known unused amounts after a successful audited result.
Adapters must honor this maximum at their provider; an estimate is insufficient.
Router accounting is in-memory and must not be treated as a durable spending
limit across restarts. Worker `usage.data_cost_usd` excludes router charges,
which the orchestrator adds separately.

`CliSubagentWorker` makes Codex CLI (or another compatible CLI) a constrained proposal worker. Each invocation creates `var/agent-tasks/<task-id>/` (the location is configurable) containing:

- `task.json`: immutable job, context hash, input snapshot, and approved logical tools;
- `prompt.md`: the versioned research prompt plus the proposal-only boundary;
- `output.schema.json`: the only permitted final response shape;
- `events.jsonl`, `stderr.log`, `result.json`: unmodified process outputs;
- `state.json` and append-only `trace.jsonl`: execution state and tamper-evident hashes in the parent platform trace.

The Codex preset invokes non-interactive `codex exec` in ephemeral mode with JSON events, a JSON output schema, an output-last-message file, a read-only sandbox, and no dangerous bypass flags. It ignores user configuration and local rule files, so desktop MCP connections, unrelated skills, and local hooks do not become part of the research task; Codex authentication remains external to the task package. The agent starts in its task directory. It can propose only `{name, specification, lineage_ids}`; the platform supplies the candidate ID, agent kind, snapshot hash, and initial `proposed` status after verifying that every lineage ID was in the immutable task context.

Use `CliAgentSpec` to name another CLI executable. Its command is passed as an argument array, never through a shell. Non-Codex adapters must implement the same file output contract. The task package does not contain a registry connection, sealed-test artifact, or data-service credentials. Logical tools are declared for audit; an integration that exposes real datasets must implement those tools behind the existing allowlisted `ToolRouter`, not give the CLI agent arbitrary worker credentials.

For alternative-data discovery, the CLI prompt additionally requires a source key and a dated asset-applicability proposal. The response is still proposal-only: the platform’s separate source-to-security mapper requires a reviewed exposure table before an external observation can become a security-level feature. See [asset applicability](asset-applicability.md).

The checked-in [example configuration](../config/cli-agents.example.json) is intentionally credential-free. A production launcher should instantiate a `FileTaskStore`, register one `CliSubagentWorker` per `AgentKind` on `SubagentOrchestrator`, and set an explicit execution budget on every `ResearchJob`.
