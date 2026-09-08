from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.auth import (
    LoginRequest,
    UpdateProfileRequest,
    UserCreateRequest,
    UserUpdateRequest,
    create_user,
    login,
    update_my_profile,
    update_user,
)
from app.db.models import Tenant, User
from app.security.auth import hash_password


def test_unknown_login_does_not_create_account() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()

        try:
            login(LoginRequest(tenant_id="tenant_demo", username="missing", password="secret"), db)
        except HTTPException as error:
            assert error.status_code == 401
            assert error.detail == "Invalid username or password"
        else:
            raise AssertionError("unknown account must not be created during login")

        assert db.exec(select(User)).all() == []


def test_database_role_controls_account_management() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        member_named_admin = User(
            id="user_named_admin",
            tenant_id="tenant_demo",
            username="admin",
            role="member",
            password_hash=hash_password("secret"),
        )
        role_admin = User(
            id="user_role_admin",
            tenant_id="tenant_demo",
            username="ops",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(member_named_admin)
        db.add(role_admin)
        db.commit()

        try:
            create_user(
                UserCreateRequest(tenant_id="tenant_demo", username="blocked", password="secret"),
                member_named_admin,
                db,
            )
        except HTTPException as error:
            assert error.status_code == 403
        else:
            raise AssertionError("an admin-looking username must not grant administrator access")

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_demo",
                username="created_admin",
                password="secret",
                role="admin",
            ),
            role_admin,
            db,
        )
        assert created.role == "admin"

        updated = update_user(
            created.id,
            UserUpdateRequest(tenant_id="tenant_demo", role="member"),
            role_admin,
            db,
        )
        assert updated.role == "member"


def test_admin_password_update_allows_login_with_account() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        admin = User(
            id="admin",
            tenant_id="tenant_demo",
            username="admin",
            role="admin",
            password_hash=hash_password("admin"),
        )
        member = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="user_demo",
            display_name="zongkelong",
            role="member",
            password_hash=hash_password("old-password"),
        )
        db.add(admin)
        db.add(member)
        db.commit()

        update_user(
            member.id,
            UserUpdateRequest(tenant_id="tenant_demo", password="123456"),
            admin,
            db,
        )

        session = login(
            LoginRequest(tenant_id="tenant_demo", username="user_demo", password="123456"),
            db,
        )

        assert session.user.id == member.id
        assert session.user.username == "user_demo"

        # 显示名不参与登录匹配
        try:
            login(
                LoginRequest(tenant_id="tenant_demo", username="zongkelong", password="123456"),
                db,
            )
        except HTTPException as error:
            assert error.status_code == 401
        else:
            raise AssertionError("display name must not be used for login")


def test_display_name_cannot_be_used_to_login() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            User(
                id="member_one",
                tenant_id="tenant_demo",
                username="member_one",
                display_name="duplicate",
                password_hash=hash_password("123456"),
            )
        )
        db.add(
            User(
                id="member_two",
                tenant_id="tenant_demo",
                username="member_two",
                display_name="duplicate",
                password_hash=hash_password("123456"),
            )
        )
        db.commit()

        try:
            login(
                LoginRequest(tenant_id="tenant_demo", username="duplicate", password="123456"),
                db,
            )
        except HTTPException as error:
            assert error.status_code == 401
            assert error.detail == "Invalid username or password"
        else:
            raise AssertionError("an ambiguous display name must not authenticate any account")


def test_create_user_persists_department() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        admin = User(
            id="admin",
            tenant_id="tenant_demo",
            username="admin",
            role="admin",
            password_hash=hash_password("secret"),
        )
        db.add(admin)
        db.commit()

        created = create_user(
            UserCreateRequest(
                tenant_id="tenant_demo",
                username="li_si",
                password="secret",
                display_name="李四",
                department="市场部",
            ),
            admin,
            db,
        )
        assert created.department == "市场部"


def test_update_user_changes_department() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        admin = User(
            id="admin",
            tenant_id="tenant_demo",
            username="admin",
            role="admin",
            password_hash=hash_password("secret"),
        )
        member = User(
            id="member",
            tenant_id="tenant_demo",
            username="member",
            role="member",
            password_hash=hash_password("secret"),
        )
        db.add(admin)
        db.add(member)
        db.commit()

        updated = update_user(
            member.id,
            UserUpdateRequest(tenant_id="tenant_demo", department="行政部"),
            admin,
            db,
        )
        assert updated.department == "行政部"
        # 清空部门:传空串落库为 None
        cleared = update_user(
            member.id,
            UserUpdateRequest(tenant_id="tenant_demo", department=""),
            admin,
            db,
        )
        assert cleared.department is None


def test_update_my_profile_changes_own_display_name() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        member = User(
            id="member",
            tenant_id="tenant_demo",
            username="member",
            display_name="旧名字",
            role="member",
            password_hash=hash_password("secret"),
        )
        db.add(member)
        db.commit()

        updated = update_my_profile(UpdateProfileRequest(display_name="  新名字  "), member, db)
        assert updated.display_name == "新名字"

        # 置空串:回退为用户名(与管理员编辑同规则)
        fallback = update_my_profile(UpdateProfileRequest(display_name="   "), member, db)
        assert fallback.display_name == "member"


def test_update_my_profile_changes_own_department() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        member = User(
            id="member",
            tenant_id="tenant_demo",
            username="member",
            display_name="成员",
            department="原部门",
            role="member",
            password_hash=hash_password("secret"),
        )
        db.add(member)
        db.commit()

        # 仅传 department:显示名保持不变,部门被更新并裁剪首尾空格
        updated = update_my_profile(UpdateProfileRequest(department="  新部门  "), member, db)
        assert updated.display_name == "成员"
        assert updated.department == "新部门"

        # 传空串:部门被清空
        cleared = update_my_profile(UpdateProfileRequest(department="  "), member, db)
        assert cleared.department is None


def test_manual_department_sets_protection_flag() -> None:
    """部门保护：本人或管理员手动改部门后打标记，供域登录同步判断。"""
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        admin = User(
            id="admin",
            tenant_id="tenant_demo",
            username="admin",
            role="admin",
            password_hash=hash_password("secret"),
        )
        member = User(
            id="member",
            tenant_id="tenant_demo",
            username="member",
            department="原部门",
            password_hash=hash_password("secret"),
        )
        db.add(admin)
        db.add(member)
        db.commit()

        updated = update_my_profile(UpdateProfileRequest(department="我的部门"), member, db)
        assert updated.department == "我的部门"
        assert updated.department_manual is True

        # 管理员手动改同样打标记（含清空）
        managed = update_user(
            member.id,
            UserUpdateRequest(tenant_id="tenant_demo", department=""),
            admin,
            db,
        )
        assert managed.department is None
        assert managed.department_manual is True


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
