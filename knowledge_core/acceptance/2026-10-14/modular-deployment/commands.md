# Modular Deployment Acceptance Commands

Run StaffDeck commands in `/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck`
and PilotDeck commands in `/Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck`.
Both branches are `standalone_alpha1_decouple_km_1013`; changes are uncommitted.
Evidence paths below are relative to StaffDeck. Fresh-directory checks require a new
output directory when repeated; the gate must then refer to that new evidence.
Node PATH includes `/Users/a1/.nvm/versions/node/v22.23.1/bin`; NODE_OPTIONS is empty.

## Source, storage, regression and contracts

```sh
backend/.venv/bin/python knowledge_core/scripts/regression_acceptance.py --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/regression-final
PYTHONPATH=knowledge_core/src backend/.venv/bin/python knowledge_core/scripts/module_compositions.py --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/composition-closeout
PYTHONPATH=backend:backend/src:knowledge_core/src backend/.venv/bin/python knowledge_core/scripts/recursive_matrix.py --baseline /Users/a1/.codex/worktrees/km-compatibility-baseline-77c21973/StaffDeck --current . --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/compatibility-final
PYTHONPATH=knowledge_core/src backend/.venv/bin/python knowledge_core/scripts/downgrade_acceptance.py --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/downgrade-closeout
PYTHONPATH=knowledge_core/src backend/.venv/bin/python knowledge_core/scripts/publication_acceptance.py knowledge_core/acceptance/2026-10-14/modular-deployment/publication.json
backend/.venv/bin/python knowledge_core/scripts/cleanup_acceptance.py --pilotdeck-root /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck --evidence-dir knowledge_core/acceptance/2026-10-14/modular-deployment
```

Latest results: 114 core, 68 compatibility tests; 189 recursive cases,
843 KM HTTP calls, no differences; 9 startup/storage/import combinations.
Historical fixture and implementation remain at the fixed baseline.

## Self-contained build, installed processes and Docker

```sh
# Inside a copy of knowledge_core alone, without either host source:
node build.mjs
# StaffDeck:
backend/.venv/bin/python knowledge_core/scripts/isolated_minimal.py --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/isolated-final --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --uv /Users/a1/.local/bin/uv --playwright /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
backend/.venv/bin/python knowledge_core/scripts/isolated_minimal.py --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/isolated-model-final --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --uv /Users/a1/.local/bin/uv --playwright /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' --model-config /private/tmp/km-modular-final-model.json
backend/.venv/bin/python knowledge_core/scripts/combination_browser.py --installed-python knowledge_core/acceptance/2026-10-14/modular-deployment/isolated-final/venv/bin/python --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/combination-browser --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --playwright /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
knowledge_core/acceptance/2026-10-14/modular-deployment/isolated-final/venv/bin/python knowledge_core/scripts/task_acceptance.py --directory knowledge_core/acceptance/2026-10-14/modular-deployment/tasks-closeout
backend/.venv/bin/python knowledge_core/scripts/docker_acceptance.py --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/docker --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --playwright /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
backend/.venv/bin/python knowledge_core/scripts/artifact_acceptance.py --wheel knowledge_core/dist/knowledge_core-0.1.0-py3-none-any.whl --output-dir knowledge_core/acceptance/2026-10-14/modular-deployment/full-artifact --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --browser-script knowledge_core/ui/browser-acceptance.mjs --playwright-module /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

Eight installed-wheel browser combinations passed. Actual cancellation and hard
restart passed. Minimal tables, process imports, full artifact workflow, image
build and normal shutdown passed. Direct model evidence uses valid results and
call IDs without fallback. Temporary model credentials were resolved by PD's
existing parser, stored only in a private temporary config and removed after use;
repeating the model command requires generating a new private config.

## PilotDeck runtime, real model and host browsers

```sh
# PilotDeck:
node --test --test-reporter=junit --test-reporter-destination=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/modular-deployment/pd-runtime.xml dist/tests/composition/knowledge-core.spec.js dist/tests/composition/knowledge-core-operations.spec.js dist/tests/composition/public-host-km-principal.spec.js dist/tests/gateway/gateway-server-binding.spec.js
env NODE_OPTIONS= PD_MODEL_ID=provider1/deepseek-v4-flash-0731 STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck KM_PLUGIN_EVIDENCE=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/modular-deployment/pd-model PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' /Users/a1/.nvm/versions/node/v22.23.1/bin/node scripts/km-plugin-acceptance.mjs
env NODE_OPTIONS= STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck G4_KNOWLEDGE_ARTIFACT_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/modular-deployment/full-browser-current PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' /Users/a1/.nvm/versions/node/v22.23.1/bin/node scripts/g4-knowledge-cancel-discovery-browser.mjs
env NODE_OPTIONS= KM_UPGRADE_ACCEPTANCE=1 STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck G4_KNOWLEDGE_ARTIFACT_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/modular-deployment/iframe-upgrade PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' /Users/a1/.nvm/versions/node/v22.23.1/bin/node scripts/g4-knowledge-cancel-discovery-browser.mjs
```

66 PD tests passed. Real PD model: provider1/deepseek-v4-flash-0731,
14 correlated calls; browser, ingestion, retrieval and actual Agent tool passed.
Run the two host-browser commands sequentially because they share ports.
Both full lifespan and fixed-host UI upgrade passed. The discovery fixture is
deterministic test data and is not counted as real-model evidence.

## Final Gate

```sh
backend/.venv/bin/python knowledge_core/scripts/modular_gate.py --evidence-dir knowledge_core/acceptance/2026-10-14/modular-deployment
git diff --check
```

17 gates passed. Both worktrees pass diff checks. No commits, pushes, production
database conversion or production model changes were performed.
