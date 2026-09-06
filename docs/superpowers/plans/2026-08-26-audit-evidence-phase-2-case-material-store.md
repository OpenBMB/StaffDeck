# 审核证据 Phase 2 项目材料库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立租户和审核项目隔离的材料库，使同一项目的材料可在新对话中重新装载，同时保留文件版本、哈希、提取状态和分块完整性。

**Architecture:** `AuditCase` 是复用和权限边界，原文件及提取文本保存在项目级受控目录，SQLModel 只保存受控存储键和状态。会话通过可空 `audit_case_id` 绑定项目；Harness 从服务端材料清单物化当前版本，不依赖聊天历史中的附件。

**Tech Stack:** Python 3.11、FastAPI、SQLModel/SQLite、Pydantic v2、pytest、React 18、TypeScript、Vitest

**Spec:** `docs/superpowers/specs/2026-08-26-audit-material-evidence-pipeline-design.md`

## Global Constraints

- 同一项目内相同 SHA256 幂等复用；同名不同 SHA256 创建新版本并保留旧版本。
- 不同项目即使组织名称相同也不得自动合并材料。
- 原文件和提取文本不得暴露宿主机绝对路径，只能通过受控 ID、存储键和 `/workspace` 沙箱路径访问。
- 所有查询都必须同时约束 `tenant_id` 和 `audit_case_id`。
- 默认访问者为项目所有者、`member_user_ids_json` 中的明确成员和租户管理员。
- 材料分块目标大小为 `900` 至 `1,200` 字符，字符区间必须连续覆盖完整提取文本。
- 失败、空文本和不支持格式必须保留明确状态，不能静默标记为成功。
- 历史聊天附件不自动迁移到项目材料库。
- 本阶段不创建证据结论；`processing_status=succeeded` 仅表示全文已确定性分块。

---

## File Structure

- `backend/app/audit_cases/schema.py`: 审核项目、材料、覆盖摘要的 API 契约。
- `backend/app/audit_cases/storage.py`: 项目级原文件和提取文本的路径隔离、原子写入和读取。
- `backend/app/audit_cases/chunking.py`: 产生无空洞字符区间的确定性分块。
- `backend/app/audit_cases/service.py`: 权限、幂等、版本、提取、分块和清单装载。
- `backend/app/api/audit_cases.py`: `/api/audit-cases` 路由。
- `backend/app/core/harness_audit_cases.py`: 把项目当前材料物化到 TaskFrame。
- `frontend-enterprise/src/pages/chat/auditCaseModel.ts`: 前端状态和 API 辅助函数。
- `frontend-enterprise/src/pages/chat/components/AuditCasePanel.tsx`: 项目选择器和材料状态卡。

### Task 1: 审核项目、材料和会话引用数据模型

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/database.py`
- Create: `backend/tests/test_audit_case_migration.py`
- Create: `backend/tests/test_audit_cases.py`

**Interfaces:**
- Consumes: `new_id(prefix: str) -> str`、`utc_now()`、`SQLModel.metadata.create_all`。
- Produces: `AuditCase`、`AuditCaseMaterial`、`AuditCaseMaterialChunk`、`AuditCaseEvent`；`ChatSession.audit_case_id: str | None`。

- [ ] **Step 1: 编写模型约束测试**

```python
def test_audit_case_tables_enforce_material_identity() -> None:
    with _test_session() as db:
        case = AuditCase(
            tenant_id="tenant_a",
            owner_user_id="user_a",
            organization_name="示例企业",
            report_type="再认证",
        )
        db.add(case)
        db.commit()
        material = AuditCaseMaterial(
            audit_case_id=case.id,
            tenant_id="tenant_a",
            attachment_id="attachment-1",
            material_type="audit_record",
            filename="审核记录.pdf",
            content_type="application/pdf",
            sha256="a" * 64,
            size=1024,
            storage_key="case/material/raw",
            version=1,
        )
        db.add(material)
        db.commit()
        assert material.extraction_status == "pending"
        assert material.processing_status == "pending"
```

- [ ] **Step 2: 运行模型测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py::test_audit_case_tables_enforce_material_identity -v`

Expected: FAIL，三个模型尚不存在。

- [ ] **Step 3: 增加模型**

