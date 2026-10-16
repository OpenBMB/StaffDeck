# Native PD SOP eight-feature acceptance, 2026-10-15

The paired PilotDeck worktree implements the remaining website gaps: ordinary
and fullscreen node model selection, authoritative pinned child-node model and
context selection, native Skill failures, child graph preview, owner draft
retention, and bounded HANDOFF_REQUIRED protocol correction.

Runtime/Core code is unchanged in this increment. This repository's independent
HTTP service is the state authority for the deployed real-model tests; no
StaffDeck backend, ORM, database, identity credential or service is involved.
Final policy is knowledge=false, versionManagement=true. Existing SQLite,
definitions and 24 baseline session states remain preserved.

The real workflow collects ticket/risk, loads a local SKILL.md through read_skill,
reads release evidence, writes a verifiable file, dynamically selects high/low,
enters a pinned child SOP, waits for the logged-in PD owner, resumes automatically,
calls a second real model in the child, and returns to its parent to complete.
Both 1440px desktop and 390px mobile views are covered. Actual model requests use
Qwen3.6 and DeepSeek v4.1 Flash. Successful sessions finish at revision 11.

- High/restart, pinned parent and child 1.0.0:
  `web:s_df94b2cb-b2df-471a-ad6a-e0f4b708056c`.
- High/approval page reload, version 1.0.1:
  `web:s_c1288cfb-51d1-4875-aff8-87a6f008ecf8`.
- Desktop low, shared-field retention, version 1.0.2:
  `web:s_a79cb772-7b2c-4f55-bc99-16614a1e7c71`.
- Mobile low, version 1.0.2:
  `web:s_246b61ad-e459-4c59-a5da-8194e1ac5fa6`.
- Desktop high on the same 1.0.2 definition as the low branch:
  `web:s_8cab4941-7c89-4827-911e-17eaa41f5a8b`.

At a child approval wait, new child/parent versions were published, then all
three services restarted. The old state, revision, wait ID and fixed snapshot
compared equal. Old parent versions retain old child versions. Duplicate resume
returns the original result and creates no second continuation task.

Dedicated deployed API cases reject missing slots/receipts, wrong receipt
turn/node, undeclared capabilities, illegal successor and stale revision without
state changes. Duplicate submit is replayed. Native webpage cases reject absent
bindings and unavailable models before execution, preserving revision 0.

PilotDeck regression evidence: 88 AgentLoop/read_skill cases, the independent
HTTP Runtime conformance test, 8 frontend cases, 9 web management/outbox cases,
4 legacy approval consumer cases, TypeScript and configured production build.
Application request tracing contains zero StaffDeck requests. Only Runtime
27491, Gateway 27493 and Web 27495 are deployed; StaffDeck remains unloaded.

Baseline failures and later failed attempts are retained, including a model
rewriting output_path. The corrected 1.0.2 fixture limits child slot updates,
and its desktop/mobile retests assert shared output_path remains unchanged.
This is fixture behavior, not a general prohibition on intentional slot updates.
Real knowledge queries are cancelled; production migration and Feishu are out
of scope. No provider credentials, private trajectory or database is committed.

Full paired report and sanitized evidence are in PilotDeck:
`products/pilotdeck-staffdeck-sop/ACCEPTANCE-EIGHT-1015.zh-CN.md` and
`products/pilotdeck-staffdeck-sop/evidence/eight-1015/`.
Private evidence, failures, service configuration and verified remote version
pair are under `~/.local/share/sop-native-1014/evidence/eight-1015/`.
