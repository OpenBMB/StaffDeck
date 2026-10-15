# 最终复验命令

SD：`/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck`。
PD：`/Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck`。
两侧分支 `standalone_alpha1_decouple_km_1013`。以下命令均实际执行，临时数据库和运行目录隔离；证据保存在本目录。

## SD 回归与矩阵

```sh
backend/.venv/bin/python knowledge_core/scripts/regression_acceptance.py --output-dir knowledge_core/acceptance/2026-10-14/regression
PYTHONPATH=backend:knowledge_core/src backend/.venv/bin/python knowledge_core/scripts/recursive_matrix.py --baseline /Users/a1/.codex/worktrees/km-compatibility-baseline-77c21973/StaffDeck --current . --output-dir knowledge_core/acceptance/2026-10-14/compatibility
```

32 core、107 知识兼容测试通过；189 递归案例通过，853 KM HTTP 调用，任务终态符合要求。JUnit、实际测试清单在 `regression/`，逐案例请求、响应和差异在 `compatibility/matrix.json`。

## 构建与独立安装

SD `packages/knowledge-ui`：

```sh
PATH=/Users/a1/.nvm/versions/node/v22.23.1/bin:$PATH NODE_OPTIONS='' npm run build
```

SD 根目录：

```sh
pip3 wheel --no-deps ./knowledge_core -w knowledge_core/acceptance/2026-10-14/artifacts
backend/.venv/bin/python knowledge_core/scripts/artifact_acceptance.py --wheel knowledge_core/acceptance/2026-10-14/artifacts/knowledge_core-0.1.0-py3-none-any.whl --output-dir knowledge_core/acceptance/2026-10-14/independent-final --node /Users/a1/.nvm/versions/node/v22.23.1/bin/node --browser-script packages/knowledge-ui/browser-acceptance.mjs --playwright-module /Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck/node_modules/playwright/index.mjs --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

TypeScript/Vite 和 wheel 构建通过；隔离安装完成浏览器 20 项流程、实际宿主导入为空、仅 14 张 KM 表。见 `independent-final/`。

## PD 联合浏览器与真实模型

```sh
PATH=/Users/a1/.nvm/versions/node/v22.23.1/bin:$PATH NODE_OPTIONS='' STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' G4_KNOWLEDGE_ARTIFACT_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/full-browser node scripts/g4-knowledge-cancel-discovery-browser.mjs
PATH=/Users/a1/.nvm/versions/node/v22.23.1/bin:$PATH NODE_OPTIONS='' STAFFDECK_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' G4_KNOWLEDGE_ARTIFACT_ROOT=/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck/knowledge_core/acceptance/2026-10-14/hosted-model-final G4_REQUIRE_MODEL_SELECTOR=1 G4_SD_ONLY=1 node scripts/g4-knowledge-browser.mjs
```

完整 SD lifespan、SD/PD 主流程和发现/取消通过；实际模型四次有效 JSON 调用、跨服务关联及无词法回退检索通过。报告、请求、截图和退出日志在 `full-browser/`、`hosted-model-final/`。

## 统一门槛

```sh
backend/.venv/bin/python knowledge_core/scripts/acceptance_gate.py --evidence-dir knowledge_core/acceptance/2026-10-14
git diff --check
```

四门槛通过、退出 0；两侧空白检查通过。不提交、不推送、不切换生产。
