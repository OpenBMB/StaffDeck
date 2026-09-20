# PilotDeck AgentLoop Integration

StaffDeck keeps `TurnPlanner`, `TaskFrameStore`, leases, SQL persistence, and the
public response path. When `PILOTDECK_AGENT_LOOP_ENABLED=true`, each TaskFrame is
executed by the configured PilotDeck sidecar as one Module Protocol v2 operation.

The sidecar command is configured with `PILOTDECK_AGENT_LOOP_COMMAND`, for example
`node /path/to/PilotDeck/dist/src/cli/pilotdeck-agent-loop-sidecar.js`. The default
factory is built into PilotDeck; a custom factory can be selected with
`PILOTDECK_AGENT_LOOP_FACTORY`.

StaffDeck remains the owner of model access, capability authorization, permission,
leases, invocation records, and checkpoints. The sidecar only owns the model/tool
loop and canonical event assembly. If the sidecar exits after a capability call,
the operation is treated as result-unknown until the existing invocation records
reconcile it; it is never blindly replayed.

## StaffDeck to generic sidecar mapping

The StaffDeck adapter projects its host-owned request into the generic sidecar
payload. For parity-sensitive executions it uses `contextOverride`:
`systemPrompt` is the host-selected prompt, `messages` is the ordered canonical
history, and `metadata` carries opaque execution observations. The serialized
TaskRequirement remains a compatibility `task.prompt` fallback; the sidecar does
not need to know any StaffDeck type or field name. `contextOverride` wins over
the ordinary `agent`, `messages`, and `tools` fields, and an explicitly supplied
message list suppresses task-prompt fallback.

TaskFrame attachment descriptors remain metadata-only. Validated image data is
projected only into transient canonical image blocks in
`contextOverride.messages` for the current execute request; it is never written
to the requirement, checkpoint, trace, or invocation record. The host's current
action budget is carried under `executionContext.remainingActions` and mirrored
in generic metadata as an observation; exact action-budget enforcement remains
owned by StaffDeck until a generic AgentLoop contract is introduced.

Only a checkpoint field explicitly named `agentLoopSeedState` is projected to
the generic `seedState` field. Other Harness checkpoint data remains
StaffDeck-owned and opaque.

For the active execute attempt, the adapter accepts events only when
`runId`, `operationId`, `requestId`, `streamId`, and the next `sequence` all
match. Events from an older request on the same operation are ignored. A
cancel observed by StaffDeck wins over a later completed event; if cancellation
is not confirmed before the grace period, the result remains `result_unknown`.
Established side effects are reconciled by the host rather than replayed.

Permission is fail-closed when no StaffDeck checker is configured. Model module
errors are returned to the PilotDeck AgentLoop unchanged so its normal retry and
failure handling remains authoritative; the adapter does not fall back from a
failed JSON action to a text generation call.

The sidecar server enforces the earlier of `attemptDeadline` and
`operationDeadline`, maps `max_turns` to a failed terminal outcome, and preserves
the complete AgentLoop result in the final event. The StaffDeck client maps
deadline, process exit, and unknown side effects to its existing reconciliation
and fencing paths.
