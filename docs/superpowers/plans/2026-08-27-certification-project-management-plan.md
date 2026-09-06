# Certification Project Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为管理员提供认证项目网页管理闭环，使其能够创建项目、选择知识库版本、维护已有内部账号成员、分类上传和处理材料，并安全进入绑定项目的审核对话。

**Architecture:** 保留现有 `AuditCase` 数据模型和 `/api/audit-cases` 读取接口，在后端增加管理员管理契约、成员维护、管理列表、材料显式替换和单材料处理能力。前端新增独立 `/enterprise/audit-cases` 列表页和 `/enterprise/audit-cases/:caseId` 详情页，聊天页继续只承担项目选择与会话绑定。

**Tech Stack:** FastAPI, SQLModel, SQLite startup migrations, pytest, React 18, TypeScript, React Router, Vitest, Testing Library, Tailwind CSS, existing StaffDeck API client and UI components.

**Spec:** `docs/superpowers/specs/2026-08-27-certification-project-management-design.md`

## Global Constraints

- 管理员项目写权限必须由后端强制执行，前端隐藏按钮不能替代后端校验。
- 项目成员只从当前租户已有 `source=web` 内部账号中选择，不实现邀请未注册账号。
- `GET /api/audit-cases` 保持现有数组响应格式，继续供聊天页使用。
- 项目管理页面使用“认证项目”，代码和数据模型继续使用 `AuditCase`、`audit_case_id`。
- 归档项目只读，不能修改、上传、处理或创建新的项目审核对话。
- 材料去重键为“项目 ID + 材料类型 + SHA-256”，文件版本键为“项目 ID + 材料类型 + 文件名”。
- 材料支持 `.pdf`、`.docx`、`.txt`、`.md`、`.markdown`、`.html`、`.htm`；旧版 `.doc` 必须提示转换为 `.docx`。
- 单个材料文件上限为 50 MB，前后端都必须校验。
- 不引入 Redux、Zustand、React Query、Celery 或 Redis。
- 材料处理第一阶段继续复用现有同步处理接口，按单文件显示阶段和失败状态。
- 现有未跟踪的 `启动StaffDeck.bat` 不属于本功能，不得暂存、修改或删除。
- 每个生产函数先有一个会失败的测试；必须实际运行并确认失败后再写实现。
- 后端测试从 `backend` 目录运行，使用 `.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py -q`；前端测试使用 `npm run test -- src/pages/audit-cases/AuditCasesPage.test.tsx`。

## File Map

### Backend

- Modify `backend/app/db/models.py`: 为材料唯一约束增加 `material_type`。
- Modify `backend/app/db/database.py`: 增加幂等 SQLite 材料约束迁移，并接入启动迁移链。
- Create `backend/tests/test_audit_material_migration.py`: 验证旧表迁移、数据保留和重复启动安全。
- Modify `backend/app/audit_cases/schema.py`: 增加更新、成员、管理列表、选项、事件和材料操作的请求/响应模型。
- Modify `backend/app/audit_cases/service.py`: 增加管理员写权限、项目更新、成员替换、管理列表聚合、选项、事件、材料替换和单材料处理逻辑。
- Modify `backend/app/api/audit_cases.py`: 暴露管理列表、选项、更新、成员、事件、单材料处理和显式替换接口。
- Modify `backend/tests/test_audit_case_api.py`: 增加 API 权限、项目更新、成员维护、管理列表和材料管理测试。
- Create `backend/tests/test_audit_case_management.py`: 聚合管理逻辑和成员事务测试。

### Frontend

