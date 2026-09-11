"""harness 产物桥接为 discord channel_payload 的测试(功能8富媒体 D8 生成侧补齐)。

契约要点:
- assistant_metadata.harness_artifacts 条目格式:
  {"type": "workspace_file", "task_frame_id", "path", "sha256", "size"}
  (app.harness.artifacts.publish_harness_artifacts 产出)
- 桥接仅在 binding.channel == "discord" 且无显式 channel_payload 时生效;
- 产物转为 {"files": [{"filename", "data"(base64), "content_type"}]} 载荷,
  单文件超 8MiB / 读取失败时静默跳过(降级,不抛异常)。
"""

import base64
import hashlib
import json
from typing import Any

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels.service_outbox import stage_channel_delivery
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.db.models import (
    ChannelBinding,
    ChannelDelivery,
    ChatSession,
    Message,
    Tenant,
    UIConfig,
)

# 与 DiscordAdapter._MAX_ATTACHMENT_BYTES 对齐
_DISCORD_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_base(db: Session, tmp_path) -> None:
    """种 tenant + 指向 tmp_path 的 harness 存储根(UIConfig)。"""
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        UIConfig(
            tenant_id="tenant_demo",
            sandbox_enabled=False,
            harness_storage_path=str(tmp_path),
        )
    )
    db.commit()


def _seed_binding(db: Session, *, channel: str) -> ChannelBinding:
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel=channel,
        status="active",
        external_account_key=f"{channel}:account",
    )
    db.add(binding)
    db.commit()
    return binding


def _discord_session(binding: ChannelBinding) -> ChatSession:
    return ChatSession(
        id="session_discord",
        tenant_id=binding.tenant_id,
        user_id="user_1",
        agent_id=binding.agent_id,
        channel=binding.channel,
        external_conv_id="discord_p2p_duser",
        channel_target_json={"channel_id": "channel-1"},
        channel_binding_id=binding.id,
        channel_account_key=binding.external_account_key,
    )


def _wechat_session(binding: ChannelBinding) -> ChatSession:
    return ChatSession(
        id="session_wechat",
        tenant_id=binding.tenant_id,
        user_id="user_1",
        agent_id=binding.agent_id,
        channel=binding.channel,
        external_conv_id="wechat_p2p_u1",
        channel_target_json={"to_user_id": "u1", "context_token": "ctx"},
        channel_binding_id=binding.id,
        channel_account_key=binding.external_account_key,
    )


def _write_harness_file(
    db: Session,
    chat_session: ChatSession,
    task_frame_id: str,
    relative_path: str,
    content: bytes,
) -> None:
    """按 harness_task_workspace_path 的根布局写入工作区文件。"""
    root = harness_task_workspace_path(
        tenant_id=chat_session.tenant_id,
        session_id=chat_session.id,
        task_frame_id=task_frame_id,
        db=db,
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / relative_path).write_bytes(content)


def _artifact(task_frame_id: str, relative_path: str, size: int) -> dict[str, Any]:
    return {
        "type": "workspace_file",
        "task_frame_id": task_frame_id,
        "path": relative_path,
        "sha256": "x" * 64,
        "size": size,
    }


def _assistant_message(
    chat_session: ChatSession,
    *,
    message_id: str,
    content: str = "已生成报告",
    metadata_json: dict[str, Any] | None = None,
) -> Message:
    return Message(
        id=message_id,
        tenant_id=chat_session.tenant_id,
        session_id=chat_session.id,
        role="assistant",
        content=content,
        metadata_json=metadata_json or {},
    )