```python
class AuditCase(SQLModel, table=True):
    __tablename__ = "audit_cases"

    id: str = Field(default_factory=lambda: new_id("auditcase"), primary_key=True)
    tenant_id: str = Field(index=True)
    owner_user_id: str = Field(index=True)
    member_user_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    organization_name: str = Field(index=True)
    report_type: str
    management_systems_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="collecting", index=True)
    knowledge_base_version_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    active_report_version_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditCaseMaterial(SQLModel, table=True):
    __tablename__ = "audit_case_materials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "audit_case_id", "sha256", name="uq_audit_case_material_sha"),
        UniqueConstraint("tenant_id", "audit_case_id", "filename", "version", name="uq_audit_case_material_version"),
    )

    id: str = Field(default_factory=lambda: new_id("auditmat"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    attachment_id: str = Field(index=True)
    material_type: str = Field(index=True)
    filename: str = Field(index=True)
    content_type: str
    sha256: str = Field(index=True)
    size: int
    storage_key: str
    extracted_text_storage_key: Optional[str] = None
    characters: int = 0
    extraction_status: str = Field(default="pending", index=True)
    processing_status: str = Field(default="pending", index=True)
    version: int = 1
    is_current: bool = Field(default=True, index=True)
    supersedes_material_id: Optional[str] = Field(default=None, index=True)
    error_code: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditCaseMaterialChunk(SQLModel, table=True):
    __tablename__ = "audit_case_material_chunks"
    __table_args__ = (
        UniqueConstraint("material_id", "chunk_index", name="uq_audit_material_chunk_index"),
    )

    id: str = Field(default_factory=lambda: new_id("auditchunk"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    material_id: str = Field(index=True)
    chunk_index: int = Field(index=True)
    start_char: int
    end_char: int
    content_sha256: str
    content: str
    page_refs_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    processing_status: str = Field(default="pending", index=True)
    extracted_facts_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditCaseEvent(SQLModel, table=True):
    __tablename__ = "audit_case_events"

    id: str = Field(default_factory=lambda: new_id("auditevent"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    actor_user_id: str = Field(index=True)
    event_type: str = Field(index=True)
    resource_type: str
    resource_id: str
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
```

`AuditCaseEvent.metadata_json` 只允许文件名、SHA256、版本、状态、错误码、知识库版本 ID、报告版本 ID 和计数，不保存正文、宿主机路径或密钥。

在 `ChatSession` 增加：

```python
audit_case_id: Optional[str] = Field(default=None, index=True)
```

- [ ] **Step 4: 增加 SQLite 兼容迁移并测试幂等**

在 `_migrate_sqlite_skill_schema()` 中调用：

```python
_migrate_audit_case_schema(conn, inspector, tables)
```

新增：

```python
def _migrate_audit_case_schema(conn, inspector, tables: set[str]) -> None:
    if "sessions" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "audit_case_id" not in columns:
        conn.execute(text("ALTER TABLE sessions ADD COLUMN audit_case_id VARCHAR"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sessions_audit_case_id ON sessions(audit_case_id)"))
```

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_case_migration.py tests/test_audit_cases.py -v`

Expected: PASS；连续执行迁移两次不报错，旧会话 `audit_case_id` 为 `NULL`。

- [ ] **Step 5: 提交数据模型**

```powershell
git add backend/app/db/models.py backend/app/db/database.py backend/tests/test_audit_case_migration.py backend/tests/test_audit_cases.py
git commit -m "feat: add audit case material models"
```

### Task 2: 受控项目存储和确定性分块

**Files:**
- Create: `backend/app/audit_cases/__init__.py`
- Create: `backend/app/audit_cases/storage.py`
- Create: `backend/app/audit_cases/chunking.py`
- Modify: `backend/tests/test_audit_cases.py`

**Interfaces:**
- Consumes: `paths.user_data_dir()`、`app.knowledge.parser.extract_text(filename, data)`。
- Produces: `write_case_blob(...) -> str`、`read_case_blob(...) -> bytes`、`chunk_text(text: str, target_chars: int = 1_000) -> list[ChunkSpan]`。

- [ ] **Step 1: 编写路径隔离和原子存储测试**

```python
def test_case_blob_path_cannot_escape_tenant_or_case(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "user_data_dir", lambda: tmp_path)
    key = write_case_blob(
        tenant_id="../../tenant",
        audit_case_id="../case",
        material_id="material",
        name="raw",
        data=b"audit evidence",
    )
    assert read_case_blob(key) == b"audit evidence"
    assert (tmp_path / key).resolve().is_relative_to((tmp_path / "audit_cases").resolve())
