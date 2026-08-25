from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    agent_private_metadata,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    model_for_agent,
)
from app.agents.schema import (
    AgentModelBindingInput,
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileUpdateRequest,
    AgentResourceBindingInput,
    AgentResourcesUpdateRequest,
)
from app.api.agents import (
    create_agent,
    delete_agent,
    get_agent_models,
    list_agents,
    list_chat_agents,
    unpublish_agent_from_gallery,
    update_agent,
    update_agent_models,
    update_agent_resources,
    use_chat_agent,
)
from app.api.general_skills import import_general_skill
from app.api.tools import create_tool, update_tool
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    AgentResourceBinding,
    AgentUsage,
    ChatSession,
    GeneralSkill,
    ModelConfig,
    Tenant,
    Tool,
    User,
)
from app.db.database import _seed_agent_branch_state
from app.general_skills.schema import GeneralSkillImportRequest
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_tenant_admin,
    require_agent_scope_viewer,
)
from app.tools.tool_schema import ToolCreateRequest, ToolUpdateRequest


def test_only_creator_or_admin_can_update_and_delete_agent() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        db.add(agent)
        db.commit()

        with pytest.raises(HTTPException) as update_error:
            update_agent(
                agent.id,
                AgentProfileUpdateRequest(tenant_id="tenant_demo", name="非法修改"),
                db=db,
                current_user=other,
            )
        assert update_error.value.status_code == 403

        updated = update_agent(
            agent.id,
            AgentProfileUpdateRequest(tenant_id="tenant_demo", name="Owner 修改"),
            db=db,
            current_user=owner,
        )
        assert updated.name == "Owner 修改"

        admin_updated = update_agent(
            agent.id,
            AgentProfileUpdateRequest(tenant_id="tenant_demo", name="Admin 修改"),
            db=db,
            current_user=admin,
        )
        assert admin_updated.name == "Admin 修改"

        with pytest.raises(HTTPException) as delete_error:
            delete_agent(agent.id, tenant_id="tenant_demo", db=db, current_user=other)
        assert delete_error.value.status_code == 403


def test_non_admin_cannot_manage_overall_agent() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        overall = AgentProfile(
            id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
        )
        db.add(overall)
        db.commit()

        with pytest.raises(HTTPException) as update_error:
            update_agent(
                overall.id,
                AgentProfileUpdateRequest(
                    tenant_id="tenant_demo", description="普通用户不能改整体员工"
                ),
                db=db,
                current_user=owner,
            )
        assert update_error.value.status_code == 403

        updated = update_agent(
            overall.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo", description="管理员可以维护整体员工"
            ),
            db=db,
            current_user=admin,
        )
        assert updated.description == "管理员可以维护整体员工"


def test_agents_always_inherit_tenant_default_model_and_legacy_bindings_are_removed() -> None:
    with _test_session() as db:
        owner, _other, admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_default_model",
            tenant_id="tenant_demo",
            name="默认模型员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        tenant_default = ModelConfig(
            id="model_tenant_default",
            tenant_id="tenant_demo",
            name="租户默认模型",
            api_key_encrypted="encrypted-default",
            model="default-model",
            is_default=True,
            enabled=True,
        )
        legacy_model = ModelConfig(
            id="model_legacy_binding",
            tenant_id="tenant_demo",
            name="旧员工绑定模型",
            api_key_encrypted="encrypted-legacy",
            model="legacy-model",
            enabled=True,
        )
        legacy_binding = AgentModelBinding(
            id="binding_legacy_model",
            tenant_id="tenant_demo",
            agent_id=agent.id,
            role="default",
            model_config_id=legacy_model.id,
        )
        db.add(agent)
        db.add(tenant_default)
        db.add(legacy_model)
        db.add(legacy_binding)
        db.commit()

        resolved = model_for_agent(db, "tenant_demo", agent.id)
        assert resolved is not None
        assert resolved.id == tenant_default.id

        rows = get_agent_models(
            agent.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        assert rows == [
            {
                "role": "default",
                "model_config_id": tenant_default.id,
                "effective": False,
            }
        ]

        update_agent_models(
            agent.id,
            AgentModelsUpdateRequest(
                tenant_id="tenant_demo",
                bindings=[
                    AgentModelBindingInput(
                        role="default",
                        model_config_id=legacy_model.id,
                    )
                ],
            ),
            db=db,
            current_user=admin,
        )
        assert db.exec(
            select(AgentModelBinding).where(
                AgentModelBinding.tenant_id == "tenant_demo",
                AgentModelBinding.agent_id == agent.id,
            )
        ).all() == []


def test_startup_seed_removes_legacy_agent_model_bindings() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                id="agent_startup_cleanup",
                tenant_id="tenant_demo",
                name="启动清理员工",
            )
        )
        db.add(
            ModelConfig(
                id="model_startup_default",
                tenant_id="tenant_demo",
                name="默认模型",
                api_key_encrypted="encrypted-default",
                model="default-model",
                is_default=True,
            )
        )
        db.add(
            AgentModelBinding(
                id="binding_startup_cleanup",
                tenant_id="tenant_demo",
                agent_id="agent_startup_cleanup",
                role="default",
                model_config_id="model_startup_default",
            )
        )
        db.commit()

    table_names = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        _seed_agent_branch_state(conn, inspect(engine), table_names)

    with Session(engine) as db:
        assert db.exec(select(AgentModelBinding)).all() == []