- Modify `frontend-enterprise/src/enums/routes.ts`: 增加认证项目列表路由。
- Modify `frontend-enterprise/src/App.tsx`: 注册管理员列表/详情路由和项目到聊天的绑定跳转。
- Modify `frontend-enterprise/src/components/AppSidebar.tsx`: 添加管理员专用“认证项目”入口。
- Modify `frontend-enterprise/src/types/index.ts`: 增加管理列表、事件、知识库版本和管理接口类型。
- Create `frontend-enterprise/src/pages/audit-cases/auditCaseTypes.ts`: 页面内部状态和上传任务类型。
- Create `frontend-enterprise/src/pages/audit-cases/auditCaseApi.ts`: 管理页面 API 调用和 multipart 上传。
- Create `frontend-enterprise/src/pages/audit-cases/auditCaseErrors.ts`: 错误码到中文提示的映射。
- Create `frontend-enterprise/src/pages/audit-cases/auditCasePresentation.ts`: 状态标签、材料类型和格式展示函数。
- Create `frontend-enterprise/src/pages/audit-cases/hooks/useAuditCaseList.ts`: 列表筛选、加载和分页状态。
- Create `frontend-enterprise/src/pages/audit-cases/hooks/useAuditCaseDetail.ts`: 详情聚合读取和局部刷新。
- Create `frontend-enterprise/src/pages/audit-cases/hooks/useMaterialUploads.ts`: 受限并发上传、自动处理和取消请求。
- Create `frontend-enterprise/src/pages/audit-cases/AuditCasesPage.tsx`: 项目列表页面。
- Create `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx`: 项目详情页面。
- Create `frontend-enterprise/src/pages/audit-cases/components/AuditCaseTable.tsx`: 项目表格和操作。
- Create `frontend-enterprise/src/pages/audit-cases/components/AuditCaseFilters.tsx`: 搜索、状态、体系和报告类型筛选。
- Create `frontend-enterprise/src/pages/audit-cases/components/CreateAuditCaseDialog.tsx`: 三步创建弹窗。
- Create `frontend-enterprise/src/pages/audit-cases/components/AuditCaseBasicForm.tsx`: 基本信息表单。
- Create `frontend-enterprise/src/pages/audit-cases/components/KnowledgeVersionSelector.tsx`: 知识库版本多选。
- Create `frontend-enterprise/src/pages/audit-cases/components/MemberSelector.tsx`: 内部账号多选和保存。
- Create `frontend-enterprise/src/pages/audit-cases/components/MaterialManager.tsx`: 材料分类、上传、处理和替换。
- Create `frontend-enterprise/src/pages/audit-cases/components/MaterialCategoryCard.tsx`: 单材料分类卡片。
- Create `frontend-enterprise/src/pages/audit-cases/components/MaterialStatusBadge.tsx`: 材料状态展示。
- Create `frontend-enterprise/src/pages/audit-cases/components/CoveragePanel.tsx`: 覆盖率和阻断项。
- Create `frontend-enterprise/src/pages/audit-cases/components/AuditCaseEventTimeline.tsx`: 项目事件时间线。
- Create `frontend-enterprise/src/pages/audit-cases/AuditCasesPage.test.tsx`: 列表、路由和创建页面测试。
- Create `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx`: 详情、成员和材料管理测试。
- Create `frontend-enterprise/src/pages/audit-cases/auditCaseApi.test.ts`: API 请求和错误映射测试。
- Modify `frontend-enterprise/src/pages/chat/components/AuditCasePanel.tsx`: 管理入口、归档过滤和绑定状态展示。
- Modify `frontend-enterprise/src/pages/chat/auditCaseModel.ts`: 复用共享项目类型和读取逻辑。
- Modify `frontend-enterprise/src/pages/chat/components/AuditCasePanel.test.tsx`: 新增管理员入口和归档项目行为测试。
- Modify `frontend-enterprise/src/i18n/en.json`: 增加新增页面的英文翻译键。

---

### Task 1: 建立材料分类唯一约束迁移

**Files:**
- Modify: `backend/app/db/models.py:45-55`
- Modify: `backend/app/db/database.py:80-106,2056-2075`
- Create: `backend/tests/test_audit_material_migration.py`

**Interfaces:**
- Consumes: 现有 `AuditCaseMaterial` SQLModel 定义、`_migrate_sqlite_skill_schema()` 启动迁移链、`app_data_migrations` 幂等记录表。
- Produces: 新建数据库使用包含 `material_type` 的材料唯一约束；旧数据库启动时自动完成同样约束迁移。

- [ ] **Step 1: Write the failing migration test**

在 `backend/tests/test_audit_material_migration.py` 写入以下行为测试：

```python
def test_audit_material_schema_migration_rebuilds_legacy_unique_constraints(tmp_path):
    db_path = tmp_path / "legacy-materials.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            CREATE TABLE audit_case_materials (
                id VARCHAR PRIMARY KEY,
                tenant_id VARCHAR NOT NULL,
                audit_case_id VARCHAR NOT NULL,
                attachment_id VARCHAR NOT NULL,
                material_type VARCHAR NOT NULL,
                filename VARCHAR NOT NULL,
                content_type VARCHAR NOT NULL,
                sha256 VARCHAR NOT NULL,
                size INTEGER NOT NULL,
                storage_key VARCHAR NOT NULL,
                extracted_text_storage_key VARCHAR,
                characters INTEGER NOT NULL DEFAULT 0,
                extraction_status VARCHAR NOT NULL DEFAULT 'pending',
                processing_status VARCHAR NOT NULL DEFAULT 'pending',
                version INTEGER NOT NULL DEFAULT 1,
                is_current BOOLEAN NOT NULL DEFAULT 1,
                supersedes_material_id VARCHAR,
                error_code VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_audit_case_material_sha
                    UNIQUE (tenant_id, audit_case_id, sha256),
                CONSTRAINT uq_audit_case_material_version
                    UNIQUE (tenant_id, audit_case_id, filename, version)
            )
        """)
        conn.exec_driver_sql(
            "INSERT INTO audit_case_materials "
            "(id, tenant_id, audit_case_id, attachment_id, material_type, filename, "
            "content_type, sha256, size, storage_key, created_at, updated_at) "
            "VALUES ('m1','t1','c1','a1','audit_record','same.txt','text/plain','h1',1,'k1',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )

    with engine.begin() as conn:
        database._migrate_audit_case_material_schema(
            conn, inspect(engine), {"audit_case_materials"}
        )

    with engine.connect() as conn:
        constraints = {
            item["name"]: tuple(item["column_names"])
            for item in inspect(conn).get_unique_constraints("audit_case_materials")
        }
        assert any(
            columns == ("tenant_id", "audit_case_id", "sha256", "material_type")
            for columns in constraints.values()
        )
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM audit_case_materials"
        ).scalar_one() == 1


def test_audit_material_schema_migration_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migrated-materials.db'}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        database._migrate_audit_case_material_schema(
            conn, inspect(engine), {"audit_case_materials"}
        )
        database._migrate_audit_case_material_schema(
            conn, inspect(engine), {"audit_case_materials"}
        )
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM audit_case_materials"
        ).scalar_one() == 0
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM app_data_migrations "
            "WHERE id = 'audit_case_material_unique_type_v1'"
        ).scalar_one() == 1
```

