"""域（LDAP/AD）登录与账号同步测试。

用 monkeypatch 替换 ldap_client 的两个入口（is_enabled / authenticate），
不依赖真实域控即可覆盖：首次登录建号、登录同步更新、口令错误回退、
域控不可用时回退、关闭回退时的 503/401 行为。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.auth import LoginRequest, login
from app.db.models import Tenant, User
from app.security import ldap_client
from app.security.auth import hash_password


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id="tenant_demo", name="Demo"))
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def ldap_on(monkeypatch):
    """开启域认证，并可注入 authenticate 的返回值 / 异常。"""

    def _install(profile=None, error=None):
        monkeypatch.setattr(ldap_client, "is_enabled", lambda: True)
        if error is not None:

            def _raise(_username, _password):
                raise error

            monkeypatch.setattr(ldap_client, "authenticate", _raise)
        else:
            monkeypatch.setattr(ldap_client, "authenticate", lambda _u, _p: profile)
        return profile

    return _install


def test_ldap_login_creates_local_account(db, ldap_on):
    ldap_on(
        ldap_client.LdapUser(
            username="chenjiali",
            display_name="陈佳丽",
            department="AILab",
            dn="CN=陈佳丽,OU=复星旅游文化集团,DC=fosun,DC=com",
        )
    )

    session = login(
        LoginRequest(tenant_id="tenant_demo", username="chenjiali", password="domain-pwd"), db
    )

    assert session.user.username == "chenjiali"
    assert session.user.display_name == "陈佳丽"
    assert session.user.department == "AILab"
    assert session.user.auth_source == "ldap"
    assert session.user.source == "web"  # 成员选择器按 source=web 过滤

    row = db.exec(select(User).where(User.username == "chenjiali")).first()
    assert row is not None
    assert row.auth_source == "ldap"
    # 域账号不保存域口令：本地口令校验必须失败
    assert not _local_login_works(db, "chenjiali", "domain-pwd")


def test_ldap_login_updates_existing_account(db, ldap_on):
    db.add(
        User(
            id="user_chenjiali",
            tenant_id="tenant_demo",
            username="chenjiali",
            display_name="旧名字",
            department="旧部门",
            password_hash=hash_password("local-pwd"),
        )
    )
    db.commit()
    ldap_on(
        ldap_client.LdapUser(
            username="chenjiali",
            display_name="陈佳丽",
            department="AILab",
            dn="CN=陈佳丽,OU=PowerBi用组,DC=fosun,DC=com",
        )
    )

    session = login(
        LoginRequest(tenant_id="tenant_demo", username="chenjiali", password="domain-pwd"), db
    )

    assert session.user.id == "user_chenjiali"
    assert session.user.display_name == "陈佳丽"
    assert session.user.department == "AILab"
    assert session.user.auth_source == "ldap"


def test_ldap_login_respects_manual_department(db, ldap_on):
    """部门保护：产品里手动改过的部门不被域登录覆盖。"""
    db.add(
        User(
            id="user_chenjiali",
            tenant_id="tenant_demo",
            username="chenjiali",
            display_name="陈佳丽",
            department="手动部门",
            department_manual=True,
            password_hash=hash_password("local-pwd"),
        )
    )
    db.commit()
    ldap_on(
        ldap_client.LdapUser(
            username="chenjiali",
            display_name="陈佳丽",
            department="AILab",
            dn="CN=陈佳丽,OU=AILab,DC=fosun,DC=com",
        )
    )

    session = login(
        LoginRequest(tenant_id="tenant_demo", username="chenjiali", password="domain-pwd"), db
    )

    # 域里的 AILab 不覆盖手动设置的部门
    assert session.user.department == "手动部门"
    assert session.user.department_manual is True
    # 显示名仍按域同步
    assert session.user.display_name == "陈佳丽"


def test_ldap_login_overwrites_unprotected_department(db, ldap_on):
    """未打保护标记的部门仍按域同步覆盖（回归）。"""
    db.add(
        User(
            id="user_chenjiali",
            tenant_id="tenant_demo",
            username="chenjiali",
            department="旧部门",
            password_hash=hash_password("local-pwd"),
        )
    )
    db.commit()
    ldap_on(
        ldap_client.LdapUser(
            username="chenjiali",
            display_name="陈佳丽",
            department="AILab",
            dn="CN=陈佳丽,OU=AILab,DC=fosun,DC=com",
        )
    )

    session = login(
        LoginRequest(tenant_id="tenant_demo", username="chenjiali", password="domain-pwd"), db
    )

    assert session.user.department == "AILab"


class _FakeAttr:
    def __init__(self, value):
        self.value = value


class _FakeEntry:
    """ldap3 Entry 的最小替身：只支持 getattr(...).value 读取。"""

    def __init__(self, values: dict):
        self._values = values

    def __getattr__(self, name):
        return _FakeAttr(self._values.get(name))


def test_department_falls_back_to_ou_when_attribute_empty():
    """AD 的 department 属性常为空：应回退用 DN 中最近的 OU 作为部门。"""
    from app.config import get_settings

    settings = get_settings()
    dn = "CN=陈佳丽,OU=AILab,OU=复星旅游文化集团,DC=fosun,DC=com"
    entry = _FakeEntry({"sAMAccountName": "chenjiali", "displayName": "陈佳丽", "department": None})

    profile = ldap_client._profile_from_entry(settings, entry, dn)

    assert profile.username == "chenjiali"
    assert profile.display_name == "陈佳丽"
    assert profile.department == "AILab"


def test_department_prefers_explicit_attribute_over_dn():
    from app.config import get_settings

    settings = get_settings()
    dn = "CN=陈佳丽,OU=复星旅游文化集团,DC=fosun,DC=com"
    entry = _FakeEntry(
        {"sAMAccountName": "chenjiali", "displayName": "陈佳丽", "department": "数据中心"}
    )

    profile = ldap_client._profile_from_entry(settings, entry, dn)

    assert profile.department == "数据中心"


def test_department_from_dn_helpers():
    assert ldap_client._department_from_dn("CN=张三,OU=AILab,OU=集团,DC=fosun,DC=com") == "AILab"
    assert ldap_client._department_from_dn("OU=集团,DC=fosun,DC=com") == "集团"
    assert ldap_client._department_from_dn("CN=张三,DC=fosun,DC=com") is None
    assert ldap_client._department_from_dn("") is None


def test_ldap_miss_falls_back_to_local_password(db, ldap_on):
    local = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash=hash_password("local-pwd"),
    )
    db.add(local)
    db.commit()
    ldap_on(profile=None)  # 域里没有这个账号

    session = login(
        LoginRequest(tenant_id="tenant_demo", username="admin", password="local-pwd"), db
    )

    assert session.user.id == "user_admin"
    assert session.user.auth_source is None


def test_ldap_unavailable_falls_back_to_local_password(db, ldap_on):
    local = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        password_hash=hash_password("local-pwd"),
    )
    db.add(local)
    db.commit()
    ldap_on(error=ldap_client.LdapUnavailable("域控连不上"))

    session = login(
        LoginRequest(tenant_id="tenant_demo", username="admin", password="local-pwd"), db
    )

    assert session.user.id == "user_admin"


def test_ldap_unavailable_without_fallback_returns_503(db, ldap_on, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ldap_local_fallback", False)
    db.add(
        User(
            id="user_admin",
            tenant_id="tenant_demo",
            username="admin",
            password_hash=hash_password("local-pwd"),
        )
    )
    db.commit()
    ldap_on(error=ldap_client.LdapUnavailable("域控连不上"))

    with pytest.raises(HTTPException) as error:
        login(LoginRequest(tenant_id="tenant_demo", username="admin", password="local-pwd"), db)

    assert error.value.status_code == 503


def test_ldap_wrong_password_without_fallback_returns_401(db, ldap_on, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ldap_local_fallback", False)
    db.add(
        User(
            id="user_admin",
            tenant_id="tenant_demo",
            username="admin",
            password_hash=hash_password("local-pwd"),
        )
    )
    db.commit()
    ldap_on(profile=None)

    with pytest.raises(HTTPException) as error:
        login(LoginRequest(tenant_id="tenant_demo", username="admin", password="local-pwd"), db)

    assert error.value.status_code == 401


def test_ldap_disabled_keeps_local_only(db):
    local = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        password_hash=hash_password("local-pwd"),
    )
    db.add(local)
    db.commit()
    # 未开启域认证：即便 ldap_client 被误配也不应介入
    session = login(
        LoginRequest(tenant_id="tenant_demo", username="admin", password="local-pwd"), db
    )
    assert session.user.id == "user_admin"


def _local_login_works(db: Session, username: str, password: str) -> bool:
    from app.security.auth import verify_password

    row = db.exec(select(User).where(User.username == username)).first()
    return bool(row and verify_password(password, row.password_hash))
