# 审核证据 Phase 1 Harness 可靠性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复长 `read_file` 结果裁断分页信息的问题，并让同一 TaskFrame 在后续轮次继续使用已经物化的附件和结构化材料清单。

**Architecture:** 保持文件工具的服务器端分页协议不变，只把 Harness 面向模型的结果裁剪改为字段感知；分页元数据始终完整保留，正文单独裁剪。TaskFrame 首轮把附件描述符写入持久 requirement，后续轮次从同一记录恢复描述符，不扩展到跨会话材料复用。

**Tech Stack:** Python 3.11、Pydantic v2、Harness v2、SQLModel、pytest

**Spec:** `docs/superpowers/specs/2026-08-26-audit-material-evidence-pipeline-design.md`

## Global Constraints

- 本阶段只承诺同一 TaskFrame 复用，不宣称支持新对话复用。
- `read_file` 返回的 `path`、`sha256`、`size`、`offset`、`next_offset`、`continuation_token`、`eof` 必须完整且不可从正文预览中解析。
- 普通工具结果继续受 `12_000` 字符模型投影上限约束。
- 不降低 `INVALID_CONTINUATION` 和 `STALE_CONTINUATION` 的文件一致性校验。
- 不把宿主机绝对路径写入模型可见描述符。
- 普通聊天和每个 TaskFrame 两次成功知识检索预算保持不变。

---

## File Structure

- `backend/app/core/harness_agent.py`: 按工具类型生成有界模型结果，保留分页控制字段。
- `backend/app/core/harness_v2_engine.py`: 选择当前上传描述符或同 TaskFrame 已保存描述符。
- `backend/app/core/task_request_compiler.py`: 在现有 `TaskRequirement` 定义旁声明 `MaterialManifestItem`，并把材料清单投影为结构化字段。
- `backend/tests/test_harness_v2.py`: 覆盖裁剪、恢复和不重复追问行为。
- `backend/tests/test_harness_filesystem.py`: 验证 40,000 字 UTF-8 文本分页连续性。

### Task 1: 字段感知的 `read_file` 结果裁剪

**Files:**
- Modify: `backend/app/core/harness_agent.py:845`
- Test: `backend/tests/test_harness_v2.py`

**Interfaces:**
- Consumes: `_bounded_capability_result(tool_name: str, result: dict[str, Any], *, max_chars: int = 12_000)`。
- Produces: `_bounded_read_file_data(data: dict[str, Any], *, char_budget: int) -> dict[str, Any]`；返回值保留控制字段，并增加 `content_truncated_for_model: bool` 和 `content_total_chars: int`。

- [ ] **Step 1: 编写长正文控制字段保留测试**

```python
def test_bounded_read_file_result_preserves_continuation_metadata() -> None:
    token = "continuation-token-that-must-remain-complete"
    result = _bounded_capability_result(
        "read_file",
        {
            "success": True,
            "data": {
                "path": "attachments/audit.txt",
                "content": "能" * 20_000,
                "offset": 0,
                "next_offset": 25_600,
                "continuation_token": token,
                "eof": False,
                "size": 120_000,
                "sha256": "a" * 64,
            },
        },
    )
    assert result["data"]["continuation_token"] == token
    assert result["data"]["next_offset"] == 25_600
    assert result["data"]["content_truncated_for_model"] is True
    assert result["data"]["content_total_chars"] == 20_000
    assert len(json.dumps(result, ensure_ascii=False)) <= 12_000
```

- [ ] **Step 2: 运行测试并确认旧实现失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_v2.py::test_bounded_read_file_result_preserves_continuation_metadata -v`

Expected: FAIL，旧返回只有整体 `preview`，没有完整 `data.continuation_token`。

- [ ] **Step 3: 实现字段感知裁剪**

```python
_READ_FILE_CONTROL_FIELDS = (
    "path",
    "requested_offset",
    "offset",
    "next_offset",
    "continuation_token",
    "truncated",
    "eof",
    "size",
    "sha256",
)


def _bounded_read_file_data(
    data: dict[str, Any], *, char_budget: int
) -> dict[str, Any]:
    content = str(data.get("content") or "")
    bounded = {
        key: data.get(key)
        for key in _READ_FILE_CONTROL_FIELDS
        if key in data
    }
    bounded["content_total_chars"] = len(content)
    fixed_chars = len(json.dumps(bounded, ensure_ascii=False, sort_keys=True, default=str))
    preview_budget = max(0, char_budget - fixed_chars - 160)
    bounded["content"] = content[:preview_budget]
    bounded["content_truncated_for_model"] = len(bounded["content"]) < len(content)
    return bounded
