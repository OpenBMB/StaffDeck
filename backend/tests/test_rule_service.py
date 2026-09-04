from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ProjectDataFieldDefinition,
    RuleDefinition,
    RuleSet,
    RuleSetVersion,
    User,
)
from app.rules.schema import (
    RuleAccessDenied,
    RuleDefinitionCreate,
    RuleSetCreate,
    RuleValidationError,
    RuleVersionImmutableError,
)
from app.rules.service import RuleLibraryService


def _user(user_id: str, tenant_id: str, role: str = "member") -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        role=role,
        source="web",
        password_hash="test",
    )


def _rule_create(**overrides: Any) -> RuleDefinitionCreate:
    values: dict[str, Any] = {
        "rule_key": "scope.required",
        "name": "认证范围必填",
        "description": "认证范围必须有可核验内容",
        "workflow_nodes": ["collect"],
        "information_domains": ["certification_project"],
        "document_types": ["audit_plan"],
        "field_keys": ["certification_project.scope"],
        "execution_level": "mandatory",
        "execution_method": "deterministic",
        "condition": {
            "operator": "required",
            "field_key": "certification_project.scope",
        },
        "input_requirements": [],
        "evidence_requirements": [{"kind": "project_field"}],
        "source_refs": [{"internal_policy_ref": "POL-001"}],
        "sequence": 0,
        "enabled": True,
    }
    values.update(overrides)
    return RuleDefinitionCreate(**values)


@pytest.fixture
def rule_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rules.db'}")
    SQLModel.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    admin = _user("admin-1", "tenant_demo", role="admin")
    member = _user("member-1", "tenant_demo")
    db.add_all([admin, member])
    db.commit()
    rule_set = RuleLibraryService(db).create_rule_set(
        admin,
        RuleSetCreate(
            tenant_id="tenant_demo",
            key="energy.audit",
            name="能源审核规则",
            description="一期规则",
            management_systems=["EnMS"],
            audit_types=["recertification"],
            business_domain="energy",
        ),
    )
    yield db, admin, member, rule_set
    db.close()


def test_create_rule_set_maps_metadata_and_starts_active(rule_context) -> None:
    _db, _admin, _member, rule_set = rule_context
    assert rule_set.status == "active"
    assert rule_set.management_systems_json == ["EnMS"]
    assert rule_set.audit_types_json == ["recertification"]
    assert rule_set.business_domain == "energy"


def test_create_rule_set_requires_same_tenant_admin(rule_context) -> None:
    db, _admin, member, _rule_set = rule_context
    foreign_admin = _user("foreign-admin", "tenant_other", role="admin")
    db.add(foreign_admin)
    db.commit()
    request = RuleSetCreate(tenant_id="tenant_demo", key="other", name="Other")
    for actor in (member, foreign_admin):
        with pytest.raises(RuleAccessDenied, match="RULE_ADMIN_REQUIRED"):
            RuleLibraryService(db).create_rule_set(actor, request)


def test_rule_set_key_is_unique_per_tenant(rule_context) -> None:
    db, admin, _member, _rule_set = rule_context
    with pytest.raises(RuleValidationError, match="RULE_SET_KEY_EXISTS"):
        RuleLibraryService(db).create_rule_set(
            admin,
            RuleSetCreate(
                tenant_id=admin.tenant_id,
                key="energy.audit",
                name="重复规则集",
            ),
        )


@pytest.mark.parametrize("key", ["", "Energy.Audit", "energy/audit", "a" * 161])
def test_rule_set_key_must_be_a_bounded_stable_key(
    rule_context, key: str
) -> None:
    db, admin, _member, _rule_set = rule_context
    with pytest.raises(RuleValidationError, match="INVALID_RULE_SET_KEY"):
        RuleLibraryService(db).create_rule_set(
            admin,
            RuleSetCreate(tenant_id=admin.tenant_id, key=key, name="无效规则集"),
        )


def test_draft_versions_increment_and_persist_definitions(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    first = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    second = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create(rule_key="scope.second", sequence=1)]
    )
    assert (first.version, second.version) == (1, 2)
    persisted = db.exec(
        select(RuleDefinition).where(RuleDefinition.rule_set_version_id == second.id)
    ).one()
    assert persisted.rule_key == "scope.second"
    assert persisted.condition_json["operator"] == "required"