The test file must import `inspect`, `create_engine`, `SQLModel`, and `from app.db import database`. Both migration tests use real SQLite state and real SQLAlchemy assertions.

- [ ] **Step 2: Run the migration tests to verify they fail**

Run from `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_material_migration.py -q
```

Expected: FAIL because the migration function does not yet rebuild the legacy constraint and the new expected constraint is absent.

- [ ] **Step 3: Write the minimal model and migration implementation**

Change `AuditCaseMaterial.__table_args__` to use:

```python
UniqueConstraint(
    "tenant_id", "audit_case_id", "sha256", "material_type",
    name="uq_audit_case_material_sha_type",
),
UniqueConstraint(
    "tenant_id", "audit_case_id", "material_type", "filename", "version",
    name="uq_audit_case_material_version_type",
),
```

Add `_migrate_audit_case_material_schema(conn, inspector, tables)` to `backend/app/db/database.py`. It must:

1. Return when `audit_case_materials` does not exist.
2. Check `app_data_migrations` for a stable migration ID such as `audit_case_material_unique_type_v1`.
3. Rebuild only the SQLite material table when the old named constraints are present.
4. Preserve every existing column and row.
5. Recreate the model-declared indexes after renaming the rebuilt table.
6. Insert the migration marker only after the rebuild succeeds.
7. Be safe when invoked twice.

Call it from the existing SQLite startup migration sequence after `_migrate_audit_case_schema`.

- [ ] **Step 4: Run the migration tests to verify they pass**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_material_migration.py -q
```

Expected: PASS with no migration warning or duplicate-index error.

- [ ] **Step 5: Run the existing database migration tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_migration.py tests/test_database_config.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/db/models.py backend/app/db/database.py backend/tests/test_audit_material_migration.py
git commit -m "feat: migrate audit material uniqueness by type"
```

### Task 2: 增加管理契约和管理员服务方法

**Files:**
- Modify: `backend/app/audit_cases/schema.py`
- Modify: `backend/app/audit_cases/service.py`
- Create: `backend/tests/test_audit_case_management.py`

**Interfaces:**
- Consumes: `AuditCase`, `AuditCaseMaterial`, `AuditCaseEvent`, `KnowledgeBaseVersion`, `User`, `record_case_event`, `ensure_tenant_admin`。
- Produces: 可供 API 使用的 `AuditCaseUpdate`、`AuditCaseMemberUpdate`、`AuditCaseManagementRead`、`AuditCaseManagementPage`、`AuditCaseManagementOptions`、`AuditCaseEventRead`；服务方法 `update_case`、`replace_members`、`list_management_cases`、`list_management_options`、`list_events`、`replace_material`、`process_one_material`。

- [ ] **Step 1: Write the failing service tests**

在 `backend/tests/test_audit_case_management.py` 建立与现有 `test_audit_case_api.py` 相同的 SQLite fixture，并先写：

```python
def test_update_case_changes_only_editable_fields_and_records_event(api_context):
    service, db, admin, case = make_case_context(api_context)
    updated = service.update_case(
        case,
        admin,
        AuditCaseUpdate(
            organization_name="更新后的企业",
            report_type="监督",
            management_systems=["能源管理体系"],
            knowledge_base_version_ids=[],
        ),
    )
    assert updated.organization_name == "更新后的企业"
    assert updated.owner_user_id == case.owner_user_id
    events = list_events(db, case.id)
    assert events[-1].event_type == "audit_case.updated"


def test_replace_members_rejects_foreign_or_channel_users_as_one_transaction(api_context):
    service, db, admin, case = make_case_context(api_context)
    with pytest.raises(AuditCaseAccessDenied, match="internal"):
        service.replace_members(case, admin, ["user-other", "channel-user"])
    db.refresh(case)
    assert case.member_user_ids_json == []


def test_management_list_aggregates_current_material_counts(api_context):
    service, _db, admin, case = make_case_context(api_context)
    page = service.list_management_cases(admin, tenant_id="tenant_demo", limit=20, offset=0)
    item = next(row for row in page.items if row.id == case.id)
    assert item.material_total == 0
    assert item.material_ready == 0
    assert item.material_failed == 0
```

- [ ] **Step 2: Run the service tests to verify they fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_management.py -q
```

Expected: FAIL because the new schema classes and service methods do not exist.

- [ ] **Step 3: Add the request and response models**

Add Pydantic models with these exact fields:

```python
class AuditCaseUpdate(BaseModel):
    organization_name: str | None = Field(default=None, min_length=1, max_length=200)
    report_type: str | None = Field(default=None, min_length=1, max_length=100)
    management_systems: list[str] | None = None
    knowledge_base_version_ids: list[str] | None = None

class AuditCaseMemberUpdate(BaseModel):
    member_user_ids: list[str] = Field(default_factory=list)

