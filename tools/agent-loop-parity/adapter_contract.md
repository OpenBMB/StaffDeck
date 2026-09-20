# AgentLoop parity adapter contract

The parity runner never imports product internals. Each product/mode is an
explicit subprocess adapter. This keeps `origin/main` runnable even when it
does not contain the modular sidecar code.

The adapter receives:

- `PARITY_SCENARIO_FILE`, `PARITY_SCENARIO_ID`, and `PARITY_Q`;
- `PARITY_MOCK_BASE_URL` for the deterministic provider/tool service;
- `PARITY_TRACE_OUT` for the JSONL trace destination;
- `PARITY_SOURCE_ROOT`, `PARITY_SOURCE_REF`, and `PARITY_MODE`.

It must execute one scenario against the real product entrypoint and write
one JSON object per logical event. Required top-level fields are `kind`,
`scenarioId`, `q`, and `sequence`. Relevant event payloads use these kinds:
`model.request`, `model.response`, `tool.call`, `tool.result`,
`permission.decision`, `checkpoint`, `terminal`, and `user.output`.

Adapters must return non-zero on setup or execution failure. They must not
write a synthetic successful trace when the product entrypoint is missing.

Both StaffDeck modes use `staffdeck_engine_impl.py`. The adapter must call the
selected checkout's real `AgentLoop.handle_turn()` and let it enter
`HarnessV2Engine.run()`. Deterministic replacements are limited to planning,
the external model provider, and the final external tool backend. Attachment
materialization, action budgets, cancellation markers, deadlines, permission
checks, checkpoint persistence, and terminal state projection remain on the
product path. The adapter must derive terminal status from persisted turn,
run, and frame records rather than from reply text.