```

- [ ] **Step 2: 编写连续分块测试**

```python
def test_chunk_text_covers_every_character_without_overlap() -> None:
    text = ("第一段审核记录。\n" * 90) + ("超长段落" * 700)
    chunks = chunk_text(text)
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(text)
    assert all(left.end_char == right.start_char for left, right in zip(chunks, chunks[1:]))
    assert "".join(chunk.content for chunk in chunks) == text
    assert all(chunk.content_sha256 == hashlib.sha256(chunk.content.encode("utf-8")).hexdigest() for chunk in chunks)
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py -k "blob or chunk_text" -v`

Expected: FAIL，存储和分块模块尚不存在。

- [ ] **Step 4: 实现受控存储接口**

```python
def _atomic_write(target: Path, data: bytes) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_case_blob(
    *, tenant_id: str, audit_case_id: str, material_id: str, name: str, data: bytes
) -> str:
    root = (paths.user_data_dir().resolve() / "audit_cases").resolve()
    key = "/".join(
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        for value in (tenant_id, audit_case_id, material_id)
    )
    target = (root / key / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError("audit case storage path escapes root")
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, data)
    return target.relative_to(paths.user_data_dir().resolve()).as_posix()


def read_case_blob(storage_key: str) -> bytes:
    data_root = paths.user_data_dir().resolve()
    target = (data_root / storage_key).resolve()
    root = (data_root / "audit_cases").resolve()
    if not target.is_relative_to(root) or target.is_symlink():
        raise ValueError("invalid audit case storage key")
    return target.read_bytes()
```

- [ ] **Step 5: 实现确定性分块**

```python
@dataclass(frozen=True)
class ChunkSpan:
    chunk_index: int
    start_char: int
    end_char: int
    content: str
    content_sha256: str