class AuditCaseManagementRead(AuditCaseRead):
    material_total: int = 0
    material_ready: int = 0
    material_failed: int = 0
    file_coverage: float = 0.0
    chunk_coverage: float = 0.0

class AuditCaseManagementPage(BaseModel):
    items: list[AuditCaseManagementRead]
    total: int

class AuditCaseEventRead(BaseModel):
    id: str
    audit_case_id: str
    actor_user_id: str
    event_type: str
    resource_type: str
    resource_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
```

Add options models for knowledge version ID, knowledge base name, version, status, supported formats, and maximum bytes.

- [ ] **Step 4: Implement minimal service behavior**

Implement administrator checks through `ensure_tenant_admin` at the API boundary and a second `actor.role == "admin"` assertion in service methods. `update_case` must:

- reload the case by tenant and ID;
- reject archived status with `AuditCaseReadOnly`;
- strip and validate changed strings;
- validate every selected knowledge version by tenant and `status == "active"`;
- leave owner and member JSON unchanged;
- call `record_case_event` with only allowed metadata keys;
- commit and refresh.

Implement `replace_members` as one transaction. Query all requested IDs with `tenant_id == case.tenant_id`, `source == "web"`, and compare the result count to the deduplicated input count. On mismatch raise `AuditCaseAccessDenied("internal project member required")` before changing the case. Store sorted IDs, record `audit_case.members_replaced` with `count`, commit, and refresh.

Implement `list_management_cases` with one project query and batch material aggregation for current materials. Do not call `list_current_materials` once per case. Use a count query grouped by `audit_case_id` and current material rows for ready/failed counts; return `total` before offset/limit.

Implement `list_management_options` using tenant-scoped active `KnowledgeBaseVersion` rows and constants for the seven supported extensions and 50 MB limit.

Implement `list_events` in descending creation order and return sanitized event metadata.

- [ ] **Step 5: Run the service tests to verify they pass**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_management.py -q
```

Expected: PASS.

- [ ] **Step 6: Run the existing audit service tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py tests/test_audit_case_api.py -q
```

Expected: PASS except for tests intentionally updated in Task 3 to reflect administrator-only project writes.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/audit_cases/schema.py backend/app/audit_cases/service.py backend/tests/test_audit_case_management.py
git commit -m "feat: add audit case management service contract"
```

### Task 3: 暴露管理 API 和收紧写权限

**Files:**
- Modify: `backend/app/api/audit_cases.py`
- Modify: `backend/tests/test_audit_case_api.py`

**Interfaces:**
- Consumes: Task 2 schema and service methods, existing auth dependencies, multipart upload handling.
- Produces: `GET /management`, `GET /management-options`, `PATCH /{case_id}`, `PUT /{case_id}/members`, `GET /{case_id}/events`, `POST /{case_id}/materials/{material_id}/process`, and `POST /{case_id}/materials/{material_id}/replace`。

- [ ] **Step 1: Write failing API tests**

Add tests with existing `api_context` and `_headers` helpers:

```python
def test_only_admin_can_create_update_members_and_archive_audit_case(api_context):
    client, _engine, users, _tmp = api_context
    member = _headers(users["member"])
    response = client.post(
        "/api/audit-cases",
        headers=member,
        json={"tenant_id": "tenant_demo", "organization_name": "禁止创建", "report_type": "监督"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "AUDIT_CASE_ADMIN_REQUIRED"


def test_management_api_returns_paginated_counts_and_updates_members(api_context):
    client, _engine, users, _tmp = api_context
    admin = _headers(users["admin"])
    created = create_case_via_api(client, admin, "网页管理企业")
    case_id = created["id"]
    page = client.get(
        "/api/audit-cases/management?tenant_id=tenant_demo&limit=20&offset=0",
        headers=admin,
    )
    assert page.status_code == 200
    assert page.json()["total"] == 1
    updated = client.patch(
        f"/api/audit-cases/{case_id}?tenant_id=tenant_demo",
        headers=admin,
        json={"report_type": "监督"},
    )
    assert updated.status_code == 200
    members = client.put(
        f"/api/audit-cases/{case_id}/members?tenant_id=tenant_demo",
        headers=admin,
        json={"member_user_ids": ["user-member"]},
    )
    assert members.status_code == 200
    assert members.json()["member_user_ids"] == ["user-member"]
```

Add tests that a member receives 403 for upload, process, archive, and all new management mutations; add tests that a cross-tenant administrator receives 404/403 without data leakage; add tests for options and events.

- [ ] **Step 2: Run the API tests to verify they fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py -q
```

Expected: FAIL because the new routes do not exist and existing member write behavior is not yet restricted.

- [ ] **Step 3: Add management routes before the dynamic case route**

Register static routes `/management` and `/management-options` before `/{case_id}`. Require `require_tenant_admin` for them and for `POST /`, `PATCH /{case_id}`, `PUT /{case_id}/members`, archive, upload, process, and replacement. Keep read endpoints using `_authorized_case`.

Map service exceptions to exact error codes:

```python
AuditCaseAccessDenied -> 403 "AUDIT_CASE_ADMIN_REQUIRED" for management writes
AuditCaseReadOnly -> 409 "AUDIT_CASE_READ_ONLY"
AuditCaseNotFound -> 404 "AUDIT_CASE_NOT_FOUND"
```

The existing report-generation read/use permission path must remain compatible with assigned members.

- [ ] **Step 4: Implement the route handlers**

Each handler must:

- require `tenant_id`;
- use the existing `get_current_user` or `require_tenant_admin` dependency;
- pass the path `case_id` to the service;
- return the declared response model;
- never use organization name as lookup key;
- preserve the current `GET /api/audit-cases` response shape.

- [ ] **Step 5: Run the API tests to verify they pass**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py tests/test_audit_case_management.py -q
```

