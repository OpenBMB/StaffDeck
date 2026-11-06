from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

# 触发 feishu/钉钉适配器注册到全局 _adapters,便于测试中保存/恢复 previous
import app.channels.adapters.dingtalk
import app.channels.adapters.feishu  # noqa: F401

# 通过 app.api.chat 完整加载 app.core / app.session 依赖链,
# 之后才能正常 import app.session.attachment_store / app.session.attachments
from app.api.chat import _user_message_metadata  # noqa: F401
from app.channels.adapters.base import (
    ChannelInbound,
    ChannelInboundAttachment,
    get_channel_adapter,
    register_channel_adapter,
    stream_download_with_limit,
)
from app.channels.attachment_bridge import (
    MAX_CHANNEL_MEDIA_BYTES,
    inbound_attachments_to_chat,
)
from app.channels.crypto import encrypt_channel_secret
from app.channels.media import detect_image_media_type, filename_with_extension
from app.db.models import ChannelBinding
from app.session.session_schema import ChatAttachmentRead


class _FakeAdapter:
    """模拟适配器:download_media 返回预设字节,用于隔离 attachment_bridge 测试。"""

    def __init__(self, download_result: bytes | Exception):
        self._download_result = download_result
        self.download_calls: list[tuple[ChannelBinding, ChannelInboundAttachment]] = []

    def download_media(self, binding, attachment, *, max_bytes=0):
        self.download_calls.append((binding, attachment))
        if isinstance(self._download_result, Exception):
            raise self._download_result
        return self._download_result


def _binding() -> ChannelBinding:
    return ChannelBinding(
        id="chan_test",
        tenant_id="tenant_a",
        agent_id="agent_a",
        channel="feishu",
        status="active",
        config_json={"app_id": "cli_app"},
        credentials_enc=encrypt_channel_secret("secret"),
        config_revision=1,
    )


def _inbound(attachments: list[ChannelInboundAttachment]) -> ChannelInbound:
    return ChannelInbound(
        channel="feishu",
        event_id="evt_1",
        from_user_id="ou_sender",
        to_user_id="ou_bot",
        session_id="oc_chat",
        group_id="",
        context_token="om_msg",
        text="",
        is_group=False,
        raw={},
        attachments=attachments,
    )


def _staged_attachment(filename: str = "img.jpg", content_type: str = "image/jpeg") -> ChatAttachmentRead:
    return ChatAttachmentRead(
        id="file_1",
        filename=filename,
        content_type=content_type,
        size=4,
        kind="image",
        data_url=f"data:{content_type};base64,AAAA",
        sha256="abc123",
        sandbox_path="/workspace/attachments/file_1-img.jpg",
    )


def test_channel_inbound_attachments_defaults_to_empty_list() -> None:
    inbound = ChannelInbound(
        channel="feishu",
        event_id="e",
        from_user_id="u",
        to_user_id="b",
        session_id="s",
        group_id="",
        context_token="c",
        text="hi",
        is_group=False,
        raw={},
    )
    assert inbound.attachments == []
    # 独立实例,不共享默认值
    assert inbound.attachments is not ChannelInbound(
        channel="feishu",
        event_id="e2",
        from_user_id="u",
        to_user_id="b",
        session_id="s",
        group_id="",
        context_token="c",
        text="hi",
        is_group=False,
        raw={},
    ).attachments


def test_channel_inbound_attachment_construction() -> None:
    att = ChannelInboundAttachment(
        media_id="img_key",
        kind="image",
        filename="x.jpg",
        content_type="image/jpeg",
        size=10,
        download_params={"file_key": "img_key"},
    )
    assert att.media_id == "img_key"
    assert att.kind == "image"
    assert att.download_params["file_key"] == "img_key"

    # 默认 download_params 为独立空字典
    default_att = ChannelInboundAttachment(media_id="m", kind="file")
    assert default_att.download_params == {}
    assert default_att.filename == ""
    assert default_att.content_type == ""
    assert default_att.size == 0


def test_bridge_returns_empty_when_adapter_has_no_download_media() -> None:
    """适配器未实现 download_media 时返回空列表,不阻塞。"""
    adapter = SimpleNamespace()  # 没有 download_media 属性
    previous = get_channel_adapter("feishu")
    register_channel_adapter("feishu", adapter)
    try:
        inbound = _inbound([ChannelInboundAttachment(media_id="m", kind="image")])
        result = inbound_attachments_to_chat(
            _binding(),
            inbound,
            db_engine=None,
            tenant_id="t",
            user_id="u",
        )
        assert result == []
    finally:
        register_channel_adapter("feishu", previous)


