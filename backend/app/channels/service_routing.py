from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlmodel import Session, select

from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelBindingAgent,
    ChannelConvState,
    utc_now,
)

COMMAND_PREFIX = "/"

HELP_TEXT = (
    "可用指令：\n"
    "/员工 查看可调度员工列表\n"
    "/切换 <名字> 切换到指定员工\n"
    "/当前 查看当前员工\n"
    "/绑定 <绑定码> 把微信绑定到你的 StaffDeck 账号\n"
    "/解绑 解除微信与 StaffDeck 账号的绑定\n"
    "/帮助 查看本说明"
)


@dataclass
class ChannelCommand:
    kind: str  # list/current/help/switch
    query: str = ""  # switch 的目标名字(可为空)


def parse_command(text: str) -> ChannelCommand | None:
    """解析行首斜杠指令(忽略大小写与首尾空白);非指令消息返回 None。"""
    stripped = (text or "").strip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None
    body = stripped[1:].strip()
    lowered = body.lower()
    if lowered in {"员工", "list"}:
        return ChannelCommand(kind="list")
    if lowered in {"当前", "目前"}:
        return ChannelCommand(kind="current")
    if lowered in {"帮助", "?", "？"}:
        return ChannelCommand(kind="help")
    if lowered in {"解绑", "unbind"}:
        return ChannelCommand(kind="unbind")
    for prefix in ("绑定", "bind"):
        if lowered.startswith(prefix):
            return ChannelCommand(kind="bind", query=body[len(prefix):].strip())
    if lowered.startswith("切换"):
        return ChannelCommand(kind="switch", query=body[len("切换"):].strip())
    if body and " " not in body and "\n" not in body:
        # /<名字> 直达
        return ChannelCommand(kind="switch", query=body)
    return ChannelCommand(kind="help")


def mounted_agents(db: Session, binding: ChannelBinding) -> list[ChannelBindingAgent]:
    """挂载集;无挂载行(存量 v1 绑定)回退为 [binding.agent_id] 默认,不依赖回填。"""
    rows = db.exec(
        select(ChannelBindingAgent)
        .where(ChannelBindingAgent.binding_id == binding.id)
        .order_by(ChannelBindingAgent.sort_order, ChannelBindingAgent.created_at)
    ).all()
    if rows:
        return list(rows)
    return [
        ChannelBindingAgent(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            agent_id=binding.agent_id,
            is_default=True,
            sort_order=0,
        )
    ]


def default_agent_id(mounts: list[ChannelBindingAgent]) -> str:
    for mount in mounts:
        if mount.is_default:
            return mount.agent_id
    return mounts[0].agent_id


def agent_names(db: Session, tenant_id: str, agent_ids: list[str]) -> dict[str, str]:
    if not agent_ids:
        return {}
    rows = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id.in_(agent_ids),
        )
    ).all()
    return {row.id: row.name for row in rows}


def _get_conv_state(db: Session, binding: ChannelBinding, external_conv_id: str) -> ChannelConvState | None:
    return db.exec(
        select(ChannelConvState).where(
            ChannelConvState.binding_id == binding.id,
            ChannelConvState.external_conv_id == external_conv_id,
        )
    ).first()


def resolve_current_agent(
    db: Session,
    binding: ChannelBinding,
    external_conv_id: str,
) -> tuple[str, bool]:
    """返回 (当前员工 agent_id, 是否发生了重置需提示)。

    无指针 → 建指针=默认员工;指针员工已不在挂载集 → 重置默认并标记需提示。
    """
    mounts = mounted_agents(db, binding)
    fallback = default_agent_id(mounts)
    state = _get_conv_state(db, binding, external_conv_id)
    if not state:
        db.add(
            ChannelConvState(
                tenant_id=binding.tenant_id,
                binding_id=binding.id,
                external_conv_id=external_conv_id,
                current_agent_id=fallback,
            )
        )
        db.flush()
        return fallback, False
    mounted_ids = {mount.agent_id for mount in mounts}
    if state.current_agent_id not in mounted_ids:
        state.current_agent_id = fallback
        state.updated_at = utc_now()
        db.add(state)
        db.flush()
        return fallback, True
    return state.current_agent_id, False


def set_current_agent(
    db: Session,
    binding: ChannelBinding,
    external_conv_id: str,
    agent_id: str,
    *,
    pin_until=None,
) -> None:
    """写路由指针;pin_until 非空时同时写手动保护窗(智能分发跳过)。"""
    state = _get_conv_state(db, binding, external_conv_id)
    if state:
        state.current_agent_id = agent_id
        state.updated_at = utc_now()
        if pin_until is not None:
            state.manual_pin_until = pin_until
    else:
        state = ChannelConvState(
            tenant_id=binding.tenant_id,
            binding_id=binding.id,
            external_conv_id=external_conv_id,
            current_agent_id=agent_id,
            manual_pin_until=pin_until,
        )
    db.add(state)
    db.flush()


def manual_pin_active(db: Session, binding: ChannelBinding, external_conv_id: str) -> bool:
    """手动切换保护窗是否仍在有效期内。"""
    state = _get_conv_state(db, binding, external_conv_id)
    return bool(state and state.manual_pin_until and state.manual_pin_until > utc_now())


def _display_name(names: dict[str, str], agent_id: str) -> str:
    return names.get(agent_id) or agent_id


def run_command(db: Session, binding: ChannelBinding, external_conv_id: str, cmd: ChannelCommand) -> str:
    """执行斜杠指令并返回回复文本。"""
    mounts = mounted_agents(db, binding)
    names = agent_names(db, binding.tenant_id, [mount.agent_id for mount in mounts])
    current_id, _ = resolve_current_agent(db, binding, external_conv_id)
    if cmd.kind == "list":
        lines = ["可调度员工："]
        for index, mount in enumerate(mounts, start=1):
            marks = []
            if mount.is_default:
                marks.append("默认")
            if mount.agent_id == current_id:
                marks.append("当前")
            suffix = f"（{'/'.join(marks)}）" if marks else ""
            lines.append(f"{index}. {_display_name(names, mount.agent_id)}{suffix}")
        lines.append("输入 /切换 <名字> 切换员工，/当前 查看当前员工。")
        return "\n".join(lines)
    if cmd.kind == "current":
        return f"当前员工：「{_display_name(names, current_id)}」。输入 /员工 查看可调度列表。"
    if cmd.kind == "switch":
        if not cmd.query:
            return "用法：/切换 <员工名字>。输入 /员工 查看可调度列表。"
        lowered = cmd.query.casefold()
        target = next(
            (
                mount
                for mount in mounts
                if _display_name(names, mount.agent_id).casefold() == lowered
            ),
            None,
        )
        if not target:
            return f"没有找到员工「{cmd.query}」。输入 /员工 查看可调度列表。"
        target_name = _display_name(names, target.agent_id)
        if target.agent_id == current_id:
            return f"当前已经是「{target_name}」。"
        # 手动切换成功:写 10 分钟保护窗,窗内智能分发不自动切回
        set_current_agent(
            db, binding, external_conv_id, target.agent_id, pin_until=utc_now() + timedelta(minutes=10)
        )
        return f"已切换到「{target_name}」，后续消息由 TA 回复。上下文各自独立，输入 /员工 查看列表。"
    return HELP_TEXT
