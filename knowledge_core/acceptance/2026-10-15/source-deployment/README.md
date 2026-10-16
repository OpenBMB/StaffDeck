# KM Source Deployment Acceptance, 2026-10-15

This directory contains fresh development evidence for the source deployment and Agent management stage. No production database was migrated, and no commit or push was performed.

## Deployment

- URL: http://127.0.0.1:18890/
- Source: `/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core`
- Persistent directory: `/Users/a1/.local/share/knowledge-core/km-1013/`
- SQLite: `knowledge.sqlite`; configuration: `configuration.json`; log: `service.log`; PID: `service.pid`.
- Independent editable virtual environment: `venv/`. No StaffDeck/PilotDeck Python path or running service is required.
- Full management modules are enabled. Authentication and external publication are disabled. The listener is loopback only. No login agent or other autostart was installed.
- Private `model.json` uses the existing credential resolver and is mode 0600. Credentials are excluded from these reports. Selected direct model: `provider1/deepseek-v4-flash-0731`.

Run from any directory:

```sh
/Users/a1/.local/share/knowledge-core/km-1013/venv/bin/python /Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/scripts/source_service.py status
```

Replace `status` with `start`, `stop`, or `restart`. Duplicate startup and occupied ports fail without stopping the process owning that port. Readiness is `/health/ready`; liveness is `/health/live`.

## Function Matrix

| Function | Result | Fresh evidence |
| --- | --- | --- |
| File parsing, ingestion, Bucket, Chunk, layered index | Passed | `source-browser/`, `deployment/report.json` |
| Search, evidence and source chapter readback | Passed | `source-browser/browser-model.json`, `deletion.json` |
| Document/Bucket/Chunk editing and persistent readback | Passed | Standalone, both host browser reports, both Agent reports |
| Delete with revision; exclude from new search; retain old citation | Passed | `source-browser/deletion.json`, core contract tests |
| Generated Wiki, Wiki edit, links/citation graph, node selection | Passed | `source-browser/minimal-desktop.png`, browser steps |
| Version copy/publish/rollback/sync/hide/restore | Passed | `full-browser/report.json`, `iframe-upgrade/report.json` |
| Discovery review/reject/export; SD Skill and Tool publication | Passed | `iframe-upgrade/report.json`, persisted host resource IDs and receipts |
| SD real Agent create/poll/query/resolve/edit/Wiki/delete | Passed | `sd-agent-management/report.json`: 7 real turns, 20 completed capability events and HTTP readbacks |
| PD real Agent including Bucket/Chunk and Wiki management | Passed | `pd-agent-management/report.json`: real gateway events correlated with completed tools and HTTP readbacks |
| Default read-only tools, explicit allowlist, scoped identity, permission and revision denial | Passed | `authorization/report.json`, `host-regression/`, core identity tests |
| In-flight cancellation and restart interruption | Passed | `task-lifecycle/report.json` |
| Source restart persistence, duplicate/occupied startup protection | Passed | `deployment/report.json`, `service.log` |
| Direct real model without fallback | Passed | `source-browser/browser-model.json`: ingestion and both routing call IDs |
| PD capability model with no SD source or service | Passed | `pd-plugin/report.json`: provider/model/call IDs and successful real query tool |
| No-model lexical installation | Passed | `latest-artifact/`: copy-only build, wheel installation, browser, import audit |
| Full SD lifespan, both host browsers and iframe upgrade without host rebuild | Passed | `full-browser/`, `iframe-upgrade/` |
| Four legacy entry points recursively compared to 77c21973 | Passed | `recursive-matrix/`: 189 cases, 843 HTTP calls, no differences |

The index is the existing layered lexical/model routing index. The graph represents Wiki concept links and citations. Neither vector embeddings nor general entity/relation extraction was introduced.

## Gap Repairs

The initial baseline is preserved in `baseline.json`: Agent management, conditional deletion, fixed source deployment and the modular graph were missing; the PD browser stopped before Agent validation. The active goal was created after that baseline.

