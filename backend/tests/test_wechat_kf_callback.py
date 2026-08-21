import base64
import hashlib
import json
import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml.common import DefusedXmlException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.api.wechat_kf as wechat_kf_api
from app.channels.adapters.wechat_kf import (
    WeChatKfAdapter,
    _split_utf8_text,
    normalize_wechat_kf_message,
)
from app.channels.crypto import encrypt_channel_secret
from app.db.models import ChannelBinding, ChannelInboundEvent, Tenant, WeChatKfAccount


def _encrypt(plaintext: str, aes_key: str, receive_id: str) -> str:
    key = base64.b64decode(aes_key + "=")
    payload = b"0123456789abcdef" + struct.pack("!I", len(plaintext.encode()))
    payload += plaintext.encode() + receive_id.encode()
    padding = 32 - len(payload) % 32
    payload += bytes((padding,)) * padding
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(payload) + encryptor.finalize()).decode()


def _signature(token: str, timestamp: str, nonce: str, ciphertext: str) -> str:
    return hashlib.sha1("".join(sorted((token, timestamp, nonce, ciphertext))).encode()).hexdigest()


def _client(monkeypatch) -> tuple[TestClient, object, str, str, str, str]:
    db_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(db_engine)
    token = "callback-token"
    aes_key = base64.b64encode(bytes(range(32))).decode().rstrip("=")
    corp_id = "ww1234567890"
    open_kfid = "wk1234567890"
    with Session(db_engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        binding = ChannelBinding(
            tenant_id="tenant_demo",
            agent_id="agent_1",
            channel="wechat_kf",
            status="active",
            credentials_enc=encrypt_channel_secret(
                '{"secret":"secret","callback_token":"callback-token",'
                f'"encoding_aes_key":"{aes_key}"}}'
            ),
            config_json={
                "corp_id": corp_id,
                "open_kfid": open_kfid,
                "sync_cursor": "",
            },
            external_account_key=(
                f"wechat_kf:corp:{len(corp_id)}:{corp_id}:"
                f"kf:{len(open_kfid)}:{open_kfid}"
            ),
            identity_scope_key=f"{corp_id}:{open_kfid}",
        )
        db.add(binding)
        db.commit()
        binding_id = binding.id
        db.add(
            WeChatKfAccount(
                tenant_id="tenant_demo",
                binding_id=binding_id,
                open_kfid=open_kfid,
                agent_id="agent_1",
            )
        )
        db.commit()
    monkeypatch.setattr(wechat_kf_api, "engine", db_engine)
    app = FastAPI()
    app.include_router(wechat_kf_api.router)
    return TestClient(app), db_engine, binding_id, token, aes_key, corp_id


def test_wechat_kf_callback_verification_returns_decrypted_echostr(monkeypatch) -> None:
    client, _engine, binding_id, token, aes_key, corp_id = _client(monkeypatch)
    timestamp = "1724000000"
    nonce = "nonce-value"
    ciphertext = _encrypt("verified", aes_key, corp_id)

    response = client.get(
        f"/api/channels/wechat-kf/{binding_id}/callback",
        params={
            "msg_signature": _signature(token, timestamp, nonce, ciphertext),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": ciphertext,
        },
    )

    assert response.status_code == 200
    assert response.text == "verified"


def test_wechat_kf_callback_rejects_invalid_signature(monkeypatch) -> None:
    client, _engine, binding_id, _token, aes_key, corp_id = _client(monkeypatch)
    ciphertext = _encrypt("verified", aes_key, corp_id)
    response = client.get(
        f"/api/channels/wechat-kf/{binding_id}/callback",
        params={
            "msg_signature": "invalid",
            "timestamp": "1724000000",
            "nonce": "nonce-value",
            "echostr": ciphertext,
        },
    )
    assert response.status_code == 403


def test_wechat_kf_xml_parser_rejects_entity_expansion() -> None:
    with pytest.raises(DefusedXmlException):
        wechat_kf_api._parse_callback_xml(
            b'<!DOCTYPE foo [<!ENTITY x "expanded">]><xml><Encrypt>&x;</Encrypt></xml>'
        )


def test_wechat_kf_callback_syncs_and_stages_customer_message(monkeypatch) -> None:
    client, db_engine, binding_id, token, aes_key, corp_id = _client(monkeypatch)
    open_kfid = "wk1234567890"
    plaintext = (
        "<xml><Event>kf_msg_or_event</Event><Token>sync-token</Token>"
        f"<OpenKfId>{open_kfid}</OpenKfId></xml>"
    )
    ciphertext = _encrypt(plaintext, aes_key, corp_id)
    captured: dict[str, str] = {}

    class FakeAdapter(WeChatKfAdapter):
        def sync_messages(self, binding, *, callback_token: str, cursor: str, open_kfid: str = ""):
            captured.update(token=callback_token, cursor=cursor)
            return {
                "errcode": 0,
                "next_cursor": "cursor-1",
                "has_more": 0,
                "msg_list": [
                    {
                        "msgid": "msg-1",
                        "open_kfid": open_kfid,
                        "external_userid": "external-1",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "你好"},
                    }
                ],
            }

    monkeypatch.setattr(wechat_kf_api, "get_channel_adapter", lambda _channel: FakeAdapter())
    monkeypatch.setattr(wechat_kf_api, "wake_staged_inbound_worker", lambda: None)
    response = client.post(
        f"/api/channels/wechat-kf/{binding_id}/callback",
        params={
            "msg_signature": _signature(token, "1724000000", "nonce", ciphertext),
            "timestamp": "1724000000",
            "nonce": "nonce",
        },
        content=f"<xml><Encrypt><![CDATA[{ciphertext}]]></Encrypt></xml>",
    )

    assert response.status_code == 200
    assert captured == {"token": "sync-token", "cursor": ""}
    with Session(db_engine) as db:
        event = db.exec(select(ChannelInboundEvent)).one()
        assert event.event_id == "msg-1"
        assert event.target_json == {"to_user_id": "external-1", "open_kfid": open_kfid}
        account = db.exec(select(WeChatKfAccount)).one()
        assert account.sync_cursor == "cursor-1"