def chunk_text(text: str, target_chars: int = 1_000) -> list[ChunkSpan]:
    spans: list[ChunkSpan] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + 1_200)
        preferred_start = min(len(text), start + 900)
        boundary = text.rfind("\n", preferred_start, hard_end)
        end = boundary + 1 if boundary >= preferred_start else hard_end
        content = text[start:end]
        spans.append(
            ChunkSpan(
                chunk_index=len(spans),
                start_char=start,
                end_char=end,
                content=content,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
        start = end
    return spans
```

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py -k "blob or chunk_text" -v`

Expected: PASS。

- [ ] **Step 6: 提交存储与分块**

```powershell
git add backend/app/audit_cases backend/tests/test_audit_cases.py
git commit -m "feat: add isolated audit material storage"
```

### Task 3: 权限、幂等和材料版本服务

**Files:**
- Create: `backend/app/audit_cases/service.py`
- Create: `backend/app/audit_cases/schema.py`
- Modify: `backend/app/audit_cases/storage.py`
- Modify: `backend/tests/test_audit_cases.py`

**Interfaces:**
- Consumes: Task 1 模型、Task 2 存储和 `chunk_text`。
- Produces: `AuditCaseService.create_case`、`add_material`、`process_material`、`list_current_materials`、`get_case_for_user`、`archive_case`、`delete_case`；`record_case_event(...)`。

- [ ] **Step 0: 定义 API schema 和领域错误**

```python
class AuditCaseCreate(BaseModel):
    tenant_id: str
    organization_name: str = Field(min_length=1, max_length=200)
    report_type: str = Field(min_length=1, max_length=100)
    management_systems: list[str] = Field(default_factory=list)
    knowledge_base_version_ids: list[str] = Field(default_factory=list)
    member_user_ids: list[str] = Field(default_factory=list)


class AuditCaseNotFound(LookupError):
    pass


class AuditMaterialProcessingError(RuntimeError):
    pass
```

`AuditCaseRead` 和 `AuditCaseMaterialRead` 只投影受控 ID、业务字段、哈希、版本、状态、错误码、字符数和时间戳；明确排除 `storage_key` 与 `extracted_text_storage_key`。

- [ ] **Step 1: 编写幂等和版本测试**

```python
def test_same_sha_is_reused_and_changed_file_creates_version() -> None:
    service, owner, case = _service_with_case()
    first = service.add_material(case, owner, "audit_record", "审核记录.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"version-one")
    duplicate = service.add_material(case, owner, "audit_record", "审核记录.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"version-one")
    second = service.add_material(case, owner, "audit_record", "审核记录.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"version-two")
    assert duplicate.id == first.id
    assert second.version == 2
    assert second.supersedes_material_id == first.id
    assert second.is_current is True
    assert first.is_current is False
```

- [ ] **Step 2: 编写跨项目和跨租户拒绝测试**

```python
def test_case_access_never_falls_back_to_organization_name() -> None:
    service, owner, first_case = _service_with_case(organization_name="同名企业")
    second_case = service.create_case(owner, AuditCaseCreate(tenant_id=owner.tenant_id, organization_name="同名企业", report_type="监督", management_systems=[]))
    material = service.add_material(first_case, owner, "audit_record", "记录.txt", "text/plain", b"secret")
    with pytest.raises(AuditCaseNotFound):
        service.get_material(second_case.id, material.id, owner)
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py -k "same_sha or access_never" -v`

Expected: FAIL，服务尚不存在。

- [ ] **Step 4: 固定服务签名和访问判定**

```python
class AuditCaseService:
    def __init__(self, db: Session):
        self.db = db

    def can_access(self, case: AuditCase, user: User) -> bool:
        return (
            case.tenant_id == user.tenant_id
            and (
                user.role == "admin"
                or case.owner_user_id == user.id
                or user.id in set(case.member_user_ids_json or [])
            )
        )

    def get_case_for_user(self, tenant_id: str, case_id: str, user: User) -> AuditCase:
        row = self.db.get(AuditCase, case_id)
        if row is None or row.tenant_id != tenant_id or not self.can_access(row, user):
            raise AuditCaseNotFound(case_id)
        return row
```

`add_material` 必须先按 `(tenant_id, audit_case_id, sha256)` 查重，再按同名当前版本计算 `version + 1`，写入原文件后才提交数据库。

每次创建项目、增加或替换材料、处理成功/失败、归档和删除都调用：

```python
def record_case_event(
    db: Session,
    *,
    case: AuditCase,
    actor_user_id: str,
    event_type: str,
    resource_type: str,
    resource_id: str,
    metadata: dict[str, Any],
) -> None:
    allowed = {
        "filename", "sha256", "version", "status", "error_code",
        "knowledge_base_version_ids", "report_version_id", "count",
    }
    if set(metadata) - allowed:
        raise ValueError("unsafe audit event metadata")
    db.add(AuditCaseEvent(
        tenant_id=case.tenant_id,
        audit_case_id=case.id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata_json=metadata,
    ))
```

- [ ] **Step 5: 实现提取和分块状态机**

```python
def process_material(self, case: AuditCase, material: AuditCaseMaterial) -> AuditCaseMaterial:
    material.processing_status = "processing"
    self.db.add(material)
    self.db.commit()
    try:
        source = read_case_blob(material.storage_key)
        text, _format = extract_text(material.filename, source)
        if not text.strip():
            raise AuditMaterialProcessingError("EMPTY_EXTRACTED_TEXT")
        material.extracted_text_storage_key = write_case_blob(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            material_id=material.id,
            name="extracted.txt",
            data=text.encode("utf-8"),
        )
        material.characters = len(text)
        material.extraction_status = "succeeded"
        for span in chunk_text(text):
            self.db.add(AuditCaseMaterialChunk(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                material_id=material.id,
                chunk_index=span.chunk_index,
                start_char=span.start_char,
                end_char=span.end_char,
                content_sha256=span.content_sha256,
                content=span.content,
            ))
        material.processing_status = "succeeded"
        material.error_code = None
    except (KnowledgeParseError, AuditMaterialProcessingError) as exc:
        material.extraction_status = "failed"
        material.processing_status = "failed"
        material.error_code = str(exc)
    material.updated_at = utc_now()
    self.db.add(material)
    self.db.commit()
    return material
```

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py -v`

Expected: PASS；失败格式保留原文件和错误码。

- [ ] **Step 6: 实现归档和租户保留策略删除**

```python
def archive_case(self, case: AuditCase, actor: User) -> AuditCase:
    self.get_case_for_user(case.tenant_id, case.id, actor)
    case.status = "archived"
    case.updated_at = utc_now()
    record_case_event(
        self.db, case=case, actor_user_id=actor.id,
        event_type="audit_case.archived", resource_type="audit_case",
        resource_id=case.id, metadata={"status": "archived"},
    )
    self.db.add(case)
    self.db.commit()
    return case
```

归档项目的 `add_material`、`process_material` 和报告写操作统一拒绝并返回 `AUDIT_CASE_READ_ONLY`。`delete_case` 仅允许租户管理员调用；它先记录不含正文的 `audit_case.deleted` 事件，再在事务中删除 Phase 2 的块、材料、会话绑定和项目行，提交后调用 `delete_case_storage(tenant_id, case.id)`。该存储函数必须重新计算并校验 `audit_cases/<tenant-hash>/<case-hash>` 的绝对路径位于受控根目录内，拒绝符号链接后才递归删除。Phase 3 创建派生表时必须把那些表加入同一删除事务。

- [ ] **Step 7: 提交服务**

```powershell
git add backend/app/audit_cases/schema.py backend/app/audit_cases/service.py backend/app/audit_cases/storage.py backend/tests/test_audit_cases.py
git commit -m "feat: manage audit material versions"
```

### Task 4: 审核项目 API

**Files:**
- Create: `backend/app/api/audit_cases.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_audit_case_api.py`

**Interfaces:**
- Consumes: `AuditCaseService` 和当前用户认证。
- Produces: 设计规格中列出的项目、材料、处理和覆盖查询端点，以及 `/archive` 和租户管理员删除端点。

- [ ] **Step 1: 编写创建、列表、上传和权限 API 测试**

```python
def test_audit_case_api_round_trip(client, auth_headers) -> None:
    created = client.post(
        "/api/audit-cases",
        headers=auth_headers,
        json={
            "tenant_id": "tenant_demo",
            "organization_name": "示例企业",
            "report_type": "再认证",
            "management_systems": ["GB/T 23331-2020"],
            "knowledge_base_version_ids": ["kbver-1"],
        },
    )
    assert created.status_code == 200
    case_id = created.json()["id"]
    uploaded = client.post(
        f"/api/audit-cases/{case_id}/materials?tenant_id=tenant_demo&material_type=audit_record",
        headers=auth_headers,
        files={"files": ("记录.txt", b"审核记录全文", "text/plain")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()[0]["sha256"]
```

同一测试文件增加：归档后上传返回 `409 AUDIT_CASE_READ_ONLY`；普通成员删除返回 `403`；租户管理员删除后数据库材料/块和项目存储目录都不存在，但 `audit_case.deleted` 事件仍保留。

- [ ] **Step 2: 运行 API 测试并确认 404**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py::test_audit_case_api_round_trip -v`

Expected: FAIL，路由尚未注册。

- [ ] **Step 3: 实现路由和错误映射**

```python
router = APIRouter(
    prefix="/api/audit-cases",
    tags=["audit-cases"],
    dependencies=[Depends(get_current_user)],
)


@router.post("", response_model=AuditCaseRead)
def create_audit_case(
    request: AuditCaseCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuditCaseRead:
    if request.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="TENANT_ACCESS_DENIED")
    return audit_case_read(AuditCaseService(db).create_case(current_user, request))
```

材料端点使用 `files: list[UploadFile] = File(...)` 和 `material_type: str = Query(...)`。`AuditCaseNotFound` 映射为 404，版本冲突和归档只读映射为 409，提取失败仍返回 200 及材料状态。新增 `POST /{case_id}/archive` 和 `DELETE /{case_id}`；删除端点必须依赖 `require_tenant_admin`。

- [ ] **Step 4: 注册路由并运行 API 测试**

在 `backend/app/main.py` 导入 `audit_cases` 并加入：

```python
app.include_router(audit_cases.router)
```

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py -v`

Expected: PASS，其他用户和其他租户得到 404 或 403，响应不含 `storage_key`。

- [ ] **Step 5: 提交 API**

```powershell
git add backend/app/api/audit_cases.py backend/app/main.py backend/tests/test_audit_case_api.py
git commit -m "feat: expose audit case material api"
```

### Task 5: 会话绑定和跨对话 TaskFrame 物化

**Files:**
- Modify: `backend/app/api/chat.py`
- Modify: `backend/app/session/session_schema.py`
- Modify: `backend/app/core/harness_v2_engine.py`
- Create: `backend/app/core/harness_audit_cases.py`
- Modify: `backend/app/core/task_request_compiler.py`
- Modify: `backend/tests/test_harness_v2.py`

**Interfaces:**
- Consumes: `ChatTurnRequest.audit_case_id`、`ChatSession.audit_case_id`、认证后的当前用户、`AuditCaseService.list_current_materials`。
- Produces: `ChatSessionRead.audit_case_id`、`materialize_audit_case_materials(...) -> list[dict[str, Any]]`；TaskRequirement 中的项目材料描述符。

- [ ] **Step 1: 编写新会话选择项目并恢复材料测试**

```python
def test_new_session_with_audit_case_materializes_existing_materials() -> None:
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        user_id="user-1",
        agent_id="agent-1",
        audit_case_id="auditcase-1",
        message="继续生成审核报告",
    )
    response = engine.run(request)
    session = db.get(ChatSession, response.session_id)
    assert session.audit_case_id == "auditcase-1"
    assert captured_requirement["audit_case_id"] == "auditcase-1"
    assert captured_requirement["material_manifest"][0]["source"] == "audit_case"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_v2.py::test_new_session_with_audit_case_materializes_existing_materials -v`

Expected: FAIL，request 和 session 尚未连通项目。

- [ ] **Step 3: 增加请求字段并固定会话绑定规则**

```python
class ChatTurnRequest(BaseModel):
    audit_case_id: Optional[str] = None


class ChatSessionRead(BaseModel):
    # 保留现有字段；新增这一项供前端锁定项目选择器。
    audit_case_id: Optional[str] = None
```

`backend/app/api/chat.py` 在调用 Harness 前使用 `get_current_user` 已认证用户做访问校验；引擎不构造或猜测 `current_user`。校验成功后才把已经授权的 `audit_case_id` 交给 Harness：

```python
if payload.audit_case_id:
    AuditCaseService(db).get_case_for_user(
        payload.tenant_id, payload.audit_case_id, current_user
    )
response = harness_engine.run(payload)
```

引擎创建或读取会话后只执行绑定一致性检查；同一会话一旦绑定，不允许切换：

```python
if request.audit_case_id:
    if session.audit_case_id and session.audit_case_id != request.audit_case_id:
        raise HarnessRequestError("SESSION_AUDIT_CASE_CONFLICT")
    session.audit_case_id = request.audit_case_id
    self.db.add(session)
    self.db.commit()
```

`session_read(...)` 必须把 `session.audit_case_id` 投影到 `ChatSessionRead`，确保页面刷新后选择器仍处于锁定状态。

- [ ] **Step 4: 实现项目材料物化**

```python
def materialize_audit_case_materials(
    *, db: Session, case: AuditCase, session_id: str, task_frame_id: str
) -> list[dict[str, Any]]:
    workspace = harness_task_workspace_path(
        tenant_id=case.tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
        db=db,
    )
    descriptors: list[dict[str, Any]] = []
    for material in AuditCaseService(db).list_current_materials(case):
        relative = f"audit-case/{material.id}/{Path(material.filename).name}"
        _write_workspace_bytes(workspace, relative, read_case_blob(material.storage_key))
        descriptors.append({
            "source": "audit_case",
            "audit_case_id": case.id,
            "material_id": material.id,
            "filename": material.filename,
            "sha256": material.sha256,
            "version": material.version,
            "status": material.processing_status,
            "workspace_path": f"/workspace/{relative}",
            "materialized": True,
        })
    return descriptors
```

优先级固定为：当前轮新附件描述符、项目当前材料描述符、同 TaskFrame 历史描述符；按 `(source, material_id 或 attachment_id, sha256)` 去重。

- [ ] **Step 5: 运行跨对话和隔离测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_v2.py -k "audit_case or material_manifest" -v`

Expected: PASS；新对话选择同一项目可见材料，选择无权项目失败，未选择项目维持旧行为。

- [ ] **Step 6: 提交 Harness 项目装载**

```powershell
git add backend/app/api/chat.py backend/app/session/session_schema.py backend/app/core/harness_v2_engine.py backend/app/core/harness_audit_cases.py backend/app/core/task_request_compiler.py backend/tests/test_harness_v2.py
git commit -m "feat: load audit case materials into task frames"
```

### Task 6: 审核项目选择器和材料状态卡

**Files:**
- Modify: `frontend-enterprise/src/types/index.ts`
- Create: `frontend-enterprise/src/pages/chat/auditCaseModel.ts`
- Create: `frontend-enterprise/src/pages/chat/auditCaseModel.test.ts`
- Create: `frontend-enterprise/src/pages/chat/components/AuditCasePanel.tsx`
- Create: `frontend-enterprise/src/pages/chat/components/AuditCasePanel.test.tsx`
- Modify: `frontend-enterprise/src/pages/chat/useChatSession.ts`
- Modify: `frontend-enterprise/src/pages/chat/ChatPage.tsx`

**Interfaces:**
- Consumes: Phase 2 API 和 `ChatTurnRequest.audit_case_id`。
- Produces: `AuditCaseRead`、`AuditCaseMaterialRead`、`AuditCaseCoverageRead` 类型；`selectedAuditCaseId` 和 `selectAuditCase(id)` hook 状态。

- [ ] **Step 1: 编写状态摘要测试**

```typescript
it('shows only unresolved material states as missing', () => {
  const summary = auditCaseSummary([
    { id: 'm1', filename: '记录.pdf', extraction_status: 'succeeded', processing_status: 'succeeded', is_current: true },
    { id: 'm2', filename: '模板.docx', extraction_status: 'failed', processing_status: 'failed', is_current: true },
  ] as AuditCaseMaterialRead[]);
  expect(summary.ready).toBe(1);
  expect(summary.failed).toEqual(['模板.docx']);
});
```

- [ ] **Step 2: 编写组件选择测试**

```tsx
it('selects an existing case without asking for files', async () => {
  const onSelect = vi.fn();
  render(<AuditCasePanel cases={[auditCase]} selectedId={null} materials={[]} onSelect={onSelect} />);
  await userEvent.click(screen.getByRole('button', { name: /示例企业/ }));
  expect(onSelect).toHaveBeenCalledWith(auditCase.id);
  expect(screen.queryByText('请重新上传')).not.toBeInTheDocument();
});
```

- [ ] **Step 3: 运行前端测试并确认失败**

Run: `cd frontend-enterprise; npm test -- auditCaseModel.test.ts AuditCasePanel.test.tsx`

Expected: FAIL，新模块尚不存在。

- [ ] **Step 4: 增加类型和 API 辅助函数**

```typescript
export type AuditCaseRead = {
  id: string;
  tenant_id: string;
  owner_user_id: string;
  organization_name: string;
  report_type: string;
  management_systems: string[];
  status: string;
  knowledge_base_version_ids: string[];
  active_report_version_id?: string;
  created_at: string;
  updated_at: string;
};

// 在现有 ChatSession 类型中新增：
audit_case_id?: string | null;

export async function listAuditCases(): Promise<AuditCaseRead[]> {
  return api.get<AuditCaseRead[]>(`/api/audit-cases?tenant_id=${encodeURIComponent(TENANT_ID)}`);
}
```

- [ ] **Step 5: 接入 `useChatSession` 和发送体**

选择项目后加载 `GET /api/audit-cases/{id}`、`/materials`、`/coverage`。发送消息时加入：

```typescript
const requestBody = {
  tenant_id: TENANT_ID,
  session_id: requestSessionId,
  agent_id: requestAgentId,
  audit_case_id: selectedAuditCaseId || undefined,
  message: text,
  attachments: outgoingAttachments,
};
```

从 `GET /api/chat/sessions/{sessionId}` 返回的 `ChatSession.audit_case_id` 恢复选择状态；会话已有该字段时锁定选择器，避免同一会话切换项目。

- [ ] **Step 6: 运行前端测试和构建**

Run: `cd frontend-enterprise; npm test -- auditCaseModel.test.ts AuditCasePanel.test.tsx useChatSession.teamScope.test.tsx`

Expected: PASS。

Run: `cd frontend-enterprise; npm run build`

Expected: PASS。

- [ ] **Step 7: 提交前端项目面板**

```powershell
git add frontend-enterprise/src/types/index.ts frontend-enterprise/src/pages/chat/auditCaseModel.ts frontend-enterprise/src/pages/chat/auditCaseModel.test.ts frontend-enterprise/src/pages/chat/components/AuditCasePanel.tsx frontend-enterprise/src/pages/chat/components/AuditCasePanel.test.tsx frontend-enterprise/src/pages/chat/useChatSession.ts frontend-enterprise/src/pages/chat/ChatPage.tsx
git commit -m "feat: add audit case selector to chat"
```

### Task 7: 发布项目驱动的审核报告 SOP v2

**Files:**
- Create: `backend/app/db/seed_fixtures/audit_report_generation_sop_v2.json`
- Modify: `backend/app/db/seed.py`
- Create: `backend/tests/test_audit_report_sop_v2.py`

**Interfaces:**
- Consumes: StaffDeck `SkillCard` 严格 schema 和项目材料 API。
- Produces: `audit_report_generation_sop` 的新版本记录；旧版本保留，已运行 TaskFrame 不切换。

- [ ] **Step 1: 编写 SOP 结构测试**

```python
def test_audit_report_sop_v2_is_project_driven() -> None:
    card = load_audit_report_sop_v2()
    assert card.required_info == [
        "audit_case_id",
        "report_type",
        "management_systems",
        "organization_name",
        "material_manifest_status",
        "evidence_coverage_status",
        "knowledge_coverage_status",
    ]
    assert [node.node_id for node in card.nodes] == [
        "select_or_create_audit_case",
        "collect_materials",
        "ingest_and_validate_materials",
        "build_material_evidence_ledger",
        "retrieve_reference_knowledge",
        "trim_template",
        "generate_report_sections",
        "consistency_and_coverage_check",
        "output_and_confirm",
        "handoff_lead_auditor",
    ]
    collect = next(node for node in card.nodes if node.node_id == "collect_materials")
    assert "只询问缺失、失败或版本冲突的材料" in collect.instruction
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_report_sop_v2.py -v`

Expected: FAIL，fixture 和加载函数尚不存在。

- [ ] **Step 3: 创建完整 SkillCard fixture**

fixture 使用版本 `2.0.0`，十个节点按规格顺序连接。`collect_materials.instruction` 必须包含：

```json
{
  "node_id": "collect_materials",
  "type": "collect_info",
  "name": "核对项目材料清单",
  "instruction": "先读取 audit_case_id 对应的结构化材料清单；只询问缺失、失败或版本冲突的材料。清单中 status=available 或 processing_status=succeeded 的当前版本不得要求用户重复上传。",
  "expected_user_info": ["material_manifest_status"],
  "allowed_actions": [],
  "knowledge_scope": {},
  "capability_refs": {
    "general_skill_ids": [],
    "tool_ids": [],
    "knowledge_base_ids": [],
    "required_general_skill_ids": [],
    "required_tool_ids": [],
    "required_knowledge_base_ids": []
  }
}
```

Phase 3 再把证据能力和知识库 ID 写入 required refs；本阶段只发布项目材料逻辑，不伪造尚未实现的能力。

- [ ] **Step 4: 实现幂等版本写入**

`seed_demo_data` 在 `tenant_demo` 功能开关启用时调用 `seed_audit_report_sop_v2(db)`。该函数只新增 `SkillVersion(version="2.0.0")`，并仅在没有运行中旧 TaskFrame 时更新主 `Skill` 的 head 内容。

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_report_sop_v2.py -v`

Expected: PASS；执行两次只存在一条 `2.0.0` 版本，旧版本仍存在。

- [ ] **Step 5: 提交 SOP v2**

```powershell
git add backend/app/db/seed_fixtures/audit_report_generation_sop_v2.json backend/app/db/seed.py backend/tests/test_audit_report_sop_v2.py
git commit -m "feat: publish project-driven audit report sop"
```

### Task 8: Phase 2 完整验证

**Files:**
- Verify: `backend/app/audit_cases`
- Verify: `backend/app/api/audit_cases.py`
- Verify: `backend/app/core/harness_audit_cases.py`
- Verify: `frontend-enterprise/src/pages/chat/components/AuditCasePanel.tsx`

**Interfaces:**
- Consumes: Tasks 1-7。
- Produces: 可独立发布的跨对话项目材料复用。

- [ ] **Step 1: 运行后端定向测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py tests/test_audit_case_api.py tests/test_audit_case_migration.py tests/test_audit_report_sop_v2.py tests/test_harness_v2.py -q`

Expected: PASS。

- [ ] **Step 2: 运行前端测试和构建**

Run: `cd frontend-enterprise; npm test -- auditCaseModel.test.ts AuditCasePanel.test.tsx`

Expected: PASS。

Run: `cd frontend-enterprise; npm run build`

Expected: PASS。

- [ ] **Step 3: 运行完整后端测试和 Ruff**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS。

Run: `cd backend; .\.venv\Scripts\python.exe -m ruff check app tests`

Expected: PASS。

- [ ] **Step 4: 手工验收跨对话复用**

在 `tenant_demo` 创建项目并上传审核记录，等待材料状态为 `succeeded`；新建对话并选择该项目，发送“继续生成审核报告”，确认上传提示次数为 0。再选择另一个项目，确认看不到前一项目材料。

- [ ] **Step 5: 提交阶段文档**

```powershell
git add README.zh.md
git commit -m "docs: explain audit case material reuse"
```