def test_discord_harness_artifacts_become_files_payload(tmp_path) -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        original = "**任务报告**\n\n- 完成调研\n- 输出结论\n".encode("utf-8")
        _write_harness_file(db, chat_session, "task_frame_1", "report.md", original)
        message = _assistant_message(
            chat_session,
            message_id="msg_harness",
            metadata_json={"harness_artifacts": [_artifact("task_frame_1", "report.md", len(original))]},
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        delivery = db.exec(select(ChannelDelivery)).one()
        assert delivery.payload_json is not None
        payload = json.loads(delivery.payload_json)
        files = payload.get("files")
        assert isinstance(files, list) and len(files) == 1
        assert files[0]["filename"] == "report.md"
        assert base64.b64decode(files[0]["data"]) == original
        assert files[0]["content_type"]


def test_non_discord_channel_does_not_bridge_harness_artifacts(tmp_path) -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="wechat")
        chat_session = _wechat_session(binding)
        _write_harness_file(db, chat_session, "task_frame_1", "report.md", b"data")
        message = _assistant_message(
            chat_session,
            message_id="msg_harness",
            metadata_json={"harness_artifacts": [_artifact("task_frame_1", "report.md", 4)]},
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        delivery = db.exec(select(ChannelDelivery)).one()
        # 微信等存量渠道不做 harness 桥接,保持纯文本行为
        assert delivery.payload_json is None


def test_no_harness_artifacts_keeps_payload_json_none(tmp_path) -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        message = _assistant_message(chat_session, message_id="msg_plain", metadata_json={})
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        delivery = db.exec(select(ChannelDelivery)).one()
        assert delivery.payload_json is None


def test_oversized_harness_artifact_is_skipped(tmp_path) -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        original = b"small report"
        _write_harness_file(db, chat_session, "task_frame_1", "report.md", original)
        # 声明超 8MiB 的条目即使文件存在也跳过(Discord 附件上限)
        oversized = _artifact("task_frame_1", "big.bin", _DISCORD_MAX_ATTACHMENT_BYTES + 1)
        message = _assistant_message(
            chat_session,
            message_id="msg_harness",
            metadata_json={
                "harness_artifacts": [
                    oversized,
                    _artifact("task_frame_1", "report.md", len(original)),
                ]
            },
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        delivery = db.exec(select(ChannelDelivery)).one()
        assert delivery.payload_json is not None
        payload = json.loads(delivery.payload_json)
        assert [file["filename"] for file in payload["files"]] == ["report.md"]


def test_missing_harness_file_is_skipped_without_raising(tmp_path) -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        original = b"survivor"
        _write_harness_file(db, chat_session, "task_frame_1", "report.md", original)
        message = _assistant_message(
            chat_session,
            message_id="msg_harness",
            metadata_json={
                "harness_artifacts": [
                    _artifact("task_frame_1", "missing.md", 10),
                    "garbage-entry",  # 非 dict 条目直接忽略
                    _artifact("other_frame", "report.md", len(original)),  # 工作区不存在
                    _artifact("task_frame_1", "report.md", len(original)),
                ]
            },
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        # 缺失文件静默跳过,不抛异常,其余文件正常桥接
        stage_channel_delivery(db, chat_session, message)
        db.commit()

        delivery = db.exec(select(ChannelDelivery)).one()
        assert delivery.status == "pending"
        assert delivery.payload_json is not None
        payload = json.loads(delivery.payload_json)
        assert [file["filename"] for file in payload["files"]] == ["report.md"]


def test_explicit_channel_payload_takes_priority_over_harness_bridge(tmp_path) -> None:
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        _write_harness_file(db, chat_session, "task_frame_1", "report.md", b"data")
        explicit = {"embeds": [{"title": "显式卡片"}]}
        message = _assistant_message(
            chat_session,
            message_id="msg_harness",
            metadata_json={
                "channel_payload": explicit,
                "harness_artifacts": [_artifact("task_frame_1", "report.md", 4)],
            },
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        delivery = db.exec(select(ChannelDelivery)).one()
        # 显式 channel_payload 优先,harness 桥接不覆盖
        assert delivery.payload_json == json.dumps(explicit, ensure_ascii=False)
        assert delivery.payload_json is not None
        assert "files" not in json.loads(delivery.payload_json)


def test_explicit_channel_payload_files_are_registered_as_artifacts(tmp_path) -> None:
    """显式 channel_payload.files 附件补登记进 harness_artifacts(投递必有登记不变量)。

    契约:生成方直接写 channel_payload.files(不经过 harness 工作区)时,投递登记
    应把这些附件镜像进 harness_artifacts,使员工后续可经已发布交付物清单检索。
    """
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        original = "显式通道附件内容".encode("utf-8")
        explicit = {
            "files": [
                {
                    "filename": "explicit.md",
                    "data": base64.b64encode(original).decode("ascii"),
                    "content_type": "text/markdown",
                }
            ]
        }
        message = _assistant_message(
            chat_session,
            message_id="msg_explicit",
            metadata_json={
                "channel_payload": explicit,
                "task_frame_ids": ["task_frame_1"],
            },
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        delivery = db.exec(select(ChannelDelivery)).one()
        assert delivery.payload_json == json.dumps(explicit, ensure_ascii=False)

        refreshed = db.get(Message, message.id)
        artifacts = (refreshed.metadata_json or {}).get("harness_artifacts")
        assert isinstance(artifacts, list) and len(artifacts) == 1
        entry = artifacts[0]
        assert entry["type"] == "workspace_file"
        assert entry["path"] == "explicit.md"
        assert entry["task_frame_id"] == "task_frame_1"
        assert entry["display_name"] == "explicit.md"
        assert entry["content_type"] == "text/markdown"
        assert entry["size"] == len(original)
        assert entry["sha256"] == hashlib.sha256(original).hexdigest()
        assert entry["operation"] == "channel_delivery"
        assert entry["source"] == "channel_delivery"


def test_explicit_channel_payload_registration_is_idempotent(tmp_path) -> None:
    """显式 channel_payload 附件登记幂等:同名 path 已在 harness_artifacts 时不重复追加。"""
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        original = b"same file content"
        explicit = {
            "files": [
                {
                    "filename": "report.md",
                    "data": base64.b64encode(original).decode("ascii"),
                    "content_type": "text/markdown",
                }
            ]
        }
        message = _assistant_message(
            chat_session,
            message_id="msg_explicit_idem",
            metadata_json={
                "channel_payload": explicit,
                "task_frame_ids": ["task_frame_1"],
                "harness_artifacts": [
                    {
                        "type": "workspace_file",
                        "task_frame_id": "task_frame_1",
                        "path": "report.md",
                        "sha256": "x" * 64,
                        "size": len(original),
                    }
                ],
            },
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        refreshed = db.get(Message, message.id)
        artifacts = (refreshed.metadata_json or {}).get("harness_artifacts")
        assert isinstance(artifacts, list) and len(artifacts) == 1
        assert artifacts[0]["path"] == "report.md"


def test_explicit_channel_payload_without_files_keeps_artifacts_untouched(tmp_path) -> None:
    """embeds-only 的显式 channel_payload 不污染 harness_artifacts。"""
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        explicit = {"embeds": [{"title": "仅卡片,无附件"}]}
        message = _assistant_message(
            chat_session,
            message_id="msg_embeds",
            metadata_json={"channel_payload": explicit},
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        refreshed = db.get(Message, message.id)
        assert (refreshed.metadata_json or {}).get("harness_artifacts") in (None, [])


def test_bridge_payload_does_not_duplicate_artifacts(tmp_path) -> None:
    """桥接路径产物天然已登记,投递登记不得重复追加同名条目。"""
    engine = _test_engine()
    with Session(engine) as db:
        _seed_base(db, tmp_path)
        binding = _seed_binding(db, channel="discord")
        chat_session = _discord_session(binding)
        original = b"bridged report"
        _write_harness_file(db, chat_session, "task_frame_1", "report.md", original)
        message = _assistant_message(
            chat_session,
            message_id="msg_bridge",
            metadata_json={"harness_artifacts": [_artifact("task_frame_1", "report.md", len(original))]},
        )
        db.add(chat_session)
        db.add(message)
        db.commit()

        stage_channel_delivery(db, chat_session, message)
        db.commit()

        refreshed = db.get(Message, message.id)
        artifacts = (refreshed.metadata_json or {}).get("harness_artifacts")
        assert isinstance(artifacts, list) and len(artifacts) == 1
        assert artifacts[0]["path"] == "report.md"