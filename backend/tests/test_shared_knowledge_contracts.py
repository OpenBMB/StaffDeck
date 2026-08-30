from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.knowledge import schema as knowledge_schema
from app.teams import schema as team_schema


def _schema(module, name: str):
    """返回待验证的契约类型；缺失时用清晰断言记录 TDD 的红灯。"""
    assert hasattr(module, name), f"missing schema: {module.__name__}.{name}"
    return getattr(module, name)


def test_knowledge_creation_contract_distinguishes_shared_from_dedicated() -> None:
    """创建契约只接受两种模式，并禁止把员工所有者挂到共享知识库。"""
    shared = knowledge_schema.KnowledgeBaseCreateRequest(
        tenant_id="tenant_demo",
        name="品牌内容库",
        mode="shared",
    )
    legacy_dedicated = knowledge_schema.KnowledgeBaseCreateRequest(
        tenant_id="tenant_demo",
        name="员工资料库",
    )

    assert shared.mode == "shared"
    assert shared.agent_id is None
    assert legacy_dedicated.mode == "dedicated"

    with pytest.raises(ValidationError):
        knowledge_schema.KnowledgeBaseCreateRequest(
            tenant_id="tenant_demo",
            name="错误共享库",
            mode="shared",
            agent_id="agent_writer",
        )
    with pytest.raises(ValidationError):
        knowledge_schema.KnowledgeBaseCreateRequest(
            tenant_id="tenant_demo",
            name="未知类型",
            mode="team",
        )


def test_team_creation_accepts_exactly_one_binding_source_and_one_default() -> None:
    """团队创建可选择现有共享库或新建共享库，且整个团队最多一个默认写入目标。"""
    selection_model = _schema(team_schema, "TeamKnowledgeSelection")
    create_model = _schema(team_schema, "TeamSharedKnowledgeCreate")

    existing = selection_model(
        existing_knowledge_base_id="kb_existing",
        is_default=True,
    )
    created = selection_model(
        create_shared=create_model(name="项目资料库"),
    )
    request = team_schema.TeamCreateRequest(
        tenant_id="tenant_demo",
        name="项目 A",
        knowledge_bases=[existing, created],
    )

    assert request.knowledge_bases[0].existing_knowledge_base_id == "kb_existing"
    assert request.knowledge_bases[1].create_shared.name == "项目资料库"

    with pytest.raises(ValidationError):
        selection_model()
    with pytest.raises(ValidationError):
        selection_model(
            existing_knowledge_base_id="kb_existing",
            create_shared=create_model(name="重复来源"),
        )
    with pytest.raises(ValidationError):
        team_schema.TeamCreateRequest(
            tenant_id="tenant_demo",
            name="项目 B",
            knowledge_bases=[
                selection_model(
                    existing_knowledge_base_id="kb_one",
                    is_default=True,
                ),
                selection_model(
                    existing_knowledge_base_id="kb_two",
                    is_default=True,
                ),
            ],
        )


def test_team_grant_contract_supports_no_access_and_three_permission_levels() -> None:
    """授权矩阵支持撤权以及 reader、editor、publisher 三级有效权限。"""
    grant_model = _schema(team_schema, "TeamKnowledgeGrantInput")
    request_model = _schema(team_schema, "TeamKnowledgeGrantsUpdateRequest")

    request = request_model(
        tenant_id="tenant_demo",
        expected_revision=3,
        grants=[
            grant_model(agent_id="agent_none", permission=None),
            grant_model(agent_id="agent_reader", permission="reader"),
            grant_model(agent_id="agent_editor", permission="editor"),
            grant_model(agent_id="agent_publisher", permission="publisher"),
        ],
    )

    assert [grant.permission for grant in request.grants] == [
        None,
        "reader",
        "editor",
        "publisher",
    ]
    with pytest.raises(ValidationError):
        grant_model(agent_id="agent_invalid", permission="owner")
    with pytest.raises(ValidationError):
        request_model(
            tenant_id="tenant_demo",
            expected_revision=3,
            grants=[
                grant_model(agent_id="agent_reader", permission="reader"),
                grant_model(agent_id="agent_reader", permission="publisher"),
            ],
        )


