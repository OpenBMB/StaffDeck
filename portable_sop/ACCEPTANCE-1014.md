# SOP v3 Local Acceptance, 2026-10-14

Delivery branch in both repositories: `standalone_alpha1_decouple_sop_1013`.
The overall delivery goal remains active until deployment, migration and live
channel acceptance pass. Local evidence directory:
`/Users/a1/.codex/sop-acceptance-1014/evidence` (private, not committed).

## Environment

- Python 3.12, Node 22.23.1; isolated PilotDeck dependency checkout.
- Independent Runtime: `127.0.0.1:26391`, separate `runtime.sqlite`.
- Complete StaffDeck backend: `127.0.0.1:26390`, separate `staffdeck.sqlite`.
- PilotDeck standalone Gateway: `127.0.0.1:26393`, WebSocket `/ws`.
- StaffDeck frontend: `127.0.0.1:26394`, tenant `sop-acceptance`.
- Model: `provider1/qwen3.6-flash-distill`, existing private configuration.
- Desktop dotenv is read only by the channel runner; credentials are never
  copied into this report, repository or exported deployment.
- Local migration backup: `pre-migration.sqlite`; employee definitions migrated:
  1; gallery fixture count: 0. This does not prove all-tenant production migration.
- All local StaffDeck workers use `SOP_RUNTIME_LEGACY_WRITES=disabled`.

## Results

| Case | Status | Evidence and limit |
| --- | --- | --- |
| Independent Runtime, no backend import | PASS | Core/Runtime focused tests and independent process recovery |
| Internal/public management edit, publish, version, child snapshot | PASS | `staffdeck-http.json`; old counts stayed Skill=1, SkillVersion=1, APISOPDraft=0 |
| StaffDeck real model, ticket collection, Tool/Skill/Knowledge, human reply, completion | PASS | `staffdeck-capabilities.json`; session `session_80c1132f9f594100`, handoff `handoff_adca6e809b5a4096`, Runtime revision 8 |
| PilotDeck real model, Tool/Skill/Knowledge, handoff, host-control resume, completion | PASS | `pilotdeck-real-model.json`; session `real-model-sop-smoke-1791954754286`, revision 3; public browser authorization is separate |
| Both adapters, same 10 conformance cases | PASS | Required receipts, invalid successor, missing slot, user/approval/external waits; doubles |
| Same four-node business flow through both real hosts | PASS | `pilotdeck-business-graph.json`; collect/verify/approval/done, ticket SOP-1014, revision 8; each host uses its own capability bindings |
| Gateway real WebSocket model/tool execution | PASS | `gateway-ws.json`, session `gateway-ws-1791955017455`, read_file and handoff |
| Runtime live restart | PASS | `restart.json`; 19 recovered sessions, 18 identical, 1 actively advanced |
| Historical submit/resume replay after restart | PASS | `live-replay.json`; 66 original responses returned unchanged |
| SQLite process restart, old revision, receipt/slot rejection, nesting cycle/depth | PASS | Runtime session/Core/process tests |
| StaffDeck browser approval, desktop 1440 and mobile 390 | PASS | `browser-staffdeck.json`, screenshots; reply session IDs match created sessions, no horizontal overflow |
| PilotDeck browser bearer approval, desktop 1440 and mobile 390 | PASS | `pilotdeck-public-approval.json`; normal installation-owner registration, original session admission, real discovery/model callback, normal StaffDeck reviewer login, original inbox, reply replay and browser send; both sessions complete at revision 3 |
| PilotDeck host restart at human wait | PASS | Same browser evidence; Gateway and StaffDeck processes have new PIDs, original approval status stays identical, model continuation completes |
| StaffDeck own session restart at human wait | PASS | `staffdeck-process-restart.json`; session `session_14b7fde092804e89`, handoff `handoff_c9f163e081194029`; host PID 61884 to 62064, normal reviewer reply, Runtime completed revision 3 |
| PilotDeck real Feishu inbound, model/tool, wait, host-control resume, final delivery | PASS (host-control scope) | `feishu.json`; incoming `om_x100b63c57fdb88acb0bbeab6f4007e7`, outgoing `om_x100b63c579088ca4cefaeb4b3716d74`, Runtime revision 3; browser reviewer approval is not covered |
| StaffDeck real Feishu inbound, wait notification, reviewer API resume and final delivery | PASS (delivery record scope) | `staffdeck-feishu.json`; session `session_2fd7b89933ec499e`, handoff `handoff_97671e36a2184b15`, inbound `om_x100b63c539e69938b0f95cd4929de85`, Runtime revision 5, final delivery `chdlv_cca3bba558dd47da`; provider history permission denied, remote reply ID unavailable |
| Production /dsh deployment and real migration | BLOCKED | SSH user/alias and verified server access unavailable |
| Main instance cutover | NOT STARTED | Requires accepted deployment, migration, cutover window and rollback plan |
| All-tenant migration command preparation | PASS (local fixture scope) | `--all-tenants --report` covers gallery/overall/employee scopes; 14 management tests pass, including two tenants and preserved drafts/repeated migration. Real CLI against isolated DB copy publishes one definition; gallery fixture remains empty. Production migration is separate |

