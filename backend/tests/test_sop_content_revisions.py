from copy import deepcopy

import pytest
from app.api.skills import create_skill, update_skill
from app.db.models import AgentResourceBinding, SkillVersion, Tenant
from app.skills.skill_schema import SkillCreateRequest, SkillUpdateRequest
from fastapi import HTTPException
from sqlmodel import select
from tests.test_skill_editing_and_stats import _admin_user, _skill_card, _test_session


def test_content_save_advances_above_all_history_and_keeps_old_snapshots():
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        original = _skill_card()
        created = create_skill(
            SkillCreateRequest(tenant_id="tenant_demo", content=original),
            agent_id=None, db=db, current_user=_admin_user(),
        )
        high = original.model_copy(deep=True, update={"version": "2.1.0"})
        db.add(SkillVersion(
            tenant_id="tenant_demo", skill_id=original.skill_id, version=high.version,
            name=high.name, content_json=high.model_dump(), status="draft",
        ))
        db.commit()
        before = deepcopy(db.exec(select(SkillVersion).where(
            SkillVersion.version == created.version
        )).one().content_json)
        edited = original.model_copy(deep=True, update={"name": "Edited", "version": "1.0.0"})
        saved = update_skill(
            original.skill_id, SkillUpdateRequest(tenant_id="tenant_demo", content=edited),
            agent_id=None, db=db, current_user=_admin_user(),
        )
        assert saved.version == "2.1.1"
        assert saved.content.version == saved.version
        assert db.exec(select(SkillVersion).where(
            SkillVersion.version == created.version
        )).one().content_json == before


def test_content_save_cannot_implicitly_change_source_lifecycle_or_bindings():
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        card = _skill_card()
        create_skill(
            SkillCreateRequest(tenant_id="tenant_demo", content=card),
            agent_id=None, db=db, current_user=_admin_user(),
        )
        before = [(row.id, row.status) for row in db.exec(select(AgentResourceBinding)).all()]
        with pytest.raises(HTTPException) as error:
            update_skill(
                card.skill_id, SkillUpdateRequest(tenant_id="tenant_demo", content=card, status="archived"),
                agent_id=None, db=db, current_user=_admin_user(),
            )
        assert error.value.status_code == 409
        assert [(row.id, row.status) for row in db.exec(select(AgentResourceBinding)).all()] == before
