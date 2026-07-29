"""用户头像上传:data_url 存库、字节头嗅探、大小限制、me/login 带出。"""

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.api.auth as auth_api
from app.db import get_session
from app.db.models import Tenant, User, UserAvatar
from app.security.auth import create_access_token, hash_password

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_GIF_BYTES = b"GIF89a" + b"\x00" * 32
_WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 16


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _make_client(engine):
    app = FastAPI()
    app.include_router(auth_api.router)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    return TestClient(app)


def _seed_user(engine, *, password: str = "secret") -> User:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_web",
            tenant_id="tenant_demo",
            username="zhangsan",
            display_name="张三",
            password_hash=hash_password(password),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user)}"}


def _upload(
    client: TestClient,
    user: User,
    data: bytes,
    *,
    content_type: str = "image/png",
    filename: str = "avatar.png",
):
    return client.put(
        "/api/auth/me/avatar",
        files={"file": (filename, data, content_type)},
        headers=_auth(user),
    )


def test_upload_avatar_and_me_login_carry_avatar_pointer() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    response = _upload(client, user, _PNG_BYTES)
    assert response.status_code == 200
    # 响应不内联二进制:avatar_url 一律为资源指针
    assert response.json()["avatar_url"] == "/api/auth/me/avatar"
    expected_data_url = f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode('ascii')}"

    me = client.get("/api/auth/me", headers=_auth(user))
    assert me.status_code == 200
    assert me.json()["avatar_url"] == "/api/auth/me/avatar"

    login = client.post(
        "/api/auth/login",
        json={"tenant_id": "tenant_demo", "username": "zhangsan", "password": "secret"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["avatar_url"] == "/api/auth/me/avatar"

    with Session(engine) as db:
        row = db.get(UserAvatar, user.id)
        # 完整 data_url 只存在于库里
        assert row is not None and row.data_url == expected_data_url


def test_get_avatar_returns_image_bytes() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    assert _upload(client, user, _PNG_BYTES).status_code == 200
    response = client.get("/api/auth/me/avatar", headers=_auth(user))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == _PNG_BYTES
    assert "no-cache" in response.headers.get("cache-control", "")


def test_get_avatar_404_without_avatar_and_requires_auth() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    assert client.get("/api/auth/me/avatar", headers=_auth(user)).status_code == 404
    assert client.get("/api/auth/me/avatar").status_code == 401


def test_upload_avatar_overwrites_existing() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    assert _upload(client, user, _PNG_BYTES).status_code == 200
    replaced = _upload(client, user, _GIF_BYTES, content_type="image/gif", filename="avatar.gif")
    assert replaced.status_code == 200
    assert replaced.json()["avatar_url"] == "/api/auth/me/avatar"

    fetched = client.get("/api/auth/me/avatar", headers=_auth(user))
    assert fetched.content == _GIF_BYTES
    assert fetched.headers["content-type"].startswith("image/gif")

    with Session(engine) as db:
        rows = db.exec(select(UserAvatar)).all()
        # upsert:仍只有一行,内容为后传的 gif
        assert len(rows) == 1
        assert rows[0].data_url.startswith("data:image/gif;base64,")


def test_upload_accepts_webp_by_magic() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    response = _upload(client, user, _WEBP_BYTES, content_type="image/webp", filename="a.webp")
    assert response.status_code == 200
    assert response.json()["avatar_url"] == "/api/auth/me/avatar"
    fetched = client.get("/api/auth/me/avatar", headers=_auth(user))
    assert fetched.headers["content-type"].startswith("image/webp")
    assert fetched.content == _WEBP_BYTES


def test_delete_avatar_is_idempotent() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    assert _upload(client, user, _PNG_BYTES).status_code == 200
    assert client.delete("/api/auth/me/avatar", headers=_auth(user)).status_code == 204
    me = client.get("/api/auth/me", headers=_auth(user))
    assert me.json()["avatar_url"] is None
    assert client.get("/api/auth/me/avatar", headers=_auth(user)).status_code == 404
    with Session(engine) as db:
        assert db.exec(select(UserAvatar)).all() == []
    # 再删一次仍 204
    assert client.delete("/api/auth/me/avatar", headers=_auth(user)).status_code == 204


def test_delete_user_cascades_avatar() -> None:
    """管理员删除用户:其头像记录一并删除,不留孤儿行。"""
    engine = _test_engine()
    member = _seed_user(engine)
    with Session(engine) as db:
        admin = User(
            id="user_admin",
            tenant_id="tenant_demo",
            username="admin2",
            display_name="管理员",
            password_hash=hash_password("secret"),
            role="admin",
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        db.expunge(admin)
    client = _make_client(engine)

    assert _upload(client, member, _PNG_BYTES).status_code == 200
    deleted = client.delete(
        "/api/auth/users/user_web?tenant_id=tenant_demo", headers=_auth(admin)
    )
    assert deleted.status_code == 200
    with Session(engine) as db:
        assert db.get(User, "user_web") is None
        assert db.get(UserAvatar, "user_web") is None


def test_upload_rejects_non_image() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    response = _upload(
        client, user, b"plain text content", content_type="text/plain", filename="a.txt"
    )
    assert response.status_code == 400


def test_upload_rejects_oversize() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024)
    response = _upload(client, user, oversize)
    assert response.status_code == 413


def test_upload_rejects_oversize_by_content_length() -> None:
    """请求体 Content-Length 明显超限(>2MB+multipart 开销):预检直接 413,不读完整内容。"""
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024 + 128 * 1024)
    response = _upload(client, user, oversize)
    assert response.status_code == 413


def test_upload_rejects_fake_content_type() -> None:
    """声明 image/png 但字节头不是图片:按内容嗅探拒绝。"""
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    response = _upload(client, user, b"definitely not an image", content_type="image/png")
    assert response.status_code == 400


def test_me_and_login_without_avatar_return_none() -> None:
    engine = _test_engine()
    user = _seed_user(engine)
    client = _make_client(engine)

    me = client.get("/api/auth/me", headers=_auth(user))
    assert me.status_code == 200
    assert me.json()["avatar_url"] is None

    login = client.post(
        "/api/auth/login",
        json={"tenant_id": "tenant_demo", "username": "zhangsan", "password": "secret"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["avatar_url"] is None
