from __future__ import annotations

from typing import Any

from app.channels.trace_streamer import TraceStreamer, _state_icon
from app.config import get_settings
from app.db.models import ChannelBinding


class FeishuTraceStreamer(TraceStreamer):
    """飞书渠道实时执行步骤卡片流式器。

    生命周期、线程模型与失败隔离见 TraceStreamer 基类；本类只负责
    飞书卡片的渲染（交互式卡片 JSON）与幂等键。卡片创建走
    POST /im/v1/messages/{message_id}/reply（msg_type=interactive，
    uuid 幂等），更新走 PATCH /im/v1/messages/{message_id} 全量替换。
    """

    channel_name = "feishu"

    def _idempotency_key(self) -> str:
        return f"feishu-trace:{self._binding.id}:{self._turn_id}"

    def _compact_sop_setting(self) -> bool:
        return bool(get_settings().channel_feishu_trace_compact_sop)

    # ---- 卡片渲染 ----

    def _render_card(
        self,
        *,
        lines: list[dict] | None = None,
        state: str = "running",
    ) -> dict[str, Any]:
        header_title = "正在思考…"
        header_template = "blue"
        if state == "completed":
            header_title = "执行完成"
            header_template = "green"
        elif state == "failed":
            header_title = "执行失败"
            header_template = "red"

        elements: list[dict[str, Any]] = []
        display_lines = lines if lines is not None else []
        if self._compact_sop and self._sop_started:
            display_lines = self._compact_lines(display_lines)
        for line in display_lines:
            elements.append(_line_to_card_element(line))
        if not display_lines:
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": "等待执行步骤…"}})

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": header_template,
            },
            "elements": elements,
        }


def _line_to_card_element(line: dict) -> dict[str, Any]:
    text = str(line.get("text") or "").strip()
    detail = str(line.get("detail") or "").strip()
    state = str(line.get("state") or "").strip()
    icon = _state_icon(state)
    content_parts = [f"{icon} {text}" if icon else text]
    if detail:
        content_parts.append(detail)
    content = "\n".join(part for part in content_parts if part)
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def is_feishu_trace_enabled(binding: ChannelBinding | None) -> bool:
    if not binding or binding.channel != "feishu":
        return False
    if not get_settings().channel_feishu_trace_enabled:
        return False
    config = binding.config_json or {}
    return not (isinstance(config, dict) and config.get("trace_enabled") is False)
