from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
from pathlib import Path

from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

from app.channels.adapters.base import ChannelInbound, ChannelInboundAttachment
from app.channels.crypto import decrypt_channel_secret
from app.channels.service_feishu_inbox import StageDisposition, stage_feishu_inbound
from app.db.models import ChannelBinding
from feishu_connector_worker import SDK_CONTRACT_VERSION


logger = logging.getLogger(__name__)


def _text_content(message) -> str:
    try:
        parsed = json.loads(str(message.content or "{}"))
    except (TypeError, ValueError):
        return ""
    return str(parsed.get("text") or "").strip() if isinstance(parsed, dict) else ""


def _image_file_content(message) -> dict[str, str]:
    """从 message.content JSON 中提取 image_key/file_key。

    飞书 image 消息 content 形如: {"image_key": "img_v3_xxxx"}
    飞书 file 消息 content 形如: {"file_key": "file_v3_xxxx", "file_name": "xxx.pdf"}
    """
    try:
        parsed = json.loads(str(message.content or "{}"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _extract_feishu_attachments(message) -> list[ChannelInboundAttachment]:
    """从飞书 message 提取图片/文件附件。

    image 消息使用 image_key,file 消息使用 file_key + file_name。
    message_id 写入 download_params,供 download_media 拼接资源下载 URL。
    """
    message_type = str(message.message_type or "").strip().lower()
    content = _image_file_content(message)
    if not content:
        return []
    message_id = str(message.message_id or "").strip()
    attachments: list[ChannelInboundAttachment] = []
    if message_type == "image":
        image_key = str(content.get("image_key") or "").strip()
        if image_key:
            attachments.append(
                ChannelInboundAttachment(
                    media_id=image_key,
                    kind="image",
                    filename=image_key,
                    content_type="",
                    download_params={
                        "file_key": image_key,
                        "type": "image",
                        "message_id": message_id,
                    },
                )
            )
    elif message_type == "file":
        file_key = str(content.get("file_key") or "").strip()
        file_name = str(content.get("file_name") or "").strip()
        if file_key:
            attachments.append(
                ChannelInboundAttachment(
                    media_id=file_key,
                    kind="file",
                    filename=file_name or file_key,
                    content_type="",
                    download_params={
                        "file_key": file_key,
                        "type": "file",
                        "message_id": message_id,
                    },
                )
            )
    return attachments


def _normalize_event(event, *, bot_open_id: str) -> tuple[ChannelInbound, dict] | None:
    header = getattr(event, "header", None)
    body = getattr(event, "event", None)
    message = getattr(body, "message", None)
    sender = getattr(body, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    if not header or not message or not sender or not sender_id:
        return None
    app_id = str(header.app_id or "").strip()
    tenant_key = str(header.tenant_key or "").strip()
    message_id = str(message.message_id or "").strip()
    open_id = str(sender_id.open_id or "").strip()
    sender_type = str(sender.sender_type or "").strip().lower()
    chat_id = str(message.chat_id or "").strip()
    chat_type = str(message.chat_type or "").strip().lower()
    message_type = str(message.message_type or "").strip().lower()
    # 放宽 message_type 过滤:允许 text / image / file
    if (
        message_type not in {"text", "image", "file"}
        or not app_id
        or not tenant_key
        or not message_id
        or not open_id
        or sender_type in {"app", "bot"}
        or open_id == bot_open_id
    ):
        return None

    # 提取图片/文件附件(image/file 消息)
    attachments = _extract_feishu_attachments(message)

    # 文本只在 text 类型时提取
    text = ""
    if message_type == "text":
        text = _text_content(message)
    if not text and not attachments:
        return None

    is_group = chat_type != "p2p"
    mentions = list(message.mentions or [])
    if is_group:
        if not chat_id:
            return None
        bot_mentions = [
            mention
            for mention in mentions
            if str(getattr(getattr(mention, "id", None), "open_id", "") or "")
            == bot_open_id
        ]
        if not bot_mentions and not attachments:
            return None
        for mention in bot_mentions:
            key = str(getattr(mention, "key", "") or "")
            if key:
                text = text.replace(key, " ")
        text = " ".join(text.split())
        if not text and not attachments:
            return None

    thread_id = str(message.thread_id or "").strip()
    conv_key = (
        f"{chat_id}:thread:{thread_id}"
        if is_group and thread_id
        else chat_id or open_id
    )
    inbound = ChannelInbound(
        channel="feishu",
        event_id=message_id,
        from_user_id=open_id,
        to_user_id=bot_open_id,
        session_id=conv_key,
        group_id=conv_key if is_group else "",
        context_token=message_id,
        text=text,
        is_group=is_group,
        raw={
            "header": {"app_id": app_id, "tenant_key": tenant_key},
            "message": {"message_id": message_id, "chat_id": chat_id},
        },
        sender_name="",
        attachments=attachments,
    )
    target = {
        "message_id": message_id,
        "reply_in_thread": bool(thread_id),
        "receive_id_type": "chat_id" if is_group else "open_id",
        "receive_id": chat_id if is_group else open_id,
    }
    return inbound, target


def _build_event_dispatcher(handler_class, receive):
    def ignore_lifecycle_event(_event) -> None:
        return None

    return (
        handler_class.builder("", "")
        .register_p2_im_message_receive_v1(receive)
        .register_p2_im_chat_member_bot_added_v1(ignore_lifecycle_event)
        .register_p2_im_chat_member_bot_deleted_v1(ignore_lifecycle_event)
        .register_p2_customized_event(
            "im.chat.access_event.bot_p2p_chat_entered_v1",
            ignore_lifecycle_event,
        )
        .build()
    )


def run_feishu_runtime(spec, control, watchdog) -> None:
    if importlib.metadata.version("lark-channel-sdk") != SDK_CONTRACT_VERSION:
        raise RuntimeError(f"lark-channel-sdk must be exactly {SDK_CONTRACT_VERSION}")
    database_path = Path(spec.database_path).expanduser().resolve()
    stage_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 0.5},
        poolclass=NullPool,
    )
    with Session(stage_engine) as db:
        binding = db.get(ChannelBinding, spec.binding_id)
        if (
            not binding
            or binding.channel != "feishu"
            or binding.status != "active"
            or binding.config_revision != spec.config_revision
            or not binding.credentials_enc
        ):
            raise RuntimeError("Feishu binding failed startup fence")
        app_id = str((binding.config_json or {}).get("app_id") or "").strip()
        bot_open_id = str((binding.config_json or {}).get("bot_open_id") or "").strip()
        app_secret = decrypt_channel_secret(binding.credentials_enc)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    from lark_channel import EventDispatcherHandler
    from lark_channel.ws.client import Client as WsClient
    from lark_channel.ws.client import _get_by_key
    from lark_channel.ws.const import HEADER_MESSAGE_ID, HEADER_TRACE_ID

    def receive(event) -> None:
        normalized = _normalize_event(event, bot_open_id=bot_open_id)
        if normalized is None:
            return
        inbound, target = normalized
        result = stage_feishu_inbound(
            db_engine=stage_engine,
            binding_id=spec.binding_id,
            expected_revision=spec.config_revision,
            event_app_id=str(event.header.app_id or ""),
            tenant_key=str(event.header.tenant_key or ""),
            inbound=inbound,
            target=target,
        )
        if result.disposition is StageDisposition.NACK:
            raise RuntimeError(result.error_code or "Feishu inbox staging failed")
        if result.disposition is StageDisposition.STAGED:
            control.emit("INBOX_STAGED", event_pk=result.event_pk)

    dispatcher = _build_event_dispatcher(EventDispatcherHandler, receive)

    class ProductionClient(WsClient):
        async def _connect(self) -> None:
            await super()._connect()
            control.emit("CONNECTED")

        async def _handle_data_frame(self, frame) -> None:
            token = ":".join(
                (
                    _get_by_key(frame.headers, HEADER_MESSAGE_ID),
                    _get_by_key(frame.headers, HEADER_TRACE_ID),
                    str(id(frame)),
                )
            )
            watchdog.arm(token)
            await super()._handle_data_frame(frame)
            watchdog.disarm(token)

        async def _disconnect_and_reconnect(self, *, expected_conn=None):
            control.emit("DISCONNECTED")
            return await super()._disconnect_and_reconnect(expected_conn=expected_conn)

    client = ProductionClient(app_id, app_secret, event_handler=dispatcher)

    def request_stop() -> None:
        async def shutdown() -> None:
            client._auto_reconnect = False
            try:
                await client._disconnect()
            finally:
                loop.stop()

        coroutine = shutdown()
        try:
            asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()

    control.add_stop_callback(request_stop)
    try:
        client.start()
    except RuntimeError as exc:
        if not control.stop_requested.is_set() or "Event loop stopped" not in str(exc):
            raise
    finally:
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        if tasks and not loop.is_closed():
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        stage_engine.dispose()