Expected: PASS.

- [ ] **Step 6: Run the complete backend audit suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_*.py -q
```

Expected: PASS, including the existing long-text and report pipeline tests.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/api/audit_cases.py backend/tests/test_audit_case_api.py
git commit -m "feat: expose admin audit case management API"
```

### Task 4: 实现材料分类去重、显式替换和单文件处理

**Files:**
- Modify: `backend/app/audit_cases/service.py`
- Modify: `backend/app/api/audit_cases.py`
- Modify: `backend/app/audit_cases/schema.py`
- Modify: `backend/tests/test_audit_case_api.py`
- Modify: `backend/tests/test_audit_cases.py`

**Interfaces:**
- Consumes: Task 1 constraints, existing `add_material`, `process_material`, storage helpers and parser.
- Produces: `replace_material(case, actor, material, filename, content_type, data)` and `process_one_material(case, actor, material_id)` with `MATERIAL_ALREADY_EXISTS`, `MATERIAL_CATEGORY_CONFLICT`, `UNSUPPORTED_DOCUMENT_FORMAT`, and `AUDIT_MATERIAL_TOO_LARGE` behavior.

- [ ] **Step 1: Write failing material behavior tests**

Add tests:

```python
def upload_material(client, headers, case_id, material_type, filename, data):
    return client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo"
        f"&material_type={material_type}",
        headers=headers,
        files={"files": (filename, data, "text/plain")},
    )


def test_same_hash_in_other_material_type_returns_category_conflict(api_context):
    client, _engine, users, _tmp = api_context
    admin = _headers(users["admin"])
    created = client.post(
        "/api/audit-cases",
        headers=admin,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "分类去重企业",
            "report_type": "监督",
        },
    )
    case_id = created.json()["id"]
    first = upload_material(client, admin, case_id, "audit_record", "same.txt", b"same")
    assert first.status_code == 200
    conflict = upload_material(client, admin, case_id, "audit_plan", "plan.txt", b"same")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "MATERIAL_CATEGORY_CONFLICT"


def test_explicit_replace_preserves_old_version_and_processes_only_new_material(api_context):
    client, _engine, users, _tmp = api_context
    admin = _headers(users["admin"])
    created = client.post(
        "/api/audit-cases",
        headers=admin,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "版本企业",
            "report_type": "再认证",
        },
    )
    case_id = created.json()["id"]
    first = upload_material(
        client, admin, case_id, "audit_record", "记录.txt", b"第一版记录"
    )
    material_id = first.json()[0]["id"]
    replaced = client.post(
        f"/api/audit-cases/{case_id}/materials/{material_id}/replace"
        "?tenant_id=tenant_demo",
        headers=admin,
        files={"file": ("记录-v2.txt", b"第二版记录", "text/plain")},
    )
    assert replaced.status_code == 200
    replacement = replaced.json()
    assert replacement["version"] == 2
    assert replacement["supersedes_material_id"] == material_id
    history = client.get(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&include_history=true",
        headers=admin,
    )
    assert history.status_code == 200
    assert {item["version"] for item in history.json()} == {1, 2}
    assert [item["id"] for item in history.json() if item["is_current"]] == [replacement["id"]]
    processed = client.post(
        f"/api/audit-cases/{case_id}/materials/{replacement['id']}/process"
        "?tenant_id=tenant_demo",
        headers=admin,
    )
    assert processed.status_code == 200
    assert processed.json()["id"] == replacement["id"]
```

The helper and tests must be written exactly with concrete fixture setup and assertions; no placeholder test body is allowed.

- [ ] **Step 2: Run the tests to verify they fail**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py -k "category_conflict or explicit_replace" -q
```

Expected: FAIL because current duplicate and same-name logic ignores material type and no explicit replacement route exists.

- [ ] **Step 3: Implement material-type-aware behavior**

Update `add_material` queries to include `material_type`. If the same SHA exists in the same case and type, return the existing material idempotently. If the same SHA exists in another type, raise a typed conflict that the API maps to `MATERIAL_CATEGORY_CONFLICT`.

Compute versions by case, material type, and filename. Explicit replacement must mark only the target material as non-current, create the next version in that type/name series, preserve the `supersedes_material_id`, and write raw storage before committing the new row.

Reject unsupported extensions with the same parser-level error code before storage is written. Reject sizes greater than 50 MB before calling `extract_text`.

Implement single-material processing by reloading the target material under the current case, asserting current and writable state, and calling the existing extraction/evidence processing path exactly once.

- [ ] **Step 4: Run material tests to verify they pass**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py tests/test_audit_cases.py -k "material or upload or process" -q
```

Expected: PASS.

