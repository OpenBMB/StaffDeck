from __future__ import annotations

import json
import logging
from typing import Any

from app.channels.trace_streamer import TraceStreamer, _state_icon
from app.config import get_settings
from app.db.models import ChannelBinding

logger = logging.getLogger(__name__)

# 钉钉官方 AI 卡片模板的 flowStatus 取值（与 dingtalk-stream SDK 的
# AICardStatus 对齐）：1=处理中，2=输入中，3=执行完成，5=执行失败。
_FLOW_STATUS_PROCESSING = "1"
_FLOW_STATUS_INPUTING = "2"
_FLOW_STATUS_FINISHED = "3"
_FLOW_STATUS_FAILED = "5"

# 流式内容槽名：官方通用 AI Markdown 卡片的正文变量。
_CARD_CONTENT_SLOT = "msgContent"


class DingTalkTraceStreamer(TraceStreamer):
    """钉钉渠道实时执行步骤卡片流式器。

    与飞书 trace 卡片同构（生命周期、节流、SOP 紧凑展示、失败隔离见
    TraceStreamer 基类）。钉钉 AI 卡片的内容渲染机制与飞书不同，更新
    序列为（与生产验证的钉钉 AI 卡片连接器一致）：

      1. start：createAndDeliver 创建卡片（flowStatus=1 处理中）；
      2. 首次内容更新：PUT /card/instances 切换 flowStatus=2（输入态）——
         钉钉卡片需先进入输入态，流式内容才会实时渲染；
      3. 后续内容更新：PUT /card/streaming 全量推送 msgContent（isFull）；
      4. finish/abort：streaming(isFinalize=true) 关闭流式通道定格内容，
         再 PUT /card/instances 定格 flowStatus=3/5。

    卡片创建/更新/流式失败仅记日志，不影响 turn 成功与正文回复
    （sessionWebhook）。渲染产物为 cardParamMap：msgTitle=状态标题、
    msgContent=步骤 markdown（段落间空行分隔）、flowStatus=AI 卡片原生
    状态、sys_full_json_obj 控制元素顺序（与官方 SDK 一致）。
    """

    channel_name = "dingtalk"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 流式通道是否已开启（INPUTING 已切换）：仅 worker 线程读写。
        self._streaming_started = False

    def _idempotency_key(self) -> str:
        return f"dingtalk-trace:{self._binding.id}:{self._turn_id}"

    def _compact_sop_setting(self) -> bool:
        return bool(get_settings().channel_dingtalk_trace_compact_sop)

    # ---- 卡片操作（worker 线程） ----

    def _do_patch_card(self, lines: list[dict] | None, *, state: str, force: bool) -> None:
        if not self._message_id:
            return
        try:
            adapter = self._ensure_adapter()
            if lines is None:
                with self._lock:
                    lines = list(self._lines)
            card = self._render_card(lines=lines, state=state)
            if state == "running":
                if not self._streaming_started:
                    # 先切入输入态：流式内容才会实时渲染。转换失败时保持
                    # 未开启状态，下个事件（节流 1s 后）重试整个序列。
                    adapter.update_card(
                        self._binding,
                        self._message_id,
                        {**card, "flowStatus": _FLOW_STATUS_INPUTING},
                    )
                    self._streaming_started = True
                adapter.stream_card(
                    self._binding, self._message_id, _CARD_CONTENT_SLOT, card[_CARD_CONTENT_SLOT]
                )
                return
            # 结束序列：先关闭流式通道（确保最终内容渲染），再定格状态。
            # 卡片从未进入流式（极短 turn / 建卡前已 finish）时直接定格，
            # msgContent 随 finalize 语义的整卡更新一并渲染。定格失败会让
            # 卡片永远停在"输入中"，两步均走瞬时重试。
            if self._streaming_started:
                self._streaming_started = False
                try:
                    self._retry_call(
                        lambda: adapter.stream_card(
                            self._binding,
                            self._message_id,
                            _CARD_CONTENT_SLOT,
                            card[_CARD_CONTENT_SLOT],
                            finalize=True,
                            failed=state == "failed",
                        ),
                        description="流式通道关闭",
                    )
                except Exception:
                    # 通道关闭失败不阻断状态定格：update_card 仍带最终内容
                    logger.exception(
                        "钉钉 trace 流式通道关闭失败 binding=%s card_id=%s",
                        self._binding.id,
                        self._message_id,
                    )
            self._retry_call(
                lambda: adapter.update_card(self._binding, self._message_id, card),
                description="卡片定格",
            )
        except Exception:
            logger.exception(
                "钉钉 trace 卡片更新失败 binding=%s card_id=%s",
                self._binding.id,
                self._message_id,
            )

    # ---- 卡片渲染 ----

    def _render_card(
        self,
        *,
        lines: list[dict] | None = None,
        state: str = "running",
    ) -> dict[str, Any]:
        title = "正在思考…"
        flow_status = _FLOW_STATUS_PROCESSING
        if state == "completed":
            title = "执行完成"
            flow_status = _FLOW_STATUS_FINISHED
        elif state == "failed":
            title = "执行失败"
            flow_status = _FLOW_STATUS_FAILED

        display_lines = lines if lines is not None else []
        if self._compact_sop and self._sop_started:
            display_lines = self._compact_lines(display_lines)
        markdown = _lines_to_markdown(display_lines)
        return {
            "msgTitle": title,
            "msgContent": markdown,
            "flowStatus": flow_status,
            "sys_full_json_obj": json.dumps(
                {"order": ["msgTitle", "msgContent"]}, ensure_ascii=False
            ),
        }


def _lines_to_markdown(lines: list[dict]) -> str:
    """把 trace 行渲染为钉钉卡片 markdown。

    步骤之间以空行分隔：钉钉 markdown 的单换行不保证断行（标准 markdown
    会折叠段内换行），空行分段是各端稳定渲染的最小公约数。
    """
    if not lines:
        return "等待执行步骤…"
    blocks: list[str] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        detail = str(line.get("detail") or "").strip()
        state = str(line.get("state") or "").strip()
        icon = _state_icon(state)
        parts = [f"{icon} {text}" if icon else text]
        if detail:
            parts.append(detail)
        block = "\n\n".join(part for part in parts if part)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def is_dingtalk_trace_enabled(binding: ChannelBinding | None) -> bool:
    if not binding or binding.channel != "dingtalk":
        return False
    if not get_settings().channel_dingtalk_trace_enabled:
        return False
    config = binding.config_json or {}
    return not (isinstance(config, dict) and config.get("trace_enabled") is False)