def test_resource_binding_requires_agent_manager() -> None:
    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_resource_owner",
            tenant_id="tenant_demo",
            name="资源员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气查询",
            method="POST",
            url="/weather",
        )
        db.add(agent)
        db.add(tool)
        db.commit()
        request = AgentResourcesUpdateRequest(
            tenant_id="tenant_demo",
            resources=[AgentResourceBindingInput(resource_type="tool", resource_id=tool.id)],
        )

        with pytest.raises(HTTPException) as update_error:
            update_agent_resources(agent.id, request, db=db, current_user=other)
        assert update_error.value.status_code == 403

        bindings = update_agent_resources(agent.id, request, db=db, current_user=owner)
        assert [(item.resource_type, item.resource_id) for item in bindings] == [("tool", tool.id)]


def test_list_agents_filters_to_visible_agents_for_non_admin() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        db.add(
            AgentProfile(id="agent_overall", tenant_id="tenant_demo", name="整体", is_overall=True)
        )
        db.add(
            AgentProfile(
                id="agent_owned",
                tenant_id="tenant_demo",
                name="我的员工",
                is_overall=False,
                metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
            )
        )
        db.add(
            AgentProfile(
                id="agent_gallery",
                tenant_id="tenant_demo",
                name="广场员工",
                is_overall=False,
                metadata_json={"published_to_gallery": True, "owner_username": other.username},
            )
        )
        db.add(
            AgentProfile(
                id="agent_private",
                tenant_id="tenant_demo",
                name="别人私有员工",
                is_overall=False,
                metadata_json={"owner_user_id": other.id, "owner_username": other.username},
            )
        )
        db.add(
            AgentProfile(
                id="agent_created_by_owner_only",
                tenant_id="tenant_demo",
                name="创建字段命中但非本人",
                is_overall=False,
                metadata_json={
                    "owner_user_id": other.id,
                    "owner_username": other.username,
                    "created_by_user_id": owner.id,
                    "created_by_username": owner.username,
                    "published_to_gallery": False,
                },
            )
        )
        db.commit()

        owner_rows = list_agents("tenant_demo", db=db, current_user=owner)
        admin_rows = list_agents("tenant_demo", db=db, current_user=admin)

        assert {row.id for row in owner_rows} == {"agent_overall", "agent_owned", "agent_gallery"}
        assert {row.id for row in admin_rows} == {
            "agent_overall",
            "agent_owned",
            "agent_gallery",
            "agent_private",
            "agent_created_by_owner_only",
        }


def test_gallery_agent_is_visible_but_not_manageable_by_non_owner() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        gallery_agent = AgentProfile(
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            is_overall=False,
            metadata_json={
                "published_to_gallery": True,
                "owner_user_id": other.id,
                "owner_username": other.username,
            },
        )
        db.add(gallery_agent)
        db.commit()

        owner_visible_rows = list_agents("tenant_demo", db=db, current_user=owner)
        assert {row.id for row in owner_visible_rows} == {"agent_gallery"}

        with pytest.raises(HTTPException) as manage_error:
            ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, owner)
        assert manage_error.value.status_code == 403

        assert (
            ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, other).id
            == gallery_agent.id
        )
        assert (
            ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, admin).id
            == gallery_agent.id
        )

        with pytest.raises(HTTPException) as create_error:
            create_tool(
                ToolCreateRequest(
                    tenant_id="tenant_demo",
                    name="blocked_gallery_tool",
                    display_name="不应创建",
                    url="/blocked",
                ),
                agent_id=gallery_agent.id,
                db=db,
                current_user=owner,
            )
        assert create_error.value.status_code == 403
        assert db.exec(select(Tool).where(Tool.name == "blocked_gallery_tool")).first() is None