def test_shared_version_mutation_contracts_require_explicit_context_and_reason() -> None:
    """草稿、发布和回滚请求都携带团队上下文、并发基线与非空变更原因。"""
    draft_model = _schema(knowledge_schema, "SharedKnowledgeDraftCreateRequest")
    publish_model = _schema(knowledge_schema, "SharedKnowledgePublishRequest")
    rollback_model = _schema(knowledge_schema, "SharedKnowledgeRollbackRequest")

    draft = draft_model(
        tenant_id="tenant_demo",
        team_id="team_a",
        change_reason="补充本周复盘",
        expected_published_version_id="kbver_1",
    )
    publish = publish_model(
        tenant_id="tenant_demo",
        team_id="team_a",
        expected_published_version_id="kbver_1",
        change_reason="复盘已审核",
        idempotency_key="publish-turn-1",  # gitleaks:allow - deterministic test fixture
    )
    rollback = rollback_model(
        tenant_id="tenant_demo",
        team_id="team_a",
        target_version_id="kbver_1",
        expected_published_version_id="kbver_2",
        change_reason="恢复稳定版本",
        idempotency_key="rollback-turn-2",
    )

    assert draft.expected_published_version_id == "kbver_1"
    assert publish.idempotency_key == "publish-turn-1"  # gitleaks:allow - test fixture
    assert rollback.target_version_id == "kbver_1"

    with pytest.raises(ValidationError):
        draft_model(
            tenant_id="tenant_demo",
            team_id="team_a",
            change_reason="   ",
        )
    with pytest.raises(ValidationError):
        publish_model(
            tenant_id="tenant_demo",
            team_id="team_a",
            expected_published_version_id="kbver_1",
            change_reason="",
        )


def test_conversion_contract_keeps_default_team_inside_initial_bindings() -> None:
    """专用转共享只能从指定员工分支转换，默认团队必须包含在初始绑定中。"""
    request_model = _schema(knowledge_schema, "KnowledgeBaseConvertToSharedRequest")

    request = request_model(
        tenant_id="tenant_demo",
        agent_id="agent_writer",
        source_version_id="kbver_private",
        name="团队内容库",
        change_reason="批准团队协作",
        team_bindings=["team_a", "team_b"],
        default_for_team_id="team_a",
    )

    assert request.team_bindings == ["team_a", "team_b"]
    assert request.default_for_team_id == "team_a"

    with pytest.raises(ValidationError):
        request_model(
            tenant_id="tenant_demo",
            agent_id="agent_writer",
            source_version_id="kbver_private",
            name="团队内容库",
            change_reason="批准团队协作",
            team_bindings=["team_a"],
            default_for_team_id="team_b",
        )


def test_shared_management_response_exposes_head_binding_and_grant_context() -> None:
    """管理响应同时暴露全局正式版本、团队默认状态、修订号和员工授权。"""
    version_model = _schema(knowledge_schema, "KnowledgeBaseVersionRead")
    grant_read_model = _schema(team_schema, "TeamKnowledgeGrantRead")
    binding_read_model = _schema(team_schema, "TeamKnowledgeBindingRead")
    now = datetime.now(UTC)

    version = version_model(
        id="kbver_2",
        tenant_id="tenant_demo",
        knowledge_base_id="kb_shared",
        version="2.0.0",
        name="品牌内容库",
        status="active",
        publication_state="released",
        parent_version_id="kbver_1",
        source_team_id="team_a",
        change_reason="更新选题规则",
        published_at=now,
        is_published_head=True,
        created_at=now,
        updated_at=now,
    )
    binding = binding_read_model(
        id="teamkb_1",
        team_id="team_a",
        knowledge_base_id="kb_shared",
        knowledge_base_name="品牌内容库",
        status="active",
        revision=4,
        is_default=True,
        published_version_id="kbver_2",
        published_version="2.0.0",
        grants=[
            grant_read_model(
                agent_id="agent_writer",
                permission="publisher",
                status="active",
            )
        ],
        created_at=now,
        updated_at=now,
    )

    assert version.is_published_head is True
    assert binding.grants[0].permission == "publisher"
    assert binding.revision == 4
