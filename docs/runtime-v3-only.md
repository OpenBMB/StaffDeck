# Unified v3 execution

Autonomous turns enter through `AgentLoop` and the registered `runtime.engine` provider.
The built-in provider is Harness v3. Deployment flags, old employee metadata, engine failures
and image attachments no longer select Harness v2. Explicit v2 selections are rejected by
the management API; persisted old selections normalize to v3 without rewriting business data.

## Reuse, not parallel implementations

- `TurnCoordinator` keeps claims, leases, task scheduling and persistence.
- `SopHost` and the existing SOP runtime keep business states and input/output control.
- The existing capability host, PEP and invocation ledger remain the authorization and
  side-effect boundaries.
- Existing planning and response policies still prepare/normalize data. Their model phases
  run through the v3 phase port, including synthesis after a resumed or forced task.
- Synchronous and SSE GeneralSkill execution share `_prepare_skill_execution` and
  `_execute_skill_test`; only result delivery differs. Reading a Skill is still a resource read.
- The old standalone Skill Runner is no longer exported by the general-skills package or
  called by an execution endpoint. Historical low-level code/tests are not execution routes.

## Images

The existing attachment validator produces ephemeral `ValidatedTaskImagePayload` objects.
The activation-scoped model gateway adds those images to the current phase's user message,
then reuses the existing OpenAI Chat, Responses, Anthropic or Gemini protocol adapter.
No image-to-text helper AgentLoop is introduced. Raw image data is not written into the
Node session or SD loop checkpoints and is not inherited by other activations.
The selected model still needs vision support; provider errors are surfaced rather than
silently switching execution engines or pretending to have read an image.

## Validation gates

- Retired engine selection and fallback cannot instantiate v2.
- Sync/SSE Skill execution cannot call GeneralSkillRunner.
- The real engine sees images in planning and execution; checkpoints contain no raw image.
- SOP suspend/resume, permissions, capability receipts and live replies retain their existing
  regression coverage. Historical v2 event records remain readable and are not rewritten.
