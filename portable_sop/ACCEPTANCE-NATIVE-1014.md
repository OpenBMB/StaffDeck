# PD native SOP acceptance — 2026-10-14

**PASS for the agreed local scope:** SOP management and execution run with
Runtime/Management, PD Gateway and PD Web only. StaffDeck backend, database,
identity and credentials are not used. Production migration, employee/gallery
imports and Feishu retesting are excluded. Real Knowledge is **not accepted**;
its replaceable binding is tested with doubles.

## Deployment and state ownership

- Independent installed Python package: `staffdeck-sop-runtime`; Python 3.12.
- Runtime/Management: `http://127.0.0.1:27491`, isolated `runtime.sqlite`.
- PD Gateway: `127.0.0.1:27493`; PD Web: `http://127.0.0.1:27495`; Node 22.23.1.
- Data/config root: `/Users/a1/.local/share/sop-native-1014`.
- LaunchAgents: `cn.openbmb.sopnative1014.{runtime,gateway,web}`.
- PD explicitly selects `hostAdapter: pilotdeck`; Knowledge module is disabled.
  Build and services use the same private `PILOTDECK_CONFIG_PATH`.
- Only the installation owner's normal PD JWT authorizes human approval.
  Runtime alone owns graph state/revision; PD owns model context and delivery.
- The previous `cn.openbmb.sop1014` four-service deployment is unloaded and
  disabled, including its StaffDeck agent. Its databases and files remain intact.
- Runtime import inspection found no `app.*` or `staffdeck_harness` modules;
  installed distributions contain Core/Runtime and their HTTP dependencies only.
- Outbound fetch, HTTP and Undici request traces contain Runtime, Gateway and
  the configured model origin; no StaffDeck origin or `/api/auth/me` request.
  This is application request tracing, not a packet capture.

## Evidence

Private raw evidence: `/Users/a1/.local/share/sop-native-1014/evidence`.
No model credentials, owner tokens or databases are committed.

| Case | Result | Evidence |
| --- | --- | --- |
| Real-model webpage generation, name edit/save, publish, version details, 1440px | PASS | `generation-three-browser.json`, `generated-three-editor-1440.png`, `version-three-1440.png` |
| Real-model rewrite and publication, 390px, no horizontal overflow | PASS | `rewrite-three-browser.json`, `management-390.png`; published 1.0.2 |
| Webpage collect → actual read_file → owner handoff → automatic continuation → complete | PASS | `execution-three-browser.json`; desktop/mobile revisions 1 → 5 → 6 → 8 |
| Runtime/Gateway/Web restart during human wait | PASS | Desktop case; revision and pinned 1.0.1 unchanged, normal owner form resumes |
| Duplicate original resume | PASS | Both widths return the same receipt/revision and do not add a continuation |
| Durable, unsubmitted resume intent after Web restart | PASS | `outbox-restart.json`; real model completes at revision 4; second restart does not resubmit accepted run |
| New publication affects new sessions and preserves old snapshots | PASS | `deployment-final.json`; new session 1.0.2, completed desktop still 1.0.1 |
| Owner JWT, changed owner, forged authority, foreign session/project, wrong wait/revision | PASS | Native authority/router/approval bridge tests |
| Draft CAS, immutable versions, rollback, archive/delete, atomic child publication | PASS | Independent management tests; child unpublished/cycle/depth >8 rejected |
| Worker cancellation, event replay, interrupted worker, one repair, draft preservation | PASS | Independent management and PD worker/router tests |
| Submit/revision/slots/receipts, wait/resume and process recovery | PASS | Independent Runtime/Core and real HTTP AgentLoop tests |
| Tool/Skill/Knowledge binding success/failure/missing binding and receipt scope | PASS with doubles | HTTP AgentLoop test; real webpage capability is read_file only |
| StaffDeck compatibility adapter | PASS | Existing approval and SOP AgentLoop focused regression |

Real three-service sessions:

- Desktop: `web:s_5b025b9f-61a1-472b-9270-1e05701a620c`,
  wait `978ba279-9d29-4f77-9266-ab30ab443ee9`, version 1.0.1, final revision 8.
- Mobile: `web:s_12cfe888-4eeb-4d21-aeab-19cc8d727080`,
  wait `02153e33-ec1c-4db8-8f19-9c096cd87ef2`, version 1.0.1, final revision 8.
- Outbox restart: `web:s_1083e9ed-d094-492d-9cb3-4a27d08f58ae`,
  wait `7283309f-8e59-4ed1-b4e6-44586fcfa901`,
  run `sop-resume:2fdd84b9-7f4b-4534-b803-f6ed41d28350`.

The restart test initially exposed `interaction_reconnect_required`: a restarted
Web had lost its server connection proof. The bridge now persists that proof,
reconnects with it before continuation, and retains stale-proof rejection. The
corrected real restart case passes. Earlier wrong-project test intents were
rejected and retained as failed; they are not counted as successful sessions.
Accepted interrupted Gateway runs are retained in the execution log and are not
silently rerun; the outbox only redelivers work Gateway has not accepted.

## Focused verification and versions

- `PYTHONPATH=portable_sop/src backend/.venv/bin/python -m pytest portable_sop/tests -q`: **32 passed**.
- PD HTTP Runtime/native and StaffDeck adapter tests: **95 passed** (`backend-final.tap`).
- PD Web worker/router/authority/outbox/binding and normal bridge regressions:
  **73 passed in 9 files** (`web-final.txt`).
- Frontend module generator: **10 passed**.
- Backend TypeScript compile, frontend typecheck, configured production Web
  build and both repositories' `git diff --check`: passed.
  These suite counts overlap; they are not summed into a unique-test claim.

Both repositories use `standalone_alpha1_decouple_sop_1013`.
The PD report records its paired StaffDeck implementation commit. Exact final
remote SHAs are also stored in the private `evidence/version-pair.json` after
push verification. This report supersedes the former four-service completion
standard in `ACCEPTANCE-1014.md`.