def test_wechat_kf_replay_accepts_account_scope(monkeypatch) -> None:
    _client_instance, db_engine, binding_id, _token, _aes_key, corp_id = _client(monkeypatch)
    open_kfid = "wk1234567890"
    with Session(db_engine) as db:
        event = ChannelInboundEvent(
            tenant_id="tenant_demo",
            binding_id=binding_id,
            channel="wechat_kf",
            event_id="replay-msg",
            payload_json={
                "schema_version": 1,
                "account": {"scope": f"{corp_id}:{open_kfid}"},
                "inbound": {
                    "channel": "wechat_kf",
                    "event_id": "replay-msg",
                    "from_user_id": "external-1",
                    "to_user_id": open_kfid,
                    "session_id": "external-1",
                    "group_id": "",
                    "context_token": open_kfid,
                    "text": "你好",
                    "is_group": False,
                    "raw": {},
                    "sender_name": "",
                    "account_scope": f"{corp_id}:{open_kfid}",
                    "attachments": [],
                },
            },
            target_json={"to_user_id": "external-1", "open_kfid": open_kfid},
            status="received",
        )
        db.add(event)
        db.commit()
        event_id = event.id

    from app.channels.service_intake import _decode_and_validate_staged_event

    with Session(db_engine) as db:
        row = db.get(ChannelInboundEvent, event_id)
        binding = db.get(ChannelBinding, binding_id)
        assert _decode_and_validate_staged_event(row, binding).to_user_id == open_kfid


