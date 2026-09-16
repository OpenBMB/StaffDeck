"""转人工通知的统一内容构建。

网页收件箱(api.chat 的 human_handoff_read)与渠道私聊通知
(channels.service_outbox 的 notify_handoff_assignee)共用同一份内容构建,
保证两端看到的信息一致:标题(SOP·触发节点)、提问人、未配置处理人说明、
自进入 SOP 起的对话窗口。渠道侧在此基础上做文本渲染(字数预算与回复指引),
网页侧直接消费结构化字段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlmodel import Session, select

from app.channels.service_identity import external_account_scope
from app.core.human_handoff_service import HumanHandoffService
from app.db.models import (
    AgentEvent,
    ChannelBinding,
    ChannelIdentity,
    ChatSession,
    HumanHandoffRequest,
    Message,
    Skill,
    User,
)

# handoff 通知文本预算:整条不超过 1800 字(在适配器 2000 字拆分阈值内留余量,
# 超限拆多条会让处理人引用回复时只有末条消息 id 可关联,破坏回复匹配);
# 单条会话消息超 400 字在句末边界截断。仅渠道文本渲染受限,网页展示全文。
HANDOFF_NOTICE_TOTAL_LIMIT = 1800
HANDOFF_NOTICE_MESSAGE_LIMIT = 400
HANDOFF_NOTICE_SEPARATOR = "────────────────"
# 飞书支持引用回复,通知尾部给引用回复指引;其他渠道(企微/微信客服等)没有
# 可靠的引用消息 ID,尾部改为提示处理人带 handoff ID 显式回复。
HANDOFF_NOTICE_QUOTE_REPLY_FOOTER = (
    "如需答复，请直接回复本条消息（引用后输入答复内容）；也可发送 /回复反馈 <答复内容>。"
)
HANDOFF_NOTICE_ROLE_LABELS = {"user": "用户", "assistant": "助手"}


def _handoff_notice_footer(channel: str | None, handoff_id: str) -> str:
    """渠道对应的回复指引:feishu 引用回复,其余渠道带 handoff ID 精确回复。"""
    if channel == "feishu" or not channel:
        return HANDOFF_NOTICE_QUOTE_REPLY_FOOTER
    return f"请发送 /回复反馈 {handoff_id} <答复内容> 精确回复此请求。"


@dataclass
class HandoffNoticeContent:
    """网页收件箱与渠道通知共享的转人工通知内容。"""

    # "法律咨询·转交真人法务":SOP 显示名 + 触发节点名,任一缺失时省略该段。
    title: str
    # 提问人显示名:渠道身份 display_name,回退 User.display_name。
    inquirer: str
    # 未命中显式处理人配置时对处理人的说明,如
    # "由于没有配置处理人，已经转接给Administrator。";已配置时为空。
    assignee_notice: str
    # 对话窗口是否按 SOP 入口截取(查不到入口事件时回退完整会话)。
    scoped: bool
    # "进入 SOP → 转人工"的会话窗口,元素为 (role, content)。
    entries: list[tuple[str, str]]
    # 无任何会话消息时的兜底文案(取 handoff.pending_question)。
    fallback_question: str


def resolve_handoff_skill_name(db: Session, handoff: HumanHandoffRequest) -> str:
    """转人工通知标题里的 SOP 显示名。

    handoff.trigger_skill_id 存的是 Skill.skill_id(会话 active_skill_id 同源),
    据此查 Skill.name;查不到(如会话无 SOP 或 SOP 已删)返回空。
    """
    skill_id = str(handoff.trigger_skill_id or "").strip()
    if not skill_id:
        return ""
    row = db.exec(
        select(Skill).where(
            Skill.tenant_id == handoff.tenant_id, Skill.skill_id == skill_id
        )
    ).first()
    return str(row.name or "").strip() if row else ""


def _resolve_session_binding(
    db: Session, session: ChatSession | None
) -> ChannelBinding | None:
    """会话所属渠道绑定:提问人身份按会话发生地的渠道/scope 解析。"""
    if session is None or not session.channel_binding_id:
        return None
    binding = db.get(ChannelBinding, session.channel_binding_id)
    if binding and binding.tenant_id == session.tenant_id:
        return binding
    return None


def _resolve_inquirer_display_name(
    db: Session,
    session: ChatSession,
    binding: ChannelBinding | None,
) -> str:
    """查找提问人显示名:优先渠道身份 display_name,回退 User.display_name。"""
    if not session.user_id:
        return ""
    if binding is not None:
        scope = external_account_scope(db, binding)
        identity = db.exec(
            select(ChannelIdentity).where(
                ChannelIdentity.staffdeck_user_id == session.user_id,
                ChannelIdentity.channel == binding.channel,
                ChannelIdentity.external_account_scope == scope,
            )
        ).first()
        if identity and identity.display_name:
            return identity.display_name.strip()
    user = db.get(User, session.user_id)
    if user:
        return str(user.display_name or user.username or "").strip()
    return ""


def load_handoff_conversation(
    db: Session,
    handoff: HumanHandoffRequest,
    session: ChatSession | None,
) -> tuple[list[tuple[str, str]], bool]:
    """加载"进入 SOP → 转人工"的会话窗口,返回 ((role, content), 是否按 SOP 入口截取)。

    入口边界:会话内最近一条 to_skill_id 匹配 trigger_skill_id 的
    skill_started/skill_resumed 运行时事件。触发 SOP 的用户消息发生在事件之前,
    窗口起点回退到事件前最近一条用户消息,保证"用户最初触发 SOP 的消息 →
    转人工回复"整段可见。查不到入口事件(历史数据/无 SOP)回退完整会话记录。

    转人工回复在渠道通知构建时尚未落库(harness_v2 在 _finalize_turn 之前创建
    handoff),用 metadata.step_reply 补末条助手消息;网页读取时回复已落库且可能
    追加了"未配置处理人"说明,归一化回 step_reply,保证两端内容一致。
    """
    if session is None:
        return [], False
    skill_id = str(handoff.trigger_skill_id or "").strip()
    start_at = None
    scoped = False
    if skill_id:
        events = db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == handoff.tenant_id,
                AgentEvent.session_id == handoff.session_id,
                AgentEvent.event_type.in_(("skill_started", "skill_resumed")),
            )
            .order_by(AgentEvent.created_at.desc())
            .limit(20)
        ).all()
        entry = next(
            (
                row
                for row in events
                if str((row.payload_json or {}).get("to_skill_id") or "").strip()
                == skill_id
            ),
            None,
        )
        if entry is not None:
            scoped = True
            trigger = db.exec(
                select(Message)
                .where(
                    Message.session_id == handoff.session_id,
                    Message.role == "user",
                    Message.created_at <= entry.created_at,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            ).first()
            start_at = trigger.created_at if trigger else entry.created_at
    query = select(Message).where(Message.session_id == handoff.session_id)
    if start_at is not None:
        query = query.where(Message.created_at >= start_at)
    rows = db.exec(query.order_by(Message.created_at, Message.id)).all()
    entries = [
        (row.role, re.sub(r"\s+", " ", row.content or "").strip())
        for row in rows
    ]
    entries = [(role, content) for role, content in entries if content]
    metadata = handoff.metadata_json if isinstance(handoff.metadata_json, dict) else {}
    step_reply = re.sub(r"\s+", " ", str(metadata.get("step_reply") or "")).strip()
    if step_reply:
        if entries and entries[-1][0] == "assistant" and step_reply in entries[-1][1]:
            entries[-1] = ("assistant", step_reply)
        elif not entries or entries[-1][0] != "assistant":
            entries.append(("assistant", step_reply))
    return entries, scoped


def clip_handoff_message(content: str) -> str:
    """单条会话消息超长时在句末边界截断,找不到边界则硬切(仅渠道文本使用)。"""
    limit = HANDOFF_NOTICE_MESSAGE_LIMIT
    if len(content) <= limit:
        return content
    clipped = content[:limit]
    for boundary in ("。", "！", "？", "；", "，", " "):
        index = clipped.rfind(boundary)
        if index >= limit // 2:
            return clipped[: index + 1].rstrip() + "…"
    return clipped.rstrip() + "…"


def build_handoff_notice_content(
    db: Session, handoff: HumanHandoffRequest
) -> HandoffNoticeContent:
    """构建网页收件箱与渠道通知共用的转人工通知内容。

    assignee_notice 与用户侧会话回复里的说明同源
    (HumanHandoffService.unconfigured_assignee_notice):命中渠道默认处理人
    视为已配置,回退到数字员工负责人/租户管理员/人工队列时向处理人说明
    实际转接对象。
    """
    metadata = handoff.metadata_json if isinstance(handoff.metadata_json, dict) else {}
    step = metadata.get("step") if isinstance(metadata.get("step"), dict) else {}
    step_name = str(step.get("name") or "").strip()
    skill_name = resolve_handoff_skill_name(db, handoff)
    title = "·".join(part for part in (skill_name, step_name) if part)
    session = db.get(ChatSession, handoff.session_id)
    inquirer = (
        _resolve_inquirer_display_name(
            db, session, _resolve_session_binding(db, session)
        )
        if session
        else ""
    )
    assignee = db.get(User, handoff.assignee_user_id) if handoff.assignee_user_id else None
    assignee_notice = HumanHandoffService.unconfigured_assignee_notice(handoff, assignee) or ""
    entries, scoped = load_handoff_conversation(db, handoff, session)
    fallback_question = re.sub(r"\s+", " ", str(handoff.pending_question or "")).strip()
    return HandoffNoticeContent(
        title=title,
        inquirer=inquirer,
        assignee_notice=assignee_notice,
        scoped=scoped,
        entries=entries,
        fallback_question=fallback_question,
    )


def render_handoff_notice_text(
    content: HandoffNoticeContent,
    *,
    channel: str | None = None,
    handoff_id: str = "",
) -> str:
    """渲染渠道私聊通知正文:标题/提问人/未配置说明 + 对话窗口 + 回复指引。

    会话超预算时从最旧的消息开始丢弃并标注省略条数,保证含 slot 答复的
    最新轮次完整;找不到任何会话消息时回退 fallback_question。
    回复指引按 channel 区分:飞书引导引用回复,其余渠道提示带 handoff_id 回复。
    """
    footer = _handoff_notice_footer(channel, handoff_id)
    header_lines = [f"【转人工】{content.title}".rstrip()]
    if content.inquirer:
        header_lines.append(f"提问人：{content.inquirer}")
    if content.assignee_notice:
        header_lines.append(content.assignee_notice)
    if not content.entries:
        fallback = content.fallback_question[:600] or "当前会话需要人工处理后继续。"
        body_lines = [fallback]
    else:
        header_lines.append(f"对话记录{'（自进入该SOP起）' if content.scoped else ''}：")
        lines = [
            f"  {HANDOFF_NOTICE_ROLE_LABELS.get(role, role)}：{clip_handoff_message(text)}"
            for role, text in content.entries
        ]
        budget = HANDOFF_NOTICE_TOTAL_LIMIT - len(footer)
        for line in (*header_lines, HANDOFF_NOTICE_SEPARATOR):
            budget -= len(line) + 1
        kept: list[str] = []
        used = 0
        omitted = 0
        for line in reversed(lines):
            if kept and used + len(line) + 1 > budget:
                omitted = len(lines) - len(kept)
                break
            kept.append(line)
            used += len(line) + 1
        kept.reverse()
        body_lines = []
        if omitted:
            body_lines.append(f"（较早的 {omitted} 条对话已省略）")
        body_lines.extend(kept)
    return "\n".join([*header_lines, *body_lines, HANDOFF_NOTICE_SEPARATOR, footer])