def test_only_admin_can_unpublish_gallery_agent_without_deleting_it() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        gallery_agent = AgentProfile(
            id="agent_gallery_governance",
            tenant_id="tenant_demo",
            name="待治理广场员工",
            is_overall=False,
            metadata_json={
                "published_to_gallery": True,
                "gallery_published_at": "2026-08-08T08:00:00+00:00",
                "gallery_published_by": other.username,
                "owner_user_id": other.id,
                "owner_username": other.username,
            },
        )
        binding = AgentResourceBinding(
            id="binding_gallery_governance",
            tenant_id="tenant_demo",
            agent_id=gallery_agent.id,
            resource_type="tool",
            resource_id="tool_governance",
            status="active",
        )
        db.add(gallery_agent)
        db.add(binding)
        db.commit()

        with pytest.raises(HTTPException) as member_error:
            unpublish_agent_from_gallery(
                gallery_agent.id,
                tenant_id="tenant_demo",
                db=db,
                current_user=owner,
            )
        assert member_error.value.status_code == 403

        unpublished = unpublish_agent_from_gallery(
            gallery_agent.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        assert unpublished.status == "active"
        assert unpublished.metadata["published_to_gallery"] is False
        assert unpublished.metadata["gallery_unpublished_by"] == admin.username
        assert "gallery_published_at" not in unpublished.metadata
        assert "gallery_published_by" not in unpublished.metadata
        assert db.get(AgentProfile, gallery_agent.id) is not None
        assert db.get(AgentResourceBinding, binding.id) is not None
        assert gallery_agent.id not in {
            row.id for row in list_agents("tenant_demo", db=db, current_user=owner)
        }
        assert gallery_agent.id in {
            row.id for row in list_agents("tenant_demo", db=db, current_user=other)
        }

        repeated = unpublish_agent_from_gallery(
            gallery_agent.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        assert repeated.metadata["gallery_unpublished_at"] == unpublished.metadata[
            "gallery_unpublished_at"
        ]


def test_agent_ownership_uses_immutable_user_id_not_username_metadata() -> None:
    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            id="agent_spoofed_owner_name",
            tenant_id="tenant_demo",
            name="用户名不能授权",
            metadata_json={
                "owner_user_id": other.id,
                "owner_username": owner.username,
            },
        )
        db.add(agent)
        db.commit()

        assert list_agents("tenant_demo", db=db, current_user=owner) == []
        with pytest.raises(HTTPException) as manage_error:
            ensure_agent_scope_manager(db, "tenant_demo", agent.id, owner)
        assert manage_error.value.status_code == 403
        assert ensure_agent_scope_manager(db, "tenant_demo", agent.id, other).id == agent.id


def test_agent_scope_viewer_allows_owned_and_gallery_but_blocks_private_agents() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        private = AgentProfile(
            id="agent_private_scope",
            tenant_id="tenant_demo",
            name="私有员工",
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        gallery = AgentProfile(
            id="agent_gallery_scope",
            tenant_id="tenant_demo",
            name="广场员工",
            metadata_json={
                "owner_user_id": owner.id,
                "owner_username": owner.username,
                "published_to_gallery": True,
            },
        )
        db.add(private)
        db.add(gallery)
        db.commit()

        assert require_agent_scope_viewer("tenant_demo", private.id, owner, db) is owner
        assert require_agent_scope_viewer("tenant_demo", gallery.id, other, db) is other
        assert require_agent_scope_viewer("tenant_demo", private.id, admin, db) is admin
        with pytest.raises(HTTPException) as private_error:
            require_agent_scope_viewer("tenant_demo", private.id, other, db)
        assert private_error.value.status_code == 403
        with pytest.raises(HTTPException) as tenant_error:
            require_agent_scope_viewer("another_tenant", private.id, owner, db)
        assert tenant_error.value.status_code == 403


def test_tenant_settings_require_an_administrator() -> None:
    with _test_session() as db:
        owner, _, admin = _seed_users(db)
        assert ensure_tenant_admin("tenant_demo", admin) is admin
        with pytest.raises(HTTPException) as role_error:
            ensure_tenant_admin("tenant_demo", owner)
        assert role_error.value.status_code == 403
        with pytest.raises(HTTPException) as tenant_error:
            ensure_tenant_admin("another_tenant", admin)
        assert tenant_error.value.status_code == 403


def test_chat_agents_exclude_unused_gallery_agents_until_current_user_marks_used() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        owned = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="我的员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        gallery = AgentProfile(
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            is_overall=False,
            metadata_json={
                "published_to_gallery": True,
                "owner_user_id": other.id,
                "owner_username": other.username,
            },
        )
        private = AgentProfile(
            id="agent_private",
            tenant_id="tenant_demo",
            name="管理员可见私有员工",
            is_overall=False,
            metadata_json={"owner_user_id": other.id, "owner_username": other.username},
        )
        db.add(owned)
        db.add(gallery)
        db.add(private)
        db.commit()

        enterprise_rows = list_agents("tenant_demo", db=db, current_user=owner)
        assert {row.id for row in enterprise_rows} == {"agent_owned", "agent_gallery"}
        assert {row.id for row in list_chat_agents("tenant_demo", current_user=owner, db=db)} == {
            "agent_owned"
        }
        assert {row.id for row in list_chat_agents("tenant_demo", current_user=admin, db=db)} == {
            "agent_owned",
            "agent_private",
        }

        used = use_chat_agent(gallery.id, tenant_id="tenant_demo", current_user=owner, db=db)
        assert used.id == gallery.id
        assert used.metadata["used_by_current_user"] is True
        used_again = use_chat_agent(gallery.id, tenant_id="tenant_demo", current_user=owner, db=db)
        assert used_again.id == gallery.id
        assert (
            db.exec(
                select(ChatSession).where(
                    ChatSession.user_id == owner.id, ChatSession.agent_id == gallery.id
                )
            ).first()
            is None
        )
        usage_rows = db.exec(
            select(AgentUsage).where(
                AgentUsage.user_id == owner.id, AgentUsage.agent_id == gallery.id
            )
        ).all()
        assert len(usage_rows) == 1

        chat_rows = list_chat_agents("tenant_demo", current_user=owner, db=db)
        assert {row.id for row in chat_rows} == {"agent_owned", "agent_gallery"}
        assert (
            next(row for row in chat_rows if row.id == "agent_gallery").metadata[
                "used_by_current_user"
            ]
            is True
        )


def test_create_agent_records_creator_and_blocks_non_admin_overall() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)

        created = create_agent(
            AgentProfileCreateRequest(tenant_id="tenant_demo", name="新员工", source_mode="blank"),
            db=db,
            current_user=owner,
        )
        assert created.metadata["owner_user_id"] == owner.id
        assert created.metadata["owner_username"] == owner.username
        assert created.metadata["created_by_user_id"] == owner.id
        assert created.metadata["created_by_username"] == owner.username

        admin_updated = update_agent(
            created.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                metadata={
                    **created.metadata,
                    "owner_user_id": other.id,
                    "owner_username": other.username,
                    "created_by_user_id": other.id,
                    "created_by_username": other.username,
                    "role_name": "管理员可修改的业务字段",
                },
            ),
            db=db,
            current_user=admin,
        )
        assert admin_updated.metadata["owner_user_id"] == owner.id
        assert admin_updated.metadata["owner_username"] == owner.username
        assert admin_updated.metadata["created_by_user_id"] == owner.id
        assert admin_updated.metadata["created_by_username"] == owner.username
        assert admin_updated.metadata["role_name"] == "管理员可修改的业务字段"

        source = AgentProfile(
            id="agent_source",
            tenant_id="tenant_demo",
            name="源员工",
            is_overall=False,
            persona_prompt="源提示词",
            metadata_json={
                "owner_user_id": owner.id,
                "owner_username": owner.username,
                "created_by_user_id": owner.id,
                "created_by_username": owner.username,
                "published_to_gallery": True,
                "role_name": "源角色",
            },
        )
        db.add(source)
        db.commit()
        copied = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="复制员工",
                source_mode="copy",
                copy_from_agent_id=source.id,
                metadata={
                    **source.metadata_json,
                    "owner_user_id": other.id,
                    "owner_username": other.username,
                },
            ),
            db=db,
            current_user=other,
        )
        assert copied.metadata["owner_user_id"] == other.id
        assert copied.metadata["owner_username"] == other.username
        assert copied.metadata["created_by_user_id"] == other.id
        assert copied.metadata["created_by_username"] == other.username
        assert copied.metadata["role_name"] == "源角色"

        with pytest.raises(HTTPException) as create_error:
            create_agent(
                AgentProfileCreateRequest(
                    tenant_id="tenant_demo", name="普通用户整体", is_overall=True
                ),
                db=db,
                current_user=owner,
            )
        assert create_error.value.status_code == 403

        overall = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo", name="管理员整体", is_overall=True, source_mode="blank"
            ),
            db=db,
            current_user=admin,
        )
        assert overall.is_overall is True