def test_wechat_kf_callback_rejects_unbound_account(monkeypatch) -> None:
    client, db_engine, binding_id, token, aes_key, corp_id = _client(monkeypatch)
    with Session(db_engine) as db:
        db.delete(
            db.exec(
                select(WeChatKfAccount).where(
                    WeChatKfAccount.binding_id == binding_id,
                )
            ).one()
        )
        db.commit()
    plaintext = (
        "<xml><Event>kf_msg_or_event</Event><Token>sync-token</Token>"
        "<OpenKfId>wk-unbound</OpenKfId></xml>"
    )
    ciphertext = _encrypt(plaintext, aes_key, corp_id)

    response = client.post(
        f"/api/channels/wechat-kf/{binding_id}/callback",
        params={
            "msg_signature": _signature(token, "1724000000", "nonce", ciphertext),
            "timestamp": "1724000000",
            "nonce": "nonce",
        },
        content=f"<xml><Encrypt><![CDATA[{ciphertext}]]></Encrypt></xml>",
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "该客服账号尚未绑定 StaffDeck 渠道"


def test_wechat_kf_normalize_ignores_servicer_messages() -> None:
    raw = {
        "msgid": "msg-1",
        "open_kfid": "wk-1",
        "external_userid": "external-1",
        "origin": 5,
        "msgtype": "text",
        "text": {"content": "人工回复"},
    }
    assert normalize_wechat_kf_message(raw) is None


def test_wechat_kf_normalizes_image_and_file_messages() -> None:
    image = normalize_wechat_kf_message(
        {
            "msgid": "image-1",
            "open_kfid": "wk-1",
            "external_userid": "external-1",
            "origin": 3,
            "msgtype": "image",
            "image": {"media_id": "media-image"},
        }
    )
    assert image is not None
    assert image.text == ""
    assert image.attachments[0].kind == "image"
    assert image.attachments[0].media_id == "media-image"
    assert image.attachments[0].download_params["provider_max_bytes"] == 2 * 1024 * 1024

    file = normalize_wechat_kf_message(
        {
            "msgid": "file-1",
            "open_kfid": "wk-1",
            "external_userid": "external-1",
            "origin": 3,
            "msgtype": "file",
            "file": {"media_id": "media-file", "filename": "报价单.pdf"},
        }
    )
    assert file is not None
    assert file.attachments[0].kind == "file"
    assert file.attachments[0].filename == "报价单.pdf"
    assert file.attachments[0].download_params["provider_max_bytes"] == 20 * 1024 * 1024


def test_wechat_kf_file_metadata_uses_provider_name_and_mime() -> None:
    inbound = normalize_wechat_kf_message(
        {
            "msgid": "file-1",
            "open_kfid": "wk-1",
            "external_userid": "external-1",
            "origin": 3,
            "msgtype": "file",
            "file": {"media_id": "media-file", "name": "报告.docx"},
        }
    )
    assert inbound is not None
    attachment = inbound.attachments[0]
    assert attachment.filename == "报告.docx"
    assert attachment.content_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_wechat_kf_normalizes_mixed_message() -> None:
    inbound = normalize_wechat_kf_message(
        {
            "msgid": "mixed-1",
            "open_kfid": "wk-1",
            "external_userid": "external-1",
            "origin": 3,
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "请查收"}},
                    {"msgtype": "file", "file": {"media_id": "media-file", "filename": "a.txt"}},
                ]
            },
        }
    )
    assert inbound is not None
    assert inbound.text == "请查收"
    assert [item.kind for item in inbound.attachments] == ["file"]


def test_wechat_kf_replay_restores_attachment_dataclass() -> None:
    from app.channels.service_wechat_kf_inbox import decode_replay_envelope

    inbound = decode_replay_envelope(
        {
            "schema_version": 1,
            "account": {"scope": "corp:wk-1"},
            "inbound": {
                "channel": "wechat_kf",
                "event_id": "file-1",
                "from_user_id": "external-1",
                "to_user_id": "wk-1",
                "session_id": "external-1",
                "group_id": "",
                "context_token": "wk-1",
                "text": "",
                "is_group": False,
                "raw": {},
                "account_scope": "corp:wk-1",
                "attachments": [{"media_id": "media-file", "kind": "file", "filename": "a.txt"}],
            },
        }
    )
    assert inbound.attachments[0].media_id == "media-file"


def test_wechat_kf_downloads_binary_media(monkeypatch) -> None:
    adapter = WeChatKfAdapter()
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="wechat_kf",
        config_json={"corp_id": "ww-1"},
    )
    monkeypatch.setattr(adapter._tokens, "get", lambda _binding: "token")

    class FakeResponse:
        def __init__(self):
            self.headers = {
                "content-type": "application/octet-stream",
                "content-disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.docx",
                "content-length": "7",
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            return iter([b"payload"])

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url, *, params):
            assert method == "GET"
            assert url.endswith("/media/get")
            assert params == {"access_token": "token", "media_id": "media-file"}
            return FakeResponse()

    monkeypatch.setattr("app.channels.adapters.wechat_kf.httpx.Client", lambda **_kwargs: FakeClient())
    attachment = normalize_wechat_kf_message(
        {
            "msgid": "file-1",
            "open_kfid": "wk-1",
            "external_userid": "external-1",
            "origin": 3,
            "msgtype": "file",
            "file": {"media_id": "media-file", "filename": "a.txt"},
        }
    ).attachments[0]
    assert adapter.download_media(binding, attachment) == b"payload"
    assert attachment.filename == "报告.docx"
    assert attachment.content_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_wechat_kf_download_rejects_json_error(monkeypatch) -> None:
    adapter = WeChatKfAdapter()
    binding = ChannelBinding(tenant_id="t", agent_id="a", channel="wechat_kf")
    monkeypatch.setattr(adapter._tokens, "get", lambda _binding: "token")

    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self, _size):
            return iter([json.dumps({"errcode": 40001, "errmsg": "bad media"}).encode()])

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.channels.adapters.wechat_kf.httpx.Client", lambda **_kwargs: FakeClient())
    attachment = normalize_wechat_kf_message(
        {
            "msgid": "image-1",
            "open_kfid": "wk-1",
            "external_userid": "external-1",
            "origin": 3,
            "msgtype": "image",
            "image": {"media_id": "media-image"},
        }
    ).attachments[0]
    with pytest.raises(Exception, match="bad media"):
        adapter.download_media(binding, attachment)