- [ ] **Step 5: Run long-text acceptance tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_pipeline_acceptance.py tests/test_audit_report_e2e.py -q
```

Expected: PASS with complete source text preserved through chunks and report evidence.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/audit_cases/service.py backend/app/api/audit_cases.py backend/app/audit_cases/schema.py backend/tests/test_audit_case_api.py backend/tests/test_audit_cases.py
git commit -m "feat: add typed audit material replacement"
```

### Task 5: 增加前端 API 类型和管理员列表页

**Files:**
- Modify: `frontend-enterprise/src/enums/routes.ts`
- Modify: `frontend-enterprise/src/App.tsx`
- Modify: `frontend-enterprise/src/components/AppSidebar.tsx`
- Modify: `frontend-enterprise/src/types/index.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/auditCaseTypes.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/auditCaseApi.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/auditCaseErrors.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/auditCasePresentation.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/hooks/useAuditCaseList.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/AuditCasesPage.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/AuditCaseTable.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/AuditCaseFilters.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/CreateAuditCaseDialog.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/AuditCaseBasicForm.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/KnowledgeVersionSelector.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/MemberSelector.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/AuditCasesPage.test.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/auditCaseApi.test.ts`
- Modify: `frontend-enterprise/src/i18n/en.json`

**Interfaces:**
- Consumes: Task 3 management API, existing `api` client, `EmployeeAccount` type, existing `isEnterpriseAdmin` route patterns and UI primitives.
- Produces: administrator list route and creation flow that returns a newly created `caseId` and navigates to detail in Task 6.

- [ ] **Step 1: Write failing API and page tests**

In `auditCaseApi.test.ts` assert exact requests:

```ts
it('loads a management page with encoded filters', async () => {
  await listManagedAuditCases({ query: '示例企业', status: 'active', offset: 20, limit: 20 });
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/api/audit-cases/management?tenant_id='),
    expect.objectContaining({ headers: expect.any(Object) }),
  );
});
```

In `AuditCasesPage.test.tsx` cover:

```tsx
it('shows the create action and management rows for an administrator', async () => {
  render(<AuditCasesPage />);
  expect(await screen.findByRole('button', { name: '新建认证项目' })).toBeInTheDocument();
  expect(await screen.findByText('示例企业')).toBeInTheDocument();
});
```

Add tests for empty state, filters in URL, admin-only route and three-step creation validation.

- [ ] **Step 2: Run frontend tests to verify they fail**

Run from `frontend-enterprise`:

```powershell
npm run test -- src/pages/audit-cases/auditCaseApi.test.ts src/pages/audit-cases/AuditCasesPage.test.tsx
```

Expected: FAIL because the page and API functions do not exist.

- [ ] **Step 3: Add frontend types and API functions**

Define `AuditCaseManagementRead`, `AuditCaseManagementPage`, `AuditCaseManagementOptions`, and `AuditCaseEventRead` in `types/index.ts`. Add these functions to `auditCaseApi.ts`:

```ts
export function listManagedAuditCases(params: AuditCaseListParams): Promise<AuditCaseManagementPage>;
export function loadAuditCaseManagementOptions(): Promise<AuditCaseManagementOptions>;
export function createAuditCase(request: AuditCaseCreateRequest): Promise<AuditCaseRead>;
export function updateAuditCase(caseId: string, request: AuditCaseUpdateRequest): Promise<AuditCaseRead>;
export function replaceAuditCaseMembers(caseId: string, memberUserIds: string[]): Promise<AuditCaseRead>;
```

All query parameters must use `URLSearchParams`; do not concatenate raw organization names or IDs.

- [ ] **Step 4: Implement the list page and creation dialog**

Use a local reducer in `useAuditCaseList` with URL-backed filters. Use existing `DataTable`, `Input`, `Select`, `Dialog`, `UIButton`, and `notify` components. The creation dialog must keep its draft after a failed request, submit only trimmed values, and navigate to `/enterprise/audit-cases/{created.id}` after success.

The knowledge selector consumes management options and renders name/version/status, but submits only version IDs. The member selector loads `/api/auth/users?tenant_id=tenant_demo`, displays only `source=web` accounts, and submits only user IDs.

- [ ] **Step 5: Register route and sidebar entry**

Add `EnterpriseRoute.AuditCases`, import `AuditCasesPage`, add the admin conditional route, and add the sidebar item adjacent to the other tenant management entries. Use the existing route redirect pattern for non-admin access.

- [ ] **Step 6: Run frontend tests to verify they pass**

```powershell
npm run test -- src/pages/audit-cases/auditCaseApi.test.ts src/pages/audit-cases/AuditCasesPage.test.tsx src/components/AppSidebar.test.tsx
```

Expected: PASS.

- [ ] **Step 7: Run typecheck and i18n check**

```powershell
npm run build
npm run i18n:check
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add frontend-enterprise/src/enums/routes.ts frontend-enterprise/src/App.tsx frontend-enterprise/src/components/AppSidebar.tsx frontend-enterprise/src/types/index.ts frontend-enterprise/src/pages/audit-cases frontend-enterprise/src/i18n/en.json
git commit -m "feat: add audit case management list page"
```

### Task 6: 实现项目详情、成员维护和覆盖率

