# CLI-agent orchestration

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
