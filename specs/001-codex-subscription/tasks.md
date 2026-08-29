---

description: "Task list for Codex subscription model implementation"
---

# Tasks: Codex Subscription Models

**Input**: Design documents from `/specs/001-codex-subscription/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/model-subscription-api.md`, `quickstart.md`

**Tests**: Required. Every behavior task follows red-green-refactor: write the focused test, run it to observe the intended failure, then add the smallest production change and re-run it.

## Phase 1: Specification and Environment

**Purpose**: Create the source-of-truth feature artifacts and establish the local test commands.

- [X] T001 Create the feature specification and quality checklist in `specs/001-codex-subscription/spec.md` and `specs/001-codex-subscription/checklists/requirements.md`
- [X] T002 Record protocol, security, API-contract and validation decisions in `specs/001-codex-subscription/{research.md,data-model.md,contracts/model-subscription-api.md,quickstart.md,plan.md}`
- [X] T003 Verify the backend test runtime and frontend test commands from `specs/001-codex-subscription/quickstart.md`

---

## Phase 2: Foundational Model-Configuration Boundary

**Purpose**: Add the explicit authentication boundary without changing existing API Key behavior. This blocks subscription configuration, runtime routing and UI work.

- [X] T004 Write failing auth-mode and migration regression tests in `backend/tests/test_model_protocols.py` and `backend/tests/test_database_config.py`
- [X] T005 Implement `auth_mode`, `codex_app_server` protocol, fingerprinting and backward-compatible snapshot behavior in `backend/app/llm/model_protocols.py`, `backend/app/llm/model_config_resolver.py`, `backend/app/db/models.py`, and `backend/app/db/database.py`
- [X] T006 Run the focused tests in `backend/tests/test_model_protocols.py` and `backend/tests/test_database_config.py` to prove the green baseline
- [X] T007 Write failing API Key and subscription creation/update isolation tests in `backend/tests/test_model_configs_api.py`
- [X] T008 Implement request/read schemas and conditional model configuration validation in `backend/app/llm/schemas.py` and `backend/app/api/model_configs.py`
- [X] T009 Run `backend/tests/test_model_configs_api.py` to prove API Key behavior remains compatible and subscription payloads never require a key

**Checkpoint**: Models have an explicit authentication mode, existing rows migrate to API Key, and no subscription secrets can enter the configuration table.

---

## Phase 3: User Story 1 - Add a Subscription Model (Priority: P1) 🎯 MVP

**Goal**: An administrator can start managed browser authorization, view safe status, and save a subscription model without API Key fields.

**Independent Test**: A fake local app-server reports login state; account endpoints return only sanitized fields and an authenticated subscription configuration can be saved/verified.

- [X] T010 [P] [US1] Write failing app-server lifecycle and sanitized-account tests in `backend/tests/test_codex_subscription.py`
- [X] T011 [P] [US1] Extend API contract assertions for subscription account routes in `backend/tests/test_model_configs_api.py`
- [X] T012 [US1] Implement the process-local JSON-RPC client, managed browser login, account state and safe errors in `backend/app/codex_subscription/app_server.py`
- [X] T013 [US1] Add local Codex command/timeout configuration and stop the app-server process on shutdown in `backend/app/config.py` and `backend/app/main.py`
- [X] T014 [US1] Add administrator-only subscription account routes and account-state schema in `backend/app/api/model_configs.py` and `backend/app/llm/schemas.py`
- [X] T015 [US1] Run `backend/tests/test_codex_subscription.py backend/tests/test_model_configs_api.py` and refactor only after green

**Checkpoint**: An administrator can complete browser-managed subscription authorization without copying sensitive data, and no endpoint reveals OAuth material.

---

## Phase 4: User Story 2 - Run with a Subscription Model (Priority: P2)

**Goal**: A verified subscription model serves text, streaming text, and JSON requests through Codex app-server while preserving existing LLM callers.