def test_private_tool_edit_does_not_mutate_open_gallery_tool() -> None:
    with _test_session() as db:
        owner, _other, _admin = _seed_users(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        open_tool = Tool(
            id="tool_open_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气",
            method="POST",
            url="/api/weather",
        )
        db.add(agent)
        db.add(open_tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", open_tool.id, "active")
        ensure_private_resource_binding(db, "tenant_demo", agent.id, "tool", open_tool.id, "active")
        db.commit()

        updated = update_tool(
            open_tool.id,
            ToolUpdateRequest(
                tenant_id="tenant_demo",
                name="weather",
                display_name="员工天气",
                description="员工私有配置",
                url="/api/private-weather",
            ),
            agent_id=agent.id,
            db=db,
            current_user=owner,
        )

        db.refresh(open_tool)
        assert updated.id != open_tool.id
        assert open_tool.display_name == "天气"
        assert open_tool.url == "/api/weather"
        assert updated.display_name == "员工天气"
        assert updated.name.startswith("weather-agent_ow")
        visible_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == updated.id,
                AgentResourceBinding.status == "active",
            )
        ).first()
        old_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "tool",
                AgentResourceBinding.resource_id == open_tool.id,
            )
        ).first()
        assert visible_binding is not None
        assert old_binding and old_binding.status == "deleted"

        with pytest.raises(HTTPException) as rename_error:
            update_tool(
                updated.id,
                ToolUpdateRequest(
                    tenant_id="tenant_demo",
                    name="weather_renamed",
                    display_name="员工天气重命名",
                    description=updated.description,
                    url=updated.url,
                ),
                agent_id=agent.id,
                db=db,
                current_user=owner,
            )
        assert rename_error.value.status_code == 400
        assert rename_error.value.detail == "Tool name cannot be modified"