- SD/PD now register only explicitly configured Agent methods also supported by KM. Defaults remain the existing query tools. No write tools are implicitly added.
- Create accepts title and Markdown and returns an ingestion job. Real Agents polled the job to success. Edits require the revision read by the Agent; delete performs an atomic revision condition. Legacy DELETE without a revision remains compatible.
- Wiki create refuses an existing page; edit refuses missing or stale revisions. Insert and update checks are atomic. Archived documents no longer contribute Wiki pages to new retrieval; old snapshots remain readable.
- The modular standalone UI has the shared graph and delays scoped optional panels until a space is available.
- Browser fixtures now enter the iframe before operating the shared UI, declare the operations required by plugin initialization, and wait for completed mutations.

## Agent Configuration

PD `modules.knowledge.agentMethods` must be a subset of binding `methods`. Default is `[query]`; `[]` disables Agent tools. Write operations also require a writable binding and KM permission. Example explicit management declaration:

```yaml
agentMethods: [query, list_documents, get_document, import_document, update_document, delete_document, list_document_buckets, list_document_chunks, get_bucket, get_chunk, update_bucket, update_chunk, get_job, cancel_job, list_concepts, get_concept, upsert_concept, resolve_citation]
```

SD uses comma-separated `KNOWLEDGE_AGENT_METHODS` with the same method names; default is `query`. Existing current-user, employee binding and resource policy checks still apply. No administrator privilege is added. The acceptance instance explicitly enabled writes for temporary data only.

Both hosts preserve their original query tool name. Write tools are marked non-read-only and serialized. Version publication, rollback and external resource publication remain management operations, not Agent tools.

## Verification Commands

Run in the StaffDeck worktree:

```sh
backend/.venv/bin/python knowledge_core/scripts/regression_acceptance.py --output-dir knowledge_core/acceptance/2026-10-15/source-deployment/regression
backend/.venv/bin/python knowledge_core/scripts/host_regression_acceptance.py --pilot-root /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --output-dir knowledge_core/acceptance/2026-10-15/source-deployment/host-regression
backend/.venv/bin/python knowledge_core/scripts/source_acceptance_gate.py --evidence-dir knowledge_core/acceptance/2026-10-15/source-deployment
```

Final focused counts: 116 core, 73 SD compatibility, 64 PD runtime, and 13 focused SD Agent/composition tests. Commands and collected test identities are saved in regression reports/logs. Required gates reject absent, failed or skipped evidence, missing task termination, absent model correlation or absent successful tool execution.

## Failed Attempts And Limits

- Available model probes included failures and a Moonshot RPM limit. Those failures were not counted as model acceptance. Successful final source evidence uses DeepSeek with no fallback.
- Earlier source browser attempts exceeded short model timeouts. Final task/response waits allow the configured model latency and still require successful terminal tasks and actual response content.
- The SD Agent script initially used the ingestion status `succeeded` when examining a capability ledger whose terminal success is `completed`. Saved raw events were revalidated with the actual ledger contract; `verification_repair` records this. The model was not rerun or substituted to fabricate events.
- An additional old Harness hardening fixture suite had seven failures: six detached-result fixtures lack the current result policy contract, and one imports the removed host KnowledgeBase ORM. They are not part of the passed counts. The focused composition/Agent tests and real full-lifespan acceptance passed. See `additional-hardening/` for baseline/current comparison.
- SD emits `ASGI callable returned without completing response` during shutdown of background streams. Both lifecycle shutdowns complete; logs are preserved. No forced host termination was used.
- Baseline and final reports are separate. Historical acceptance does not replace this run. Production migration, production model switching, Docker deployment, login autostart, commit and push were not executed.

Build environments and copied source directories under `latest-artifact/` are disposable test workspaces, not persistent instance data. Raw requests, logs and screenshots remain in this stable evidence directory.