**Files:**
- Modify: `frontend-enterprise/src/App.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/hooks/useAuditCaseDetail.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/CoveragePanel.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/AuditCaseEventTimeline.tsx`
- Modify: `frontend-enterprise/src/pages/audit-cases/components/MemberSelector.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx`

**Interfaces:**
- Consumes: Task 3 detail, member, coverage and events APIs; Task 5 shared API/types.
- Produces: detail route with project overview, editable basic fields, knowledge version selection, member replacement, coverage and event timeline.

- [ ] **Step 1: Write failing detail tests**

```tsx
it('loads the detail data for the route case id and saves members as a complete list', async () => {
  renderAt('/enterprise/audit-cases/case-1');
  expect(await screen.findByText('示例企业')).toBeInTheDocument();
  await user.click(screen.getByRole('tab', { name: '项目成员' }));
  await user.click(screen.getByRole('checkbox', { name: '成员账号' }));
  await user.click(screen.getByRole('button', { name: '保存成员' }));
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/api/audit-cases/case-1/members'),
    expect.objectContaining({ method: 'PUT' }),
  );
});

it('does not allow an archived project to submit edits', async () => {
  renderAt('/enterprise/audit-cases/case-archived');
  expect(await screen.findByText('已归档')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
});
```

Add a stale-response test: resolve a request for `case-1` after navigation to `case-2`; assert `case-1` data is not rendered under `case-2`.

- [ ] **Step 2: Run detail tests to verify they fail**

```powershell
npm run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx
```

Expected: FAIL because the detail route and hook do not exist.

- [ ] **Step 3: Implement detail loading with abort protection**

`useAuditCaseDetail(caseId)` must load project, current materials, coverage, options, users, and events. Use `AbortController` or a request generation token. Only apply a response when its captured `caseId` equals the active route ID.

If materials, coverage, or events fail, render a retry state only in that section. If project or member/options loading fails, do not submit an empty replacement.

- [ ] **Step 4: Implement editable overview and member tab**

The basic editor submits only editable fields. The member tab submits the full deduplicated ID list. The owner is shown separately and cannot be removed through the member selector. Saving refreshes the server state and shows the mapped error message on failure.

For archived projects, disable all mutation controls and show the read-only explanation.

- [ ] **Step 5: Implement coverage and event timeline**

Render file coverage, chunk coverage, current/ready/failed counts, element coverage, and backend blockers. Render event metadata only after passing through the error-safe presentation function; never render raw JSON blobs containing unknown fields.

- [ ] **Step 6: Register detail route and run tests**

Add `/enterprise/audit-cases/:caseId` before the wildcard route. Run:

```powershell
npm run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx src/pages/audit-cases/AuditCasesPage.test.tsx
npm run build
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add frontend-enterprise/src/App.tsx frontend-enterprise/src/pages/audit-cases
git commit -m "feat: add audit case detail and member management"
```

### Task 7: 实现材料管理 UI 和自动处理

**Files:**
- Create: `frontend-enterprise/src/pages/audit-cases/hooks/useMaterialUploads.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/components/MaterialManager.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/MaterialCategoryCard.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/MaterialStatusBadge.tsx`
- Modify: `frontend-enterprise/src/pages/audit-cases/auditCaseApi.ts`
- Modify: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx`
- Modify: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx`

**Interfaces:**
- Consumes: Task 4 upload/process/replacement APIs, Task 6 detail refresh and coverage state.
- Produces: 四类材料卡片、文件校验、并发上传、自动处理、单文件重试、显式替换和历史版本查看。

- [ ] **Step 1: Write failing material UI tests**

```tsx
it('rejects legacy doc before making an upload request', async () => {
  renderMaterialManager('case-1');
  await user.upload(screen.getByLabelText('审核记录上传'), new File(['x'], '记录.doc'));
  expect(await screen.findByText('请转换为 .docx 后上传')).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining('/materials'), expect.anything());
});

it('processes a successfully uploaded file when auto processing is enabled', async () => {
  renderMaterialManager('case-1', { autoProcess: true });
  await user.upload(screen.getByLabelText('审核记录上传'), new File(['record'], '记录.txt'));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/materials/material-1/process'),
    expect.objectContaining({ method: 'POST' }),
  ));
});
```

Add tests that one failed file does not remove a successful file, that replacement includes the original material ID, and that archived projects disable upload controls.

- [ ] **Step 2: Run material UI tests to verify they fail**

```powershell
npm run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx
```

Expected: FAIL because material manager and upload hook do not exist.

- [ ] **Step 3: Add multipart API functions**

Implement:

```ts
export function uploadAuditCaseMaterial(
  caseId: string,
  materialType: AuditMaterialType,
  file: File,
  signal?: AbortSignal,
): Promise<AuditCaseMaterialRead[]>;

export function processAuditCaseMaterial(caseId: string, materialId: string): Promise<AuditCaseMaterialRead>;

export function replaceAuditCaseMaterial(
  caseId: string,
  materialId: string,
  file: File,
): Promise<AuditCaseMaterialRead>;
```

Use `FormData`, do not set a manual multipart content type, and always URL-encode `caseId`, `materialId`, `tenant_id`, and `material_type`.

- [ ] **Step 4: Implement upload task state**