Current focused validation: Python 135 passed; latest adapter/resume regression 8 passed;
StaffDeck UI 8 passed and production build passed; PilotDeck TypeScript build
passed, 136 non-export tests passed, 8 export tests passed after supplying the
actual StaffDeck source path. Historical earlier counts are not added to these.
Independent package test entry point: 26 passed with only its source on PYTHONPATH;
53 legacy compatibility tests pass in the host test directory. The PilotDeck
independent HTTP Runtime recovery test passes with the configured Python/source.
Final management/adapter/resume regression: 22 passed; PilotDeck export/config/
project binding regression: 35 passed. These overlap earlier suites.
Public approval follow-up: independent HTTP Runtime regression 1 passed (including
scoped definition projection/repeated prepare), frontend assembly/wait/inbox 12
passed; TypeScript build, UI typecheck, UI production build and diff checks pass.

## Acceptance Fixes

- StaffDeck bridge capability results now retain their real invocation IDs.
- Human replies mark the existing SOP task frame ready to resume; Runtime stays
  authoritative. Pending Runtime resumes take priority over planner completion.
- StaffDeck submit/user-resume IDs include the Runtime revision, avoiding collisions
  when one execution loop spans multiple waits or revisits the same approval node.
- Legacy v2 tests live in the host suite, keeping the independent package's default
  test entry point free of StaffDeck imports.
- PilotDeck real acceptance config supplies Knowledge tenant/actor scope from the
  host binding, matching the model-visible tool contract.
- PilotDeck project bindings select v3 and explicitly map tenant/agent SOP IDs.
- PilotDeck Runtime snapshot projection retains the local SOP ID in the host
  bundle, so scoped prepare requests preserve the pinned version across reads.
- Existing SOP management/approval UI can assemble against lifecycle v3. The
  public inbox waits for normal reviewer login; external-task controls do not
  report the human approval route's missing bearer as their own failure.
- `run-public-approval-v3.mjs` records real desktop/mobile browser approval and
  both process-restart cases. PilotDeck's current interaction prepares the
  continuation in the composer; the browser test clicks the ordinary send button.
- StaffDeck mobile chat starts with the icon sidebar and constrains header and
  statistics layout; browser tests use unique session markers.
- Seed scripts target only the isolated acceptance SQLite and preserve the first
  migration backup. Evidence remains outside git.
- cc-connect was suspended only during channel isolation and restored after each
  host test. The isolated StaffDeck binding is disabled; the shared app-server
  and supervisor were kept running.
- Feishu reaction calls and message-history reads are denied by bot permissions.
  Both hosts delivered workflow replies. StaffDeck's successful send is recorded
  by its durable outbox; its remote reply ID still needs permission or API evidence.

## Deployment Gate

Inspect actual /dsh processes, workers, versions and databases before changing
them. Back up the actual database, pause management writes, drain old sessions,
migrate all tenant/employee/gallery scopes and compare definitions, versions,
drafts and publications. Disable old writes on every worker, then test management,
execution, approvals, Feishu and restart. New Runtime state must survive rollback:
pause new executions and retain its SQLite; never replace it with the old DB.

The local branch is reviewable even while live channel and server access are
blocked. Pushing the branch does not authorize main-instance cutover.