```

在 `_bounded_capability_result` 序列化前加入：

```python
data = result.get("data")
if tool_name == "read_file" and isinstance(data, dict):
    payload["data"] = _bounded_read_file_data(data, char_budget=max_chars - 256)
```

- [ ] **Step 4: 运行裁剪测试和 Harness agent 测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_v2.py -k "bounded or continuation or transcript" -v`

Expected: PASS。

- [ ] **Step 5: 提交裁剪修复**

```powershell
git add backend/app/core/harness_agent.py backend/tests/test_harness_v2.py
git commit -m "fix: preserve read continuation metadata"
```

### Task 2: 验证长 UTF-8 文本逐页无空洞

**Files:**
- Modify: `backend/tests/test_harness_filesystem.py`
- Verify: `backend/app/harness/filesystem.py:394`

**Interfaces:**
- Consumes: `read_file` 的 `continuation_token`。
- Produces: 回归测试证明每页 `offset` 等于上一页 `next_offset`，最终拼接文本与原文完全一致。

- [ ] **Step 1: 编写 40,000 字分页回归测试**

```python
def test_read_file_continuation_covers_long_utf8_text_without_gaps(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    original = "审核证据第00001段。\n" * 4_000
    context.workspace_root.mkdir(parents=True)
    (context.workspace_root / "audit.txt").write_text(original, encoding="utf-8")
    arguments: dict[str, object] = {"path": "audit.txt", "max_bytes": 4096}
    pages: list[str] = []
    expected_offset = 0
    while True:
        page = _execute(executor, context, "read_file", arguments)
        assert page["offset"] == expected_offset
        pages.append(page["content"])
        if page["eof"]:
            break
        expected_offset = page["next_offset"]
        arguments = {
            "path": "audit.txt",
            "continuation_token": page["continuation_token"],
            "max_bytes": 4096,
        }
    assert "".join(pages) == original
```

- [ ] **Step 2: 运行测试确认当前文件工具协议通过**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_filesystem.py::test_read_file_continuation_covers_long_utf8_text_without_gaps -v`

Expected: PASS；如果失败，停止并先修复 `_align_utf8_offset` 或 `_decode_utf8_chunk`，不得用放宽断言规避。

- [ ] **Step 3: 提交分页回归测试**

```powershell
git add backend/tests/test_harness_filesystem.py
git commit -m "test: cover long utf8 file pagination"
```

### Task 3: 同一 TaskFrame 恢复附件描述符

**Files:**
- Modify: `backend/app/core/harness_v2_engine.py:760`
- Test: `backend/tests/test_harness_v2.py`

**Interfaces:**
- Consumes: `HarnessTaskFrameRecord.task_requirement_json["attachments"]`。
- Produces: `_task_attachment_descriptors(row: HarnessTaskFrameRecord) -> list[dict[str, Any]]` 和 `_resolve_task_attachment_descriptors(...) -> list[dict[str, Any]]`。

- [ ] **Step 1: 编写无新附件时恢复旧描述符的测试**

```python
def test_same_task_frame_reuses_persisted_attachment_descriptors() -> None:
    row = HarnessTaskFrameRecord(
        tenant_id="tenant_demo",
        session_id="session-1",
        task_id="task-1",
        kind="sop",
        user_intent="生成审核报告",
        task_requirement_json={
            "attachments": [
                {
                    "attachment_id": "file-1",
                    "filename": "审核记录.pdf",
                    "workspace_path": "/workspace/attachments/file-1.pdf",
                    "sha256": "b" * 64,
                    "materialized": True,
                }
            ]
        },
    )
    assert _resolve_task_attachment_descriptors(row, []) == row.task_requirement_json["attachments"]
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_v2.py::test_same_task_frame_reuses_persisted_attachment_descriptors -v`

Expected: FAIL，恢复函数尚不存在。

- [ ] **Step 3: 实现受控恢复函数**

```python
def _task_attachment_descriptors(
    row: HarnessTaskFrameRecord,
) -> list[dict[str, Any]]:
    raw = (row.task_requirement_json or {}).get("attachments")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("workspace_path") or "")
        if path.startswith("/workspace/attachments/") and item.get("materialized") is True:
            result.append(dict(item))
    return result