def test_bridge_downloads_and_stages_attachments() -> None:
    """完整链路:download_media -> parse_chat_attachment -> stage_chat_attachment。"""
    image = b"\x89PNG\r\n\x1a\nimage-data"
    fake_adapter = _FakeAdapter(image)
    previous = get_channel_adapter("feishu")
    register_channel_adapter("feishu", fake_adapter)
    try:
        attachment = ChannelInboundAttachment(
            media_id="img_v3_001",
            kind="image",
            filename="img_v3_001.jpg",
            content_type="image/jpeg",
            download_params={"file_key": "img_v3_001", "type": "image", "message_id": "om_1"},
        )
        inbound = _inbound([attachment])

        staged = _staged_attachment(filename="img_v3_001.jpg")
        with (
            patch(
                "app.session.attachments.parse_chat_attachment",
                return_value=staged,
            ) as mock_parse,
            patch(
                "app.session.attachment_store.stage_chat_attachment",
                return_value=staged.model_copy(
                    update={"sha256": "real_sha", "sandbox_path": "/ws/x"}
                ),
            ) as mock_stage,
        ):
            results = inbound_attachments_to_chat(
                _binding(),
                inbound,
                db_engine=None,
                tenant_id="tenant_a",
                user_id="user_1",
            )

        assert len(results) == 1
        assert results[0].sha256 == "real_sha"
        assert results[0].sandbox_path == "/ws/x"
        # download_media 被调用一次,参数正确
        assert len(fake_adapter.download_calls) == 1
        assert fake_adapter.download_calls[0][1].media_id == "img_v3_001"
        # parse_chat_attachment 收到原始字节和文件名
        mock_parse.assert_called_once_with("img_v3_001.png", "image/png", image)
        # stage_chat_attachment 收到 attachment + 字节 + tenant/user
        mock_stage.assert_called_once()
        stage_kwargs = mock_stage.call_args.kwargs
        assert stage_kwargs["tenant_id"] == "tenant_a"
        assert stage_kwargs["user_id"] == "user_1"
    finally:
        register_channel_adapter("feishu", previous)


def test_bridge_skips_empty_download_result() -> None:
    fake_adapter = _FakeAdapter(b"")
    previous = get_channel_adapter("feishu")
    register_channel_adapter("feishu", fake_adapter)
    try:
        inbound = _inbound([ChannelInboundAttachment(media_id="m", kind="image")])
        results = inbound_attachments_to_chat(
            _binding(), inbound, db_engine=None, tenant_id="t", user_id="u"
        )
        assert results == []
    finally:
        register_channel_adapter("feishu", previous)


def test_bridge_skips_oversized_attachment() -> None:
    """超过 MAX_CHANNEL_MEDIA_BYTES 的附件被跳过。"""
    oversized = b"x" * (MAX_CHANNEL_MEDIA_BYTES + 1)

    class OversizedAdapter:
        def download_media(self, binding, attachment, *, max_bytes=0):
            if max_bytes and len(oversized) > max_bytes:
                raise ValueError(f"下载内容超过上限 {max_bytes} bytes")
            return oversized

    fake_adapter = OversizedAdapter()
    previous = get_channel_adapter("feishu")
    register_channel_adapter("feishu", fake_adapter)
    try:
        inbound = _inbound([ChannelInboundAttachment(media_id="big", kind="file")])
        results = inbound_attachments_to_chat(
            _binding(), inbound, db_engine=None, tenant_id="t", user_id="u"
        )
        assert results == []
    finally:
        register_channel_adapter("feishu", previous)


