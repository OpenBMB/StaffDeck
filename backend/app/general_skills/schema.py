from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.capability_scope import CapabilityScope


class GeneralSkillFile(BaseModel):
    path: str
    content: str
    size: Optional[int] = None
    mime_type: Optional[str] = None


class GeneralSkillImportRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    #: 标签分类（办公类 / 财务类 / 人力资源类 / 编程类）；空串表示清空。
    category: Optional[str] = None
    homepage: Optional[str] = None
    markdown: Optional[str] = None
    files: list[GeneralSkillFile] = Field(default_factory=list)
    directories: Optional[list[str]] = None
    status: str = "published"
    capability_scope: Optional[CapabilityScope] = None
    original_slug: Optional[str] = None
    # 用户在「缺少 SKILL.md 引用文件」的二次确认弹窗里点了「仍要导入」时为 True。
    allow_missing_references: bool = False


class GeneralSkillClawHubImportRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    source: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    status: str = "published"
    capability_scope: CapabilityScope = "general"
    allow_missing_references: bool = False


class GeneralSkillPackageUploadRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    filename: str
    content_base64: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    status: str = "published"
    capability_scope: CapabilityScope = "general"
    allow_missing_references: bool = False


class GeneralSkillPackagePreviewFile(BaseModel):
    path: str
    size: Optional[int] = None
    mime_type: Optional[str] = None
    content: str


class GeneralSkillPackagePreviewResponse(BaseModel):
    filename: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    markdown: str
    files: list[GeneralSkillPackagePreviewFile] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)


class GeneralSkillRead(BaseModel):
    id: str
    tenant_id: str
    slug: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    homepage: Optional[str] = None
    skill_markdown: str
    skill_files: list[GeneralSkillFile] = Field(default_factory=list)
    skill_directories: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    capability_scope: CapabilityScope
    #: 技能包累计下载次数（广场下载 + 免登录分享下载都 +1）。
    download_count: int = 0
    permissions: dict[str, Any] = Field(default_factory=dict)
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class GeneralSkillRunRequest(BaseModel):
    tenant_id: str
    agent_id: Optional[str] = None
    user_id: str = ""
    query: str
    operation: Literal["read", "execute"] = "execute"
    session_id: Optional[str] = None
    model_config_id: Optional[str] = None
    max_attempts: int = Field(default=10, ge=1, le=10)


class GeneralSkillRunResponse(BaseModel):
    skill_slug: str
    operation: Literal["read", "execute"] = "execute"
    execution_trace: list[dict[str, Any]] = Field(default_factory=list)
    generated_code: str = ""
    stdout: str = ""
    stderr: str = ""
    structured_result: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    reply: str


class GeneralSkillSelection(BaseModel):
    use_general_skill: bool = False
    selected_slug: Optional[str] = None
    operation: Literal["read", "execute"] = "execute"
    use_knowledge: bool = False
    knowledge_query: Optional[str] = None
    confidence: float = 0.0
    reason: Optional[str] = None


class GeneralSkillExecutionPlan(BaseModel):
    code: str
    runtime: str = "python"
    rationale: Optional[str] = None
    expected_output: Optional[str] = None


class GeneralSkillExecutionReview(BaseModel):
    result_sufficient: bool = False
    needs_retry: bool = False
    terminal: bool = False
    reason: str = ""
    repair_hint: Optional[str] = None


class GeneralSkillReply(BaseModel):
    reply: str