Use a `Map` keyed by local upload ID and a maximum of three active uploads. Validate extension and size before making a request. For automatic processing, invoke the single-material process endpoint only after the upload response supplies the material ID. Keep the task in `failed` state with the mapped error code when a request fails.

- [ ] **Step 5: Implement material category cards**

Render categories with exact API values from the presentation map. Show filename, version, size, character count, extraction status, processing status, and error reason. Provide buttons for “重新处理”, “替换文件”, and “查看历史版本”. After every mutation, refresh materials and coverage for the current route `caseId`.

- [ ] **Step 6: Run material tests and full frontend tests**

```powershell
npm run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx
npm test
npm run build
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add frontend-enterprise/src/pages/audit-cases
git commit -m "feat: add audit case material management"
```

### Task 8: 接入聊天绑定和最终回归

**Files:**
- Modify: `frontend-enterprise/src/pages/chat/components/AuditCasePanel.tsx`
- Modify: `frontend-enterprise/src/pages/chat/auditCaseModel.ts`
- Modify: `frontend-enterprise/src/pages/chat/useChatSession.ts`
- Modify: `frontend-enterprise/src/pages/chat/components/AuditCasePanel.test.tsx`
- Modify: `frontend-enterprise/src/App.tsx`
- Modify: `frontend-enterprise/src/i18n/en.json`

**Interfaces:**
- Consumes: Task 5 list route, Task 6 detail route, existing chat session creation and `audit_case_id` binding.
- Produces: 从管理页进入绑定审核对话、聊天页管理员管理入口、归档项目过滤和已绑定会话不可切换行为。

- [ ] **Step 1: Write failing chat integration tests**

```tsx
it('offers administrators a management link when no audit case exists', async () => {
  renderAuditCasePanel({ cases: [], isAdmin: true });
  await user.click(screen.getByRole('button', { name: '管理认证项目' }));
  expect(navigateMock).toHaveBeenCalledWith('/enterprise/audit-cases');
});

it('does not render archived cases as selectable chat projects', () => {
  renderAuditCasePanel({
    cases: [activeCase, { ...archivedCase, status: 'archived' }],
  });
  expect(screen.getByRole('button', { name: activeCase.organization_name })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: archivedCase.organization_name })).not.toBeInTheDocument();
});
```

Add a binding test asserting the chat session creation request body contains the selected `audit_case_id`, not only a URL query parameter.

- [ ] **Step 2: Run chat tests to verify they fail**

```powershell
npm run test -- src/pages/chat/components/AuditCasePanel.test.tsx
```

Expected: FAIL because the panel has no management link/filter and the management-to-chat action is not connected.

- [ ] **Step 3: Implement chat integration**

Add administrator-only “管理认证项目” and “新建认证项目” links. Filter `status === 'archived'` from selectable cases while retaining the existing read-only history behavior. Keep the current session lock.

Implement a detail-page action that creates a new chat session with `audit_case_id: caseId` and navigates to the returned `session_id`. Do not infer project from organization name or from local storage.

- [ ] **Step 4: Run targeted chat tests**

```powershell
npm run test -- src/pages/chat/components/AuditCasePanel.test.tsx src/pages/chat/useChatSession.teamScope.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Run complete verification**

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

From `frontend-enterprise`:

```powershell
npm test
npm run i18n:check
npm run build
```

From the repository root after the service is running:

```powershell
Invoke-WebRequest http://127.0.0.1:5173/api/health -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:5173/workspace/gallery -UseBasicParsing
```

Expected: backend and frontend tests pass, the build succeeds, health is HTTP 200 with `status=ok`, and gallery is HTTP 200.

- [ ] **Step 6: Perform manual acceptance**

Using an administrator account:

1. Open “认证项目” from the sidebar.
2. Create a project with one knowledge version and one existing internal member.
3. Confirm the detail route contains the returned project ID.
4. Upload one file into each of the four material categories.
5. Confirm each file has an independent processing status.
6. Replace one file and confirm the previous version remains visible in history.
7. Remove and re-add the member.
8. Enter the audit chat and confirm the created session is bound to the project.
9. Archive the project and confirm all mutation controls are disabled.
10. Log in as the member and confirm the project is usable in chat but project management controls are unavailable.
11. Confirm an unrelated project never appears in the current project’s materials or coverage.

- [ ] **Step 7: Commit final integration**

```powershell
git add frontend-enterprise/src/App.tsx frontend-enterprise/src/pages/chat frontend-enterprise/src/i18n/en.json
git commit -m "feat: connect audit project management to chat"
```

## Self-Review Checklist

- [ ] Spec section 1 background/current gaps covered by Tasks 1-3.
- [ ] Project list and creation covered by Task 5.
- [ ] Detail, member and knowledge version maintenance covered by Task 6.
- [ ] Material types, formats, size, versioning, retry and replacement covered by Tasks 1 and 4 and 7.
- [ ] Permission and tenant isolation covered by Tasks 2 and 3.
- [ ] Stale response and cross-project protection covered by Task 6 and Task 7.
- [ ] Chat binding and archived filtering covered by Task 8.
- [ ] Long-document acceptance and health/gallery verification covered by Tasks 4 and 8.
- [ ] No new placeholder remains in production or test code.
- [ ] The migration requirement is explicit; no task incorrectly assumes that changing SQLModel constraints needs no database migration.
