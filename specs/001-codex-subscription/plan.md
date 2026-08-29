# Implementation Plan: Codex Subscription Models

**Branch**: `001-codex-subscription` | **Date**: 2026-08-28 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-codex-subscription/spec.md`

## Summary

在不改变现有 API Key 模型运行链路的前提下，新增一个由本机 Codex app-server 管理的 ChatGPT/Codex 订阅运行通道。模型配置显式保存认证方式；订阅模型不保存密钥，使用 Codex 持有并自动刷新的登录状态。模型页提供一次浏览器授权、状态、取消、重新授权与退出入口，并让 API Key 与订阅模型共同参与现有的验证、启用和默认模型机制。

## Technical Context

**Language/Version**: Python 3.11+；TypeScript（严格模式）

**Primary Dependencies**: FastAPI、SQLModel/SQLAlchemy、Pydantic、React、Vite；本机 `codex app-server` JSON-RPC 运行时

**Storage**: SQLite（现有模型配置表增加认证方式字段）；订阅 OAuth 凭据由 Codex 本机安全存储管理，不进入 StaffDeck 数据库

**Testing**: pytest、Vitest、Ruff、TypeScript/Vite build；用可控 app-server 替身测试 JSON-RPC 映射，不执行真实浏览器登录或真实订阅调用

**Target Platform**: StaffDeck 本机单端口桌面/单用户部署（macOS、Windows、Linux）

**Project Type**: FastAPI Web 服务 + React 管理控制台

**Performance Goals**: 管理员在一次页面交互中可发起授权；状态轮询不阻塞其他模型操作；流式文本以已有消费者可消费的增量形式转发

**Constraints**: 只能使用 Codex 受管 `chatgpt` 登录；禁止访问、传入、持久化或回显原始 OAuth 令牌、授权码、Cookie 与回调地址；订阅运行时用临时会话、只读沙箱和拒绝审批策略执行

**Scale/Scope**: 首期一台机器一个 Codex 登录状态，支持每个租户多个 API Key 或订阅模型；不做订阅账号多选、远程共享凭据、集群协调或用量计费

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` 仍是初始化模板，未定义项目专属 MUST 原则。适用仓库门禁：不破坏 API Key 回归、所有秘密不入库不回显、SQLite 迁移可重复、管理操作保留租户管理员校验、先红后绿的 TDD。设计满足这些门禁；无复杂度例外。

## Project Structure

### Documentation (this feature)

```text
specs/001-codex-subscription/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)
```text
backend/
├── app/
│   ├── api/model_configs.py
│   ├── codex_subscription/app_server.py
│   ├── config.py
│   ├── db/{database.py,models.py}
│   └── llm/{client.py,model_config_resolver.py,model_protocols.py,protocol_drivers.py,schemas.py}
└── tests/
    ├── test_codex_subscription.py
    ├── test_database_config.py
    ├── test_model_configs_api.py
    └── test_model_protocols.py

frontend-enterprise/
└── src/
    ├── pages/{ModelsPage.tsx,ModelsPage.test.ts}
    └── types/index.ts
```

**Structure Decision**: 在现有模型配置 API 和 LLM 驱动抽象中增加订阅分支；新增的 app-server 适配器只负责本机 JSON-RPC、账户状态与临时回合。前端继续使用模型页，依据 `auth_mode` 条件展示不同字段和授权控制。

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| None | N/A | N/A |