def test_tool_name_cannot_be_modified_after_create() -> None:
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        db.add(
            AgentProfile(
                id="agent_overall",
                tenant_id="tenant_demo",
                name="开放广场",
                is_overall=True,
            )
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气",
            method="POST",
            url="/api/weather",
        )
        db.add(tool)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "tool", tool.id, "active")
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            update_tool(
                tool.id,
                ToolUpdateRequest(
                    tenant_id="tenant_demo",
                    name="weather_v2",
                    display_name="天气新版",
                    url="/api/weather-v2",
                ),
                agent_id=None,
                db=db,
                current_user=admin,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Tool name cannot be modified"


def test_private_general_skill_edit_does_not_mutate_open_gallery_skill() -> None:
    with _test_session() as db:
        owner, _other, _admin = _seed_users(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        agent = AgentProfile(
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            is_overall=False,
            metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
        )
        open_skill = GeneralSkill(
            id="genskill_open_weather",
            tenant_id="tenant_demo",
            slug="weather",
            name="天气技能",
            description="开放广场版本",
            skill_markdown="# 天气技能\n",
            status="published",
        )
        db.add(agent)
        db.add(open_skill)
        db.flush()
        ensure_open_gallery_binding(db, "tenant_demo", "general_skill", open_skill.id, "active")
        ensure_private_resource_binding(
            db, "tenant_demo", agent.id, "general_skill", open_skill.id, "active"
        )
        db.commit()

        updated = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                original_slug="weather",
                slug="weather",
                name="员工天气技能",
                description="员工私有版本",
                markdown="# 员工天气技能\n",
            ),
            db=db,
            current_user=owner,
        )

        db.refresh(open_skill)
        assert updated.id != open_skill.id
        assert updated.slug.startswith("weather-")
        assert updated.name == "员工天气技能"
        assert open_skill.name == "天气技能"
        assert open_skill.description == "开放广场版本"
        assert (
            db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == "tenant_demo",
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "general_skill",
                    AgentResourceBinding.resource_id == updated.id,
                    AgentResourceBinding.status == "active",
                )
            ).first()
            is not None
        )

        with pytest.raises(HTTPException) as rename_error:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    agent_id=agent.id,
                    original_slug=updated.slug,
                    slug="weather-renamed",
                    name="员工天气技能",
                    markdown="# 员工天气技能\n",
                ),
                db=db,
                current_user=owner,
            )
        assert rename_error.value.status_code == 400
        assert rename_error.value.detail == "General skill slug cannot be modified"