def _resolve_task_attachment_descriptors(
    row: HarnessTaskFrameRecord,
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return current if current else _task_attachment_descriptors(row)
```

在 `_run_frame` 中先物化当前附件，再调用恢复函数：

```python
current_descriptors = materialize_task_attachments(
    request.attachments,
    tenant_id=request.tenant_id,
    session_id=session.id,
    task_frame_id=row.task_id,
    user_id=request.user_id or "",
    db=self.db,
)
attachment_descriptors = _resolve_task_attachment_descriptors(row, current_descriptors)
```

- [ ] **Step 4: 运行 TaskFrame 恢复测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_v2.py -k "attachment and task_frame" -v`

Expected: PASS；新上传描述符优先于旧描述符，空上传恢复旧描述符。

- [ ] **Step 5: 提交 TaskFrame 复用**

```powershell
git add backend/app/core/harness_v2_engine.py backend/tests/test_harness_v2.py
git commit -m "fix: reuse task frame attachments across turns"
```

### Task 4: 结构化材料清单进入 TaskRequirement

**Files:**
- Modify: `backend/app/core/task_request_compiler.py`
- Create: `backend/tests/test_task_request_compiler.py`
- Test: `backend/tests/test_harness_v2.py`

**Interfaces:**
- Consumes: `attachments: list[dict[str, Any]]`。
- Produces: `MaterialManifestItem` 和 `TaskRequirement.material_manifest: list[MaterialManifestItem]`。

- [ ] **Step 1: 编写清单投影测试**

```python
def test_compiler_projects_materialized_attachments_into_manifest() -> None:
    requirement = compiler.compile(
        frame,
        session,
        active_skill,
        capability_manifest,
        [],
        [],
        [
            {
                "attachment_id": "file-1",
                "filename": "审核记录.pdf",
                "sha256": "c" * 64,
                "materialized": True,
                "workspace_path": "/workspace/attachments/file-1.pdf",
            }
        ],
        source_user_message="继续生成",
    )
    assert requirement.material_manifest[0].attachment_id == "file-1"
    assert requirement.material_manifest[0].status == "available"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_task_request_compiler.py::test_compiler_projects_materialized_attachments_into_manifest -v`

Expected: FAIL，`material_manifest` 尚不存在。

- [ ] **Step 3: 增加清单模型和投影**

```python
class MaterialManifestItem(BaseModel):
    attachment_id: str
    filename: str
    sha256: str | None = None
    workspace_path: str | None = None
    status: Literal["available", "failed"]


class TaskRequirement(BaseModel):
    material_manifest: list[MaterialManifestItem] = Field(default_factory=list)
```

编译器使用：

```python
material_manifest = [
    MaterialManifestItem(
        attachment_id=str(item.get("attachment_id") or ""),
        filename=str(item.get("filename") or ""),
        sha256=str(item.get("sha256") or "") or None,
        workspace_path=str(item.get("workspace_path") or "") or None,
        status="available" if item.get("materialized") else "failed",
    )
    for item in attachments
    if item.get("attachment_id") and item.get("filename")
]
```

- [ ] **Step 4: 验证模型在清单已满足时不再询问上传**

在 `backend/tests/test_harness_v2.py` 增加 FakeLLMClient 场景，第二轮 `request.attachments=[]`，断言传给模型的 `task_requirement.material_manifest[0].status == "available"`，最终回复不包含“请上传”。

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_task_request_compiler.py tests/test_harness_v2.py -k "material_manifest or reuse" -v`

Expected: PASS。

- [ ] **Step 5: 提交结构化清单**

```powershell
git add backend/app/core/task_request_compiler.py backend/tests/test_task_request_compiler.py backend/tests/test_harness_v2.py
git commit -m "feat: expose task material manifest"
```

### Task 5: Phase 1 完整验证

**Files:**
- Verify: `backend/app/core/harness_agent.py`
- Verify: `backend/app/core/harness_v2_engine.py`
- Verify: `backend/app/core/task_request_compiler.py`

**Interfaces:**
- Consumes: Tasks 1-4 的接口。
- Produces: 可独立发布的 Harness 可靠性修复。

- [ ] **Step 1: 运行定向测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_filesystem.py tests/test_task_request_compiler.py tests/test_harness_v2.py -q`

Expected: PASS。

- [ ] **Step 2: 运行后端完整测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS。

- [ ] **Step 3: 运行 Ruff**

Run: `cd backend; .\.venv\Scripts\python.exe -m ruff check app tests`

Expected: PASS。

- [ ] **Step 4: 记录阶段边界**

在发布说明中写明：“当前仅支持同一 TaskFrame 继续使用已有附件；新对话复用将在 Phase 2 提供。”

- [ ] **Step 5: 提交阶段说明**

```powershell
git add README.zh.md
git commit -m "docs: describe harness attachment reuse boundary"
```
