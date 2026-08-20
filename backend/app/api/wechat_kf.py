from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlmodel import Session, select

from app.channels import binding_lifecycle_lock
from app.channels.adapters.base import get_channel_adapter
from app.channels.adapters.wechat_kf import (
    WeChatKfAdapter,
    normalize_wechat_kf_message,
    wechat_kf_credentials,
)
from app.channels.service_durable_inbox import StageDisposition
from app.channels.service_identity import external_account_scope
from app.channels.service_intake import wake_staged_inbound_worker
from app.channels.service_wechat_kf_inbox import stage_wechat_kf_inbound
from app.db import engine
from app.db.models import ChannelBinding, WeChatKfAccount, utc_now

router = APIRouter(prefix="/api/channels/wechat-kf", tags=["wechat-kf"])


def _callback_signature(token: str, timestamp: str, nonce: str, ciphertext: str) -> str:
    values = sorted((token, timestamp, nonce, ciphertext))
    return hashlib.sha1("".join(values).encode("utf-8")).hexdigest()


def _verify_callback(
    token: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    ciphertext: str,
) -> None:
    expected = _callback_signature(token, timestamp, nonce, ciphertext)
    if not hmac.compare_digest(expected, msg_signature):
        raise HTTPException(status_code=403, detail="微信客服回调签名无效")


def _decrypt_callback_message(ciphertext: str, aes_key: str, corp_id: str) -> str:
    try:
        key = base64.urlsafe_b64decode(aes_key + "=")
        if len(key) != 32:
            raise ValueError("invalid AES key")
        encrypted = base64.b64decode(ciphertext, validate=True)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        padding = padded[-1]
        if not 1 <= padding <= 32 or padded[-padding:] != bytes((padding,)) * padding:
            raise ValueError("invalid PKCS#7 padding")
        payload = padded[:-padding]
        content_length = struct.unpack("!I", payload[16:20])[0]
        content_end = 20 + content_length
        content = payload[20:content_end]
        receive_id = payload[content_end:].decode("utf-8")
        if receive_id != corp_id:
            raise ValueError("unexpected receive id")
        return content.decode("utf-8")
    except (ValueError, IndexError, struct.error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=403, detail="微信客服回调消息解密失败") from exc


def _callback_binding(
    binding_id: str, *, allow_pending: bool = False
) -> tuple[ChannelBinding, dict[str, str]]:
    with Session(engine) as db:
        binding = db.get(ChannelBinding, binding_id)
        allowed_status = {"active"}
        if allow_pending:
            allowed_status.add("pending")
        if not binding or binding.channel != "wechat_kf" or binding.status not in allowed_status:
            raise HTTPException(status_code=404, detail="微信客服渠道绑定不存在或未启用")
        credentials = wechat_kf_credentials(binding)
        db.expunge(binding)
    return binding, credentials


@router.get("/{binding_id}/callback")
def verify_callback_url(
    binding_id: str,
    msg_signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
    echostr: str = Query(""),
) -> Response:
    """微信客服后台配置回调 URL 时的 GET 校验入口。"""
    if not all((msg_signature, timestamp, nonce, echostr)):
        raise HTTPException(status_code=400, detail="缺少微信客服回调校验参数")
    binding, credentials = _callback_binding(binding_id, allow_pending=True)
    token = credentials.get("callback_token", "")
    aes_key = credentials.get("encoding_aes_key", "")
    corp_id = str((binding.config_json or {}).get("corp_id") or "")
    _verify_callback(token, msg_signature, timestamp, nonce, echostr)
    plaintext = _decrypt_callback_message(echostr, aes_key, corp_id)
    return Response(content=plaintext, media_type="text/plain")


def _xml_text(root: ET.Element, name: str) -> str:
    node = root.find(name)
    return str(node.text or "").strip() if node is not None else ""


