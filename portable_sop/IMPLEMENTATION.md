# SOP Runtime Decoupling Implementation

Both repositories use `standalone_alpha1_decouple_sop_1013` in their existing
task worktrees. Current delivery evidence is recorded in ACCEPTANCE-1014.md.

## Delivered

- `sop.lifecycle/v3` JSON contracts and documented ownership/revision rules.
- Independent Python Core: graph validation, slots, capability receipts,
  waits, transitions, terminal validation, nested expansion and depth/cycle checks.
- Nested publication pins child versions. Employee import recursively copies
  pinned children; gallery publication serves both gallery and overall-agent scope.
- Human nodes require a resumed approval before completion; an approval cannot
  be reused when a graph loop returns to the same node.
- HTTP session API with Runtime-owned state and pinned definition snapshots.
- SQLite CAS, request replay, atomic state/event/request transactions and recovery.
- In-memory stores and replaceable capability/approval host ports.
- StaffDeck HTTP adapter, handoff reply and external completion integration.
- PilotDeck v3 client, AgentLoop integration and Runtime-authoritative recovery.
  PilotDeck retains model contexts and local delivery/recovery projections.
- Shared StaffDeck definition management service, new definition/version/draft
  tables, internal/public API cutover and a repeatable legacy read migrator.
- Employee SOP import, sync, promotion, rollback, copying and resource summaries
  use the new management records after cutover.
- Process-wide ORM guard closes mutations of legacy SOP tables after cutover.
- SOP-only Docker/export does not copy or install StaffDeck backend.

## Verification

- Python focused regression: 246 passed, covering Core/Runtime, both management
  APIs, migration, StaffDeck adapter, handoff/external resume and employee flows.
- Independent Python environment without backend on PYTHONPATH: 26 passed.
  SQLite reopen, nested capability scope, receipts, revision and replay are covered.
- Real HTTP process stop/restart verifies persisted waits, revision, submit/resume
  replay and completion across four independent Runtime processes.
- Both host adapters consume the same 10 JSON conformance cases, including
  tool/skill/knowledge doubles, rejected transitions/receipts/slots and all waits.
- PilotDeck full TypeScript compilation passed in an isolated dependency checkout.
  The shared worktree dependency symlink was preserved.
- PilotDeck focused tests: 136 passed, including real independent HTTP Runtime,
  model/context behavior, capability receipts, approval/external recovery and export.
- Docker image built successfully. Container initialization verified that neither
  `app` nor `staffdeck_harness` is importable and SQLite Runtime initializes.
- Scoped Ruff checks and both repositories' `git diff --check` passed.

The completion goal covers implementation and local acceptance. Production
deployment, live business database cutover and real external services remain
operational steps, with their limits recorded below.

## Operational Cutover

Follow README.md: install the independent package in the StaffDeck host,
configure the Runtime endpoint, migrate every tenant/employee/gallery scope,
drain existing legacy sessions, then set `SOP_RUNTIME_LEGACY_WRITES=disabled`
on every worker. Compatibility mode exists only for this migration window.

Real-model local HTTP acceptance now covers both hosts, real host capability
execution, StaffDeck management and browser approvals. Production deployment,
live business database migration, StaffDeck Feishu and PilotDeck public browser
approval remain incomplete. PilotDeck real Feishu delivery and four-node business
execution are verified in ACCEPTANCE-1014.md.
Existing mutable legacy sessions are not imported; they must finish before cutover.

Runtime publication and the StaffDeck management database are separate stores.
Publication failure rolls back the local management transaction. A successful
remote publication followed by local failure may leave an immutable published
snapshot; retrying the same version is supported. This is not a distributed
transaction. Conditional expressions remain host decisions; Core validates
graph successors and does not execute arbitrary expression strings.