def test_copy_agent_deep_copies_private_general_skill_package() -> None:
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        source = AgentProfile(
            id="agent_general_skill_source",
            tenant_id="tenant_demo",
            name="源企业微信员工",
            is_overall=False,
            metadata_json={"owner_user_id": admin.id, "owner_username": admin.username},
        )
        skill = GeneralSkill(
            id="genskill_private_wecom",
            tenant_id="tenant_demo",
            slug="wecom-unified",
            name="企业微信",
            skill_markdown=(
                "# 企业微信\n\n读取 references/wecomcli-calendar-meeting-room.md。\n"
            ),
            skill_files_json=[
                {
                    "path": "SKILL.md",
                    "content": (
                        "# 企业微信\n\n读取 references/wecomcli-calendar-meeting-room.md。\n"
                    ),
                },
                {
                    "path": "references/wecomcli-calendar-meeting-room.md",
                    "content": "# 日程查询\n",
                },
            ],
            metadata_json=agent_private_metadata(source.id),
            status="published",
        )
        db.add(source)
        db.add(skill)
        db.flush()
        ensure_private_resource_binding(
            db,
            "tenant_demo",
            source.id,
            "general_skill",
            skill.id,
            "active",
        )
        db.commit()

        copied_agent = create_agent(
            AgentProfileCreateRequest(
                tenant_id="tenant_demo",
                name="复制企业微信员工",
                source_mode="copy",
                copy_from_agent_id=source.id,
            ),
            db=db,
            current_user=admin,
        )

        copied_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == copied_agent.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.status == "active",
            )
        ).one()
        copied_skill = db.get(GeneralSkill, copied_binding.resource_id)
        assert copied_skill is not None
        assert copied_skill.id != skill.id
        assert copied_skill.slug == "wecom-unified-copy"
        assert copied_skill.skill_files_json == skill.skill_files_json
        assert copied_skill.metadata_json["owner_agent_id"] == copied_agent.id
        assert copied_skill.metadata_json["copied_from_general_skill_id"] == skill.id


def test_editing_shared_private_general_skill_forks_instead_of_mutating_source() -> None:
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        source = AgentProfile(
            id="agent_shared_skill_source",
            tenant_id="tenant_demo",
            name="共享技能源员工",
            is_overall=False,
        )
        target = AgentProfile(
            id="agent_shared_skill_target",
            tenant_id="tenant_demo",
            name="共享技能复制员工",
            is_overall=False,
        )
        original = GeneralSkill(
            id="genskill_shared_private",
            tenant_id="tenant_demo",
            slug="wecom-shared-private",
            name="企业微信共享技能",
            skill_markdown="# 原始技能\n",
            skill_files_json=[{"path": "SKILL.md", "content": "# 原始技能\n"}],
            metadata_json=agent_private_metadata(source.id),
            status="published",
        )
        db.add(source)
        db.add(target)
        db.add(original)
        db.flush()
        source_binding_metadata = agent_private_metadata(source.id)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id=source.id,
                resource_type="general_skill",
                resource_id=original.id,
                status="active",
                metadata_json=source_binding_metadata,
            )
        )
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id=target.id,
                resource_type="general_skill",
                resource_id=original.id,
                status="active",
                metadata_json=source_binding_metadata,
            )
        )
        db.commit()

        forked = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id=target.id,
                original_slug=original.slug,
                slug=original.slug,
                name="复制员工版本",
                markdown="# 复制员工修改后的技能\n",
            ),
            db=db,
            current_user=admin,
        )

        db.refresh(original)
        assert forked.id != original.id
        assert forked.slug == "wecom-shared-private-2"
        assert original.skill_markdown == "# 原始技能\n"
        assert forked.skill_markdown == "# 复制员工修改后的技能\n"
        assert forked.metadata["owner_agent_id"] == target.id
        original_target_binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == target.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == original.id,
            )
        ).one()
        assert original_target_binding.status == "deleted"


def _seed_users(db: Session) -> tuple[User, User, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    owner = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        display_name="Owner",
        password_hash="x",
    )
    other = User(
        id="user_other",
        tenant_id="tenant_demo",
        username="other",
        display_name="Other",
        password_hash="x",
    )
    admin = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        display_name="Admin",
        role="admin",
        password_hash="x",
    )
    db.add(owner)
    db.add(other)
    db.add(admin)
    db.commit()
    return owner, other, admin


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