def _save_account_cursor(account_id: str, cursor: str) -> None:
    with Session(engine) as db:
        account = db.get(WeChatKfAccount, account_id)
        if not account:
            return
        account.sync_cursor = cursor
        account.last_sync_at = utc_now()
        account.last_error = None
        account.updated_at = utc_now()
        db.add(account)
        binding = db.get(ChannelBinding, account.binding_id)
        if binding:
            binding.connected = True
            binding.last_connected_at = utc_now()
            binding.updated_at = utc_now()
            db.add(binding)
        db.commit()


@router.post("/{binding_id}/callback")
async def receive_callback(
    binding_id: str,
    request: Request,
    msg_signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
) -> Response:
    """验签并拉取微信客服消息，持久化后立即确认回调。"""
    if not all((msg_signature, timestamp, nonce)):
        raise HTTPException(status_code=400, detail="缺少微信客服回调参数")
    binding, credentials = _callback_binding(binding_id, allow_pending=True)
    try:
        envelope = ET.fromstring(await request.body())
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="微信客服回调 XML 无效") from exc
    ciphertext = _xml_text(envelope, "Encrypt")
    if not ciphertext:
        raise HTTPException(status_code=400, detail="微信客服回调缺少 Encrypt")
    _verify_callback(
        credentials.get("callback_token", ""),
        msg_signature,
        timestamp,
        nonce,
        ciphertext,
    )
    corp_id = str((binding.config_json or {}).get("corp_id") or "")
    plaintext = _decrypt_callback_message(
        ciphertext,
        credentials.get("encoding_aes_key", ""),
        corp_id,
    )
    try:
        event = ET.fromstring(plaintext)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="微信客服回调明文 XML 无效") from exc
    if _xml_text(event, "Event") != "kf_msg_or_event":
        return Response(content="success", media_type="text/plain")
    callback_token = _xml_text(event, "Token")
    open_kfid = _xml_text(event, "OpenKfId")
    if not callback_token or not open_kfid:
        raise HTTPException(status_code=403, detail="微信客服回调账号不匹配")

    with binding_lifecycle_lock(binding_id):
        # 并发回调必须在锁内重读游标，否则两个请求会从同一 cursor 重复拉取。
        binding, _credentials = _callback_binding(binding_id, allow_pending=True)
        with Session(engine) as db:
            account = db.exec(
                select(WeChatKfAccount).where(
                    WeChatKfAccount.binding_id == binding.id,
                    WeChatKfAccount.open_kfid == open_kfid,
                    WeChatKfAccount.status == "active",
                )
            ).first()
        if not account:
            raise HTTPException(status_code=403, detail="该客服账号尚未绑定 StaffDeck 渠道")
        adapter = get_channel_adapter("wechat_kf")
        if not isinstance(adapter, WeChatKfAdapter):
            raise HTTPException(status_code=503, detail="微信客服适配器不可用")
        cursor = str(account.sync_cursor or "")
        corp_id = str((binding.config_json or {}).get("corp_id") or "").strip()
        scope = f"{corp_id}:{open_kfid}" if corp_id else external_account_scope(None, binding)
        staged = False
        for _ in range(20):
            data = adapter.sync_messages(
                binding,
                callback_token=callback_token,
                cursor=cursor,
                open_kfid=open_kfid,
            )
            for raw in data.get("msg_list") or []:
                inbound = normalize_wechat_kf_message(raw, account_scope=scope)
                if inbound is None:
                    continue
                result = stage_wechat_kf_inbound(
                    db_engine=engine,
                    binding_id=binding.id,
                    expected_revision=binding.config_revision,
                    account_scope=scope,
                    inbound=inbound,
                )
                if result.disposition == StageDisposition.NACK:
                    raise HTTPException(status_code=503, detail="微信客服消息暂存失败")
                staged = staged or result.disposition == StageDisposition.STAGED
            next_cursor = str(data.get("next_cursor") or cursor)
            if next_cursor:
                cursor = next_cursor
                _save_account_cursor(account.id, cursor)
            if int(data.get("has_more") or 0) != 1:
                break
        else:
            raise HTTPException(status_code=503, detail="微信客服消息分页超过安全上限")
    if staged:
        wake_staged_inbound_worker()
    return Response(content="success", media_type="text/plain")