**Independent Test**: A protocol fixture returns app-server message deltas; `LLMClient` produces the same text, stream and parsed JSON shapes expected by current callers.

- [X] T016 [US2] Write failing subscription protocol-driver and constrained-turn tests in `backend/tests/test_codex_subscription.py`
- [X] T017 [US2] Implement the `codex_app_server` protocol driver, ephemeral read-only turn mapping, cancellation, text collection and delta streaming in `backend/app/llm/protocol_drivers.py` and `backend/app/codex_subscription/app_server.py`
- [X] T018 [US2] Route subscription configurations through the new driver without decrypting API keys in `backend/app/llm/client.py` and `backend/app/llm/model_config_resolver.py`
- [X] T019 [US2] Verify text, stream and JSON probes use the subscription runtime in `backend/tests/test_codex_subscription.py` and `backend/tests/test_model_configs_api.py`

**Checkpoint**: A verified subscription model can become default and current LLM consumers receive compatible text, stream and structured results.

---

## Phase 5: User Story 3 - Coexistence in Model Management (Priority: P3)

**Goal**: Model management presents the authentication selector and safe subscription controls while API Key model interactions remain unchanged.

**Independent Test**: Pure page helpers and request construction distinguish both authentication modes; the model list displays one API Key model and multiple subscription models without exposing secrets.

- [X] T020 [P] [US3] Write failing authentication-mode labels and subscription error mapping tests in `frontend-enterprise/src/pages/ModelsPage.test.ts`
- [X] T021 [P] [US3] Extend model configuration types for authentication mode and safe account status in `frontend-enterprise/src/types/index.ts`
- [X] T022 [US3] Implement authentication-mode form state, subscription account controls, polling and conditional request payloads in `frontend-enterprise/src/pages/ModelsPage.tsx`
- [X] T023 [US3] Render authentication mode in desktop/mobile model lists and add explicit confirmation before device-wide Codex logout in `frontend-enterprise/src/pages/ModelsPage.tsx`
- [X] T024 [US3] Run `frontend-enterprise/src/pages/ModelsPage.test.ts` and production build from `frontend-enterprise/package.json`

**Checkpoint**: The user can select API Key or ChatGPT subscription when adding models, configure several of either type, and understand that logout affects the device-level Codex account.

---

## Phase 6: Cross-Cutting Validation and Documentation

**Purpose**: Verify security boundaries, migration idempotency and regressions across both model modes.

- [X] T025 Add explicit no-secret assertions for account responses, model reads and provider errors in `backend/tests/test_codex_subscription.py` and `backend/tests/test_model_configs_api.py`
- [X] T026 Run focused backend regression, Ruff, frontend test and frontend build commands from `specs/001-codex-subscription/quickstart.md`
- [X] T027 Update user-facing model-configuration guidance in `README.md` and `README.zh.md`
- [X] T028 Re-check the manual acceptance path documented in `specs/001-codex-subscription/quickstart.md` and record any environment-only limitation in the final delivery note

---

## Dependencies & Execution Order

- Phase 1 is complete except T003, which must finish before confidence claims about test output.
- Phase 2 blocks all user stories because it establishes persistence, validation and runtime discrimination.
- US1 depends on Phase 2; US2 depends on the app-server adapter from US1; US3 depends on the API contract from US1.
- Phase 6 depends on all implemented stories.

## Parallel Opportunities

- T010 and T011 touch different test responsibilities and may be prepared in parallel after Phase 2.
- T020 and T021 are independent frontend test/type work after the backend API contract settles.
- The active implementation follows the ordered TDD path rather than parallel edits to shared files.

## Implementation Strategy

1. Finish the backward-compatible model boundary and prove its red-green tests.
2. Deliver account authorization/status as the first user-visible slice.
3. Add the runtime driver only after its app-server protocol tests fail as intended.
4. Surface the completed API in the model page.
5. Finish with secret-safety and API Key regressions; no commit, push or deployment is included in this task.