def test_bridge_continues_on_single_attachment_failure() -> None:
    """单个附件下载异常不影响其他附件。"""
    good = b"\x89PNG\r\n\x1a\nimage-data"
    call_count = {"n": 0}

    class MixedAdapter:
        def download_media(self, binding, attachment, *, max_bytes=0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("network down")
            return good

    previous = get_channel_adapter("feishu")
    register_channel_adapter("feishu", MixedAdapter())
    try:
        inbound = _inbound([
            ChannelInboundAttachment(media_id="bad", kind="image"),
            ChannelInboundAttachment(media_id="good", kind="image", filename="g.jpg"),
        ])
        staged = _staged_attachment(filename="g.jpg")
        with (
            patch(
                "app.session.attachments.parse_chat_attachment",
                return_value=staged,
            ),
            patch(
                "app.session.attachment_store.stage_chat_attachment",
                return_value=staged.model_copy(update={"sha256": "sha"}),
            ),
        ):
            results = inbound_attachments_to_chat(
                _binding(), inbound, db_engine=None, tenant_id="t", user_id="u"
            )
        # 第一个失败被吞,第二个成功
        assert len(results) == 1
        assert call_count["n"] == 2
    finally:
        register_channel_adapter("feishu", previous)


def test_bridge_passes_empty_content_type_as_none() -> None:
    """att.content_type 为空字符串时传 None 给 parse_chat_attachment(让其自动推断)。"""
    fake_adapter = _FakeAdapter(b"some bytes")
    previous = get_channel_adapter("feishu")
    register_channel_adapter("feishu", fake_adapter)
    try:
        inbound = _inbound([
            ChannelInboundAttachment(
                media_id="f_key", kind="file", filename="report.pdf", content_type=""
            )
        ])
        staged = _staged_attachment(filename="report.pdf", content_type="application/pdf")
        with (
            patch(
                "app.session.attachments.parse_chat_attachment",
                return_value=staged,
            ) as mock_parse,
            patch(
                "app.session.attachment_store.stage_chat_attachment",
                return_value=staged,
            ),
        ):
            inbound_attachments_to_chat(
                _binding(), inbound, db_engine=None, tenant_id="t", user_id="u"
            )
        # content_type="" -> extension-based MIME inference
        args, _kwargs = mock_parse.call_args
        assert args[0] == "report.pdf"
        assert args[1] == "application/pdf"
        assert args[2] == b"some bytes"
    finally:
        register_channel_adapter("feishu", previous)


def test_detect_image_media_type_recognizes_common_formats() -> None:
    """magic bytes 签名能识别 PNG/JPEG/GIF/WebP/BMP。"""
    assert detect_image_media_type(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20) == (
        "image/png",
        ".png",
    )
    assert detect_image_media_type(b"\xff\xd8\xff\xe0data\xff\xd9") == (
        "image/jpeg",
        ".jpg",
    )
    assert detect_image_media_type(b"GIF89a" + b"\x00" * 20) == ("image/gif", ".gif")
    assert detect_image_media_type(
        b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 20
    ) == ("image/webp", ".webp")
    assert detect_image_media_type(b"BM" + b"\x00" * 20) == ("image/bmp", ".bmp")


def test_detect_image_media_type_rejects_non_image_data() -> None:
    """非图片字节或太短的数据返回 None。"""
    assert detect_image_media_type(b"") is None
    assert detect_image_media_type(b"short") is None
    assert detect_image_media_type(b"RIFF" + b"\x00" * 4 + b"XXXX" + b"\x00" * 20) is None


def test_filename_with_extension_replaces_or_adds_extension() -> None:
    assert filename_with_extension("img_v3_001", ".png") == "img_v3_001.png"
    assert filename_with_extension("photo.jpg", ".png") == "photo.png"


def test_stream_download_with_limit_enforces_content_length() -> None:
    """Content-Length 超限时立即中止,不读取响应体。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "100"},
            content=b"x" * 100,
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    try:
        with pytest.raises(ValueError, match="超过上限"):
            stream_download_with_limit(client, "GET", "https://x/test", max_bytes=50)
    finally:
        client.close()


def test_stream_download_with_limit_enforces_streamed_body() -> None:
    """无 Content-Length 时流式累计读取超限即中止。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 200)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    try:
        with pytest.raises(ValueError, match="超过上限"):
            stream_download_with_limit(client, "GET", "https://x/test", max_bytes=100)
    finally:
        client.close()


def test_stream_download_with_limit_returns_body_when_under_limit() -> None:
    """未超限时正常返回 body。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello world")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    try:
        status, data = stream_download_with_limit(
            client, "GET", "https://x/test", max_bytes=100,
        )
        assert status == 200
        assert data == b"hello world"
    finally:
        client.close()