def test_declared_tenant_field_can_be_used_by_rule(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    db.add(
        ProjectDataFieldDefinition(
            tenant_id=admin.tenant_id,
            field_key="tenant.energy_grade",
            label="能源等级",
            value_type="text",
            information_domain="energy",
            scope="project",
            status="active",
        )
    )
    db.commit()
    draft = RuleLibraryService(db).create_draft_version(
        rule_set,
        admin,
        [
            _rule_create(
                field_keys=["tenant.energy_grade"],
                condition={"operator": "required", "field_key": "tenant.energy_grade"},
            )
        ],
    )
    assert draft.version == 1


def test_validate_version_returns_codes_without_mutating(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    definition = db.exec(
        select(RuleDefinition).where(RuleDefinition.rule_set_version_id == draft.id)
    ).one()
    definition.source_refs_json = []
    db.add(definition)
    db.commit()
    before = (draft.status, draft.content_sha256, draft.updated_at)

    assert RuleLibraryService(db).validate_version(draft.id, admin) == [
        "MANDATORY_RULE_SOURCE_REQUIRED"
    ]
    db.refresh(draft)
    assert (draft.status, draft.content_sha256, draft.updated_at) == before


def test_only_tenant_admin_can_publish(rule_context) -> None:
    db, admin, member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    with pytest.raises(RuleAccessDenied, match="RULE_ADMIN_REQUIRED"):
        RuleLibraryService(db).publish_version(draft.id, member)


def test_published_version_cannot_be_edited_in_place(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    published = RuleLibraryService(db).publish_version(draft.id, admin)
    with pytest.raises(RuleVersionImmutableError, match="RULE_VERSION_IMMUTABLE"):
        RuleLibraryService(db).replace_rules(
            published.id, admin, [_rule_create(name="新名称")]
        )


def test_publish_stores_content_hash_and_published_actor(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    published = RuleLibraryService(db).publish_version(draft.id, admin)
    assert len(published.content_sha256) == 64
    assert published.published_by_user_id == admin.id
    assert published.published_at is not None
    assert published.status == "published"


def test_second_publish_is_rejected(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    RuleLibraryService(db).publish_version(draft.id, admin)
    with pytest.raises(RuleVersionImmutableError, match="RULE_VERSION_IMMUTABLE"):
        RuleLibraryService(db).publish_version(draft.id, admin)


def test_failed_publish_preserves_draft_and_previous_published_version(
    rule_context,
) -> None:
    db, admin, _member, rule_set = rule_context
    first = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    published = RuleLibraryService(db).publish_version(first.id, admin)
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create(rule_key="scope.second")]
    )
    definition = db.exec(
        select(RuleDefinition).where(RuleDefinition.rule_set_version_id == draft.id)
    ).one()
    definition.evidence_requirements_json = []
    db.add(definition)
    db.commit()

    with pytest.raises(RuleValidationError, match="MANDATORY_RULE_EVIDENCE_REQUIRED"):
        RuleLibraryService(db).publish_version(draft.id, admin)

    db.refresh(draft)
    db.refresh(published)
    assert draft.status == "draft"
    assert draft.published_by_user_id is None
    assert draft.published_at is None
    assert published.status == "published"


def test_replace_rules_rewrites_only_the_draft(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    replaced = RuleLibraryService(db).replace_rules(
        draft.id, admin, [_rule_create(rule_key="scope.replaced", name="替换")]
    )
    rows = db.exec(
        select(RuleDefinition).where(RuleDefinition.rule_set_version_id == draft.id)
    ).all()
    assert replaced.id == draft.id
    assert [row.rule_key for row in rows] == ["scope.replaced"]
    assert len(replaced.content_sha256) == 64


def test_cross_tenant_version_is_not_accessible(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(
        rule_set, admin, [_rule_create()]
    )
    foreign_admin = _user("foreign-admin-2", "tenant_other", role="admin")
    db.add(foreign_admin)
    db.commit()
    with pytest.raises(RuleAccessDenied, match="RULE_ADMIN_REQUIRED"):
        RuleLibraryService(db).validate_version(draft.id, foreign_admin)


def test_rule_set_row_is_persisted(rule_context) -> None:
    db, _admin, _member, rule_set = rule_context
    assert db.get(RuleSet, rule_set.id) is not None
    assert db.exec(select(RuleSetVersion)).all() == []
