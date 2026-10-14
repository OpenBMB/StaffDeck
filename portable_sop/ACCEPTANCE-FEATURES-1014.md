# SOP deployment feature acceptance — 2026-10-14

**PASS for the two-switch increment.** Final installed Runtime policy is
`SOP_KNOWLEDGE_ENABLED=false`, `SOP_VERSION_MANAGEMENT_ENABLED=true`.
Both switches default to true for compatibility, are read at process startup,
and require a Runtime restart. No browser settings page was added.

Only Runtime/Management (27491), PilotDeck Gateway (27493) and PilotDeck Web
(27495) are running. The previous StaffDeck LaunchAgent remains unloaded;
its data is preserved. Build and services use the same private PD profile.
SQLite and service configuration backups were retained before installation.
All 13 pre-existing session states compare equal after acceptance.

## Policy and API evidence

`features` on `/v1/management/call` returns `{knowledge, versionManagement}`.
PD capabilities and generation receive this same policy. The four combinations
were exercised against the installed package and the deployed webpage:

| Knowledge | Version management | API + 1440px/390px ordinary and fullscreen editors |
| --- | --- | --- |
| off | off | PASS |
| off | on | PASS — final configuration |
| on | off | PASS — entry/binding checks only |
| on | on | PASS — entry/binding checks only |

Knowledge-off checks reject explicit knowledge nodes, bindings and requirements
with `SOP_FEATURE_DISABLED`. JSON import, management create/validate, direct
snapshot publication and execution are covered. Direct publication of a parent
with a knowledge-dependent child is also rejected. Existing knowledge-dependent
sessions cannot advance while off; rejected operations preserve their state.
Child dependencies are resolved and checked when publishing; unresolved draft
references remain editable. Ordinary SOPs remain executable.

Version-off checks reject `list_versions`, `get_version`, and `rollback` with
`SOP_FEATURE_DISABLED`. Editing, real-model generation and publication continue
without a manual version input. Each publication creates a new immutable
`snapshot-*` version. Reenabling the feature restores the history/detail/rollback
UI; rollback changes the pointer and preserves the complete version records.

## Real webpage and recovery evidence

Both 1440px and 390px cases used the configured `qwen3.6-flash-distill` through
the PD model port, with both switches off: webpage generation → name edit/save
→ publication → owner wait → second edit/publication → restart all three
services → normal owner approval → automatic completion. New sessions used the
second snapshot; the original sessions retained the first. Wait ID, revision,
slots and pinned definition survived restart unchanged. Both final states are
`completed`, revision **4** (wait 1 → resume 2 → complete 4).

| Width | Runtime session | Fixed original snapshot |
| --- | --- | --- |
| 1440 | `web:s_20a1506c-ef23-44c9-8fdc-7c27ec0d1197` | `snapshot-ec91813f0ca9483a99381c778c4ac826` |
| 390 | `web:s_40160c6d-73e7-408f-bb50-9ea8f1f13801` | `snapshot-0aa87de6bb0a4006b4c3b75257989337` |

Each original resume replay returns its original revision 2 and retains exactly
one continuation task. Both widths verified restored history, details and
rollback without rewriting either version. Mobile inspection additionally
checks panel bounds and horizontal overflow. Acceptance fixed a clipped mobile
inspector and a missing explicit close control in the version detail dialog.

Raw private evidence is under
`/Users/a1/.local/share/sop-native-1014/evidence/features-1014`:
`matrix.json`, `real-browser.json`, `deployment-final.json`, 24 matrix screenshots,
and real-model publication/completion/detail screenshots at both widths.
The raw mobile harness recorded a cleanup restart conflict after its cases
passed; the independent matrix run and final live checks resolved and verified
the final policy. That raw result is retained as `real-browser-raw.json`.
Application fetch/HTTP/Undici tracing contains only Runtime, Gateway and the
model origin: **0 StaffDeck requests**. This is application tracing, not a packet
capture. No credentials or database copies are committed.

## Focused checks and scope

- Independent Core/Runtime/Management tests: **40 passed** (`python-tests.txt`).
- PD HTTP/native and legacy StaffDeck adapter regressions: **95 passed**.
- Final PD router/worker/outbox/binding/page checks: **15 passed in 5 files**;
  public approval consumer: **4 passed**. Earlier feature suite: 20 passed.
  These runs overlap and are not added into a unique test count.
- Backend TypeScript compile, frontend typecheck, configured production Web
  build and both repositories' diff checks passed.

Real knowledge queries are **cancelled for this round**, not accepted. The
knowledge-on matrix uses an unavailable binding only for catalog filtering and
editor checks; no knowledge query is claimed. Production migration and Feishu
retesting remain outside scope.

Both branches are `standalone_alpha1_decouple_sop_1013`. The paired PD report is
`products/pilotdeck-staffdeck-sop/ACCEPTANCE-FEATURES-1014.zh-CN.md`. Verified remote
SHAs are saved after push in private `features-1014/version-pair.json`.
