"""ui_configs.show_knowledge_citations 的口径回归测试。

参考 test_plaza_hot 的做法：不起 TestClient，直接调 handler，
用 in-memory SQLite + StaticPool 造最小租户数据。

口径（超级管理员「展示知识来源」开关）：
- 新建租户默认 True（= 既有行为，回答下方照旧显示「知识来源」引用卡片）；
- 超管关掉后持久化，GET 也返回 False；
- 老客户端 PUT 不带该字段时回落 True，而不是被误置为关闭。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.ui_config import (
    UIConfigUpdateRequest,
    get_enterprise_ui_config,
    update_enterprise_ui_config,
)
from app.db.models import Tenant, UIConfig, User
from app.security.auth import hash_password
from app.security.permissions import ADMIN_ROLE

TENANT = "tenant_demo"


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_tenant(db: Session) -> User:
    db.add(Tenant(id=TENANT, name="Demo"))
    admin = User(
        id="user_admin",
        tenant_id=TENANT,
        username="admin",
        password_hash=hash_password("admin"),
        role=ADMIN_ROLE,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _update_request(**patch: object) -> UIConfigUpdateRequest:
    payload: dict[str, object] = {"tenant_id": TENANT}
    payload.update(patch)
    return UIConfigUpdateRequest(**payload)


def test_new_tenant_defaults_to_showing_knowledge_citations() -> None:
    db = _test_session()
    _seed_tenant(db)

    row = get_enterprise_ui_config(tenant_id=TENANT, db=db)

    assert row.show_knowledge_citations is True
    # 落库的默认值也必须是 True（迁移里 NOT NULL DEFAULT TRUE 对齐这一点）
    assert db.get(UIConfig, TENANT).show_knowledge_citations is True


def test_admin_can_turn_off_and_it_persists() -> None:
    db = _test_session()
    admin = _seed_tenant(db)

    updated = update_enterprise_ui_config(
        _update_request(show_knowledge_citations=False),
        db=db,
        current_user=admin,
    )
    assert updated.show_knowledge_citations is False

    # 重新读（新会话读的是同一份库数据）：仍是关闭
    assert get_enterprise_ui_config(tenant_id=TENANT, db=db).show_knowledge_citations is False


def test_legacy_payload_without_the_field_keeps_it_on() -> None:
    db = _test_session()
    admin = _seed_tenant(db)

    update_enterprise_ui_config(
        _update_request(show_knowledge_citations=False),
        db=db,
        current_user=admin,
    )
    # 老前端不带该字段 → 按默认 True 处理（不会把开关静默关掉后又被重置成开，
    # 也不会因为缺字段而报错）
    reopened = update_enterprise_ui_config(_update_request(), db=db, current_user=admin)
    assert reopened.show_knowledge_citations is True


def test_knowledge_citation_switch_is_independent_from_trace_flags() -> None:
    db = _test_session()
    admin = _seed_tenant(db)

    # 执行记录开关关掉、知识来源保持展示 → 两者互不影响
    row = update_enterprise_ui_config(
        _update_request(show_thinking_trace=False),
        db=db,
        current_user=admin,
    )
    assert row.show_thinking_trace is False
    assert row.show_knowledge_citations is True

    # 只关知识来源：思考状态维持上一步的值，不会被这个开关带走
    row = update_enterprise_ui_config(
        _update_request(show_thinking_trace=False, show_knowledge_citations=False),
        db=db,
        current_user=admin,
    )
    assert row.show_thinking_trace is False
    assert row.show_knowledge_citations is False
