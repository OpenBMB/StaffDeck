# Plugin Convergence Commands

Worktrees: StaffDeck `/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck`,
PilotDeck `/Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck`.
Both use `standalone_alpha1_decouple_km_1013` with uncommitted changes retained.
Commands are run in the indicated worktree. Node PATH includes
`/Users/a1/.nvm/versions/node/v22.23.1/bin`, and `NODE_OPTIONS` is empty.

```sh
# StaffDeck
backend/.venv/bin/python knowledge_core/scripts/regression_acceptance.py --output-dir knowledge_core/acceptance/2026-10-14/plugin-convergence/regression
PYTHONPATH=backend:backend/src:knowledge_core/src backend/.venv/bin/python knowledge_core/scripts/recursive_matrix.py --baseline /Users/a1/.codex/worktrees/km-compatibility-baseline-77c21973/StaffDeck --current . --output-dir knowledge_core/acceptance/2026-10-14/plugin-convergence/compatibility
backend/.venv/bin/python knowledge_core/scripts/cleanup_acceptance.py --pilotdeck-root /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck --evidence-dir knowledge_core/acceptance/2026-10-14/plugin-convergence
backend/.venv/bin/python knowledge_core/scripts/convergence_gate.py --evidence-dir knowledge_core/acceptance/2026-10-14/plugin-convergence
# Frontend enterprise
pnpm run build
pnpm exec vitest run src/business-ui/KnowledgeFrame.test.tsx --reporter=json --outputFile=../knowledge_core/acceptance/2026-10-14/plugin-convergence/sd-bridge-tests.json
# PilotDeck
pnpm run build
node --test --test-reporter=junit --test-reporter-destination=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/plugin-convergence/pd-runtime.xml dist/tests/composition/knowledge-core.spec.js dist/tests/composition/knowledge-core-operations.spec.js dist/tests/composition/public-host-km-principal.spec.js dist/tests/gateway/gateway-server-binding.spec.js
# PilotDeck/ui
PILOTDECK_FRONTEND_PROFILE=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/plugin-convergence/frontend-profile.yaml pnpm run build
pnpm exec vitest run src/composition/consumption.test.tsx src/composition/modules/KnowledgeFrame.test.tsx server/routes/knowledge-core.test.js server/routes/knowledge-capabilities.test.js --reporter=json --outputFile=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/plugin-convergence/pd-tests.json
# PilotDeck; existing real provider credentials are resolved by its configuration loader.
PD_MODEL_ID=provider1/deepseek-v4-flash-0731 STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' node scripts/km-plugin-acceptance.mjs
STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck G4_KNOWLEDGE_ARTIFACT_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/plugin-convergence/full-browser PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' node scripts/g4-knowledge-cancel-discovery-browser.mjs
KM_UPGRADE_ACCEPTANCE=1 STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck G4_KNOWLEDGE_ARTIFACT_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/plugin-convergence/upgrade PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' node scripts/g4-knowledge-cancel-discovery-browser.mjs
```

Independent wheel/UI artifact build and browser command:

```sh
# StaffDeck/packages/knowledge-ui
pnpm run build
# StaffDeck; clear setuptools staging before building to avoid retaining old UI assets.
node -e "require('node:fs').rmSync('knowledge_core/build',{recursive:true,force:true})"
/Users/a1/.local/bin/uv build --wheel --out-dir knowledge_core/dist knowledge_core
backend/.venv/bin/python knowledge_core/scripts/artifact_acceptance.py --wheel knowledge_core/dist/knowledge_core-0.1.0-py3-none-any.whl --output-dir knowledge_core/acceptance/2026-10-14/plugin-convergence/artifact --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --browser-script packages/knowledge-ui/browser-acceptance.mjs --playwright-module /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

All acceptance data
is temporary; only reports, screenshots and logs are retained here.