def test_wechat_kf_adapter_sends_text_with_stable_msgid(monkeypatch) -> None:
    adapter = WeChatKfAdapter()
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        adapter,
        "_post",
        lambda _binding, path, body: calls.append((path, body)) or {"errcode": 0},
    )
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="wechat_kf",
    )

    adapter.send(
        binding,
        {"to_user_id": "external-1", "open_kfid": "wk-1"},
        "回复内容",
        idempotency_key="message-1",
    )

    assert calls[0][0] == "/kf/send_msg"
    assert calls[0][1]["touser"] == "external-1"
    assert calls[0][1]["open_kfid"] == "wk-1"
    assert calls[0][1]["msgtype"] == "text"
    assert calls[0][1]["text"] == {"content": "回复内容"}
    assert len(calls[0][1]["msgid"]) == 32


def test_wechat_kf_text_split_obeys_utf8_byte_limit() -> None:
    chunks = _split_utf8_text("中" * 1000)
    assert len(chunks) == 2
    assert "".join(chunks) == "中" * 1000
    assert all(len(chunk.encode()) <= 2048 for chunk in chunks)


def test_wechat_kf_contact_way_returns_provider_url(monkeypatch) -> None:
    adapter = WeChatKfAdapter()
    monkeypatch.setattr(
        adapter,
        "_post",
        lambda _binding, path, body: {
            "url": f"https://work.weixin.qq.com/kf/example?scene={body['scene']}"
        },
    )
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="wechat_kf",
        config_json={"open_kfid": "wk-1"},
    )

    url = adapter.contact_way(binding, open_kfid="wk-1")

    assert url == "https://work.weixin.qq.com/kf/example?scene=staffdeck"


def test_wechat_kf_account_management(monkeypatch) -> None:
    adapter = WeChatKfAdapter()
    calls: list[tuple[str, dict]] = []

    def post(_binding, path, body):
        calls.append((path, body))
        if path == "/kf/account/list":
            return {
                "account_list": [
                    {"open_kfid": "wk-1", "name": "售前客服", "manage_privilege": True}
                ]
            }
        return {"open_kfid": "wk-created"}

    monkeypatch.setattr(adapter, "_post", post)
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="wechat_kf",
    )

    accounts = adapter.list_accounts(binding)
    created = adapter.create_account_with_avatar(binding, "新客服", "media-1")

    assert accounts[0]["open_kfid"] == "wk-1"
    assert created == "wk-created"
    assert calls[-1] == (
        "/kf/account/add",
        {"name": "新客服", "media_id": "media-1"},
    )


def test_wechat_kf_create_account_requires_media_id(monkeypatch) -> None:
    adapter = WeChatKfAdapter()
    monkeypatch.setattr(adapter, "_post", lambda *_args: {"open_kfid": "wk-created"})
    binding = ChannelBinding(tenant_id="tenant_demo", agent_id="agent_1", channel="wechat_kf")

    with pytest.raises(Exception, match="media_id"):
        adapter.create_account(binding, "客服")

    assert adapter.create_account_with_avatar(binding, "客服", "media-1") == "wk-created"


def test_wechat_kf_update_and_delete_account(monkeypatch) -> None:
    adapter = WeChatKfAdapter()
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        adapter,
        "_post",
        lambda _binding, path, body: calls.append((path, body)) or {"errcode": 0},
    )
    binding = ChannelBinding(tenant_id="tenant_demo", agent_id="agent_1", channel="wechat_kf")

    adapter.update_account(binding, "wk-1", "新名称", "media-2")
    adapter.delete_account(binding, "wk-1")

    assert calls == [
        (
            "/kf/account/update",
            {"open_kfid": "wk-1", "name": "新名称", "media_id": "media-2"},
        ),
        ("/kf/account/del", {"open_kfid": "wk-1"}),
    ]
