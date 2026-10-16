# SOP Lifecycle v3

Protocol version is `3.0`; contract is `sop.lifecycle/v3`.

| Value | Owner | Persisted | Host mutation |
| --- | --- | --- | --- |
| Definition identity/version/content/requirements/source/publication/digest | Publisher, then Runtime | Immutable snapshot | Publish a new version |
| Session SOP pin, active node, slots, wait, status, revision, metadata | Runtime | SQLite | Propose slot updates and transitions |
| Proposal status/reply/next node/structured result | Host | State event and request journal | Before submission |
| Receipt capability/operation/node/session/turn/status/result/evidence | Authorized host | Request journal, consumed IDs | Before submission |
| Wait ID/kind/node/requested fields/assignee reference | Runtime | Session state | None |
| Resume wait ID/slot updates/response | Authorized host | Request journal and last resume | Before submission |
| Transition state/result/wait | Runtime | Same transaction | None |

Endpoints are `POST /v1/definitions`, `GET /v1/sessions/{id}`, and
`POST /v1/sessions/{id}/{prepare,submit,resume}`. Lifecycle requests carry
`protocolVersion`, `requestId`, `operationId`, `turnId`, optional
`idempotencyKey`, and `expectedRevision`. Prepare carries `sopId` and optional
`version`; submit carries `proposal` and `receipts`; resume carries `waitId`,
`slotUpdates`, and `response`. Mutable state is never accepted from the host.

Prepare creates revision 0 and pins an expanded definition. Submit/resume
require the current revision and increment it once. Request replay precedes
revision validation and returns the original response. Reusing a request key
with another operation or payload returns `SOP_REQUEST_CONFLICT`. An explicit
idempotency key allows a different requestId, but other fields must be identical.
State, event, and replay response commit in one SQLite transaction.

Subflow nodes may carry `sub_sop_version`; StaffDeck publication fills this
from the published child. Runtime resolves that immutable child version rather
than the newest publication. The expanded graph is then pinned per session.
Human nodes cannot complete until their approval wait has been resumed. The
resume marker is consumed on advancement, including a loop to the same node.

Receipts must match session, turn, active node, and declared capability. Each
operation ID may be consumed once per session. Host authorization and identity
are outside Core. Failed notifications are host delivery failures; Runtime
state remains committed. Conditions are supplied to the host decision maker;
Core validates the chosen successor without evaluating arbitrary expressions.

Migration v2 is provided only by StaffDeck backend's `legacy_api`; the standalone
image serves v3 exclusively. Deployment publishers must use distinct versions
for edits, including employee-specific bindings.
