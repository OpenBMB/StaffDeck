# PR #286 Review and Acceptance

Review baseline: `b01c8b704455993741be85fb72ef7aac0d563395`.
Date: 2026-10-16. Branch: `standalone_alpha1_decouple_sop_1013`.

## Fixed Findings

1. A nonterminal SOP node consuming the last action marked the entire task
   completed. The adapter now leaves the task resumable with `action_budget`
   while retaining the successful node result.
2. Capability receipts containing a resource ID failed tool-name requirements.
   Receipt aliases now cover resource IDs and names from both expanded
   descriptors and the compact catalog, with distinct receipt operation IDs.
3. Direct HTTP adapter handoff creation overwrote the Runtime session pointer.
   The adapter preserves the mapping and attaches the created handoff artifact;
   an actual handoff reply service regression verifies continuation.
4. Staff composition expanded the latest child SOP instead of the version pinned
   by its published parent. Cutover composition now expands immutable version
   records through the portable nesting implementation.
5. Resuming the same node reused its old model input and omitted newly supplied
   user information. Changed TaskRequirements are now appended to canonical
   model context; identical input is not duplicated.
6. A dedicated handoff node returning `awaiting_user` was rejected by Runtime.
   The host now projects that result to `handoff`. Runtime still owns approval
   waits and rejects completion without a resumed approval.

Also updated the sidecar result test to assert the operation ID introduced by
this PR.

## Verification

The focused Python suite covers portable Core/HTTP/SQLite, management cutover,
HTTP adapters, actual handoff and external-task reply services, local SOP source,
capability isolation/authority, Harness v2 coordination and the sidecar client.
All 267 tests pass. `git diff --check` passes. The modified adapter and focused
module regression files pass Ruff. Existing lint findings in coordinator,
composition and legacy test imports were confirmed against the review baseline.
Frontend TypeScript checking and the production Vite build pass; Vite retains
its existing large-chunk warning.

## Real Process Acceptance

Isolated current-source StaffDeck and portable Runtime processes used separate
file-backed SQLite databases, the real PilotDeck sidecar, and the configured
external `provider1/deepseek-v4-flash-0731` model. No production database was used.
The local HTTP tool, Skill package and knowledge document were controlled
fixtures exercised through their real host implementations.

- Internal/public create, edit, publish, nested SOP and version-list HTTP calls
  passed. Legacy Skill/version/draft row counts stayed unchanged after cutover.
- Two user turns collected `SOP-1014`, requested operator approval, accepted an
  authenticated reviewer reply, automatically resumed, and completed at Runtime
  revision 7.
- Tool, Skill and knowledge calls succeeded with observed results and three
  consumed receipt IDs. The approval continuation completed at revision 8.
- StaffDeck and Runtime were stopped while waiting for approval and restarted
  against the same databases. State was unchanged before the reply, and the
  authenticated reply automatically continued to `completed`.
- Authenticated `/workspace/chat` rendered at 1440x1000 and 390x844 with no
  page errors or horizontal overflow. Screenshots were inspected.

Private raw evidence directory:
`/var/folders/xd/mml9c6fj2g95x40hgf_n6lrr0000gn/T/staffdeck-pr286-real-93uiaoev/evidence`.
Reports: `review-acceptance.json`, `staffdeck-http.json`,
`staffdeck-capabilities.json`, `browser.json`; screenshots: `desktop.png` and
`mobile.png`. Credentials remain outside the repository. Acceptance processes
were stopped after completion.

## Boundaries

An earlier Qwen run timed out during an external model call; it is not counted
as a pass. The successful capability run included one rejected
`capability_describe` request before the actual calls succeeded: the existing
snapshot sidecar manifest does not advertise that discovery helper. This does
not establish full discovery parity. Budget exhaustion and pinned child-version
regressions were verified with deterministic tests, not external-model scenarios.
This acceptance does not deploy or migrate production, or certify the entire
repository test suite.
