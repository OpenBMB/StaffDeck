from __future__ import annotations

import threading
from typing import Literal

from sqlalchemy import update
from sqlmodel import Session, select

from app.core import AgentLoop
from app.db import engine
from app.db.models import (
    AgentProfile,
    ChatSession,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Team,
    TeamTask,
    TeamTaskBid,
    TeamTaskEvent,
    TeamWakeEvent,
    User,
    new_id,
    utc_now,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse
from app.teams.service import (
    BID_HP_INITIAL,
    BID_SCORE_FALLBACK,
    VERDICT_TARGET_STATUS,
    apply_task_transition,
    bid_rebuttal_rounds,
    blackboard_context_lines,
    candidate_hp,
    get_team_leader,
    list_team_members,
    looks_like_dispatch_intent,
    member_concurrency,
    open_tasks_summary,
    parse_bid,
    parse_bid_award,
    parse_bid_scores,
    parse_blackboard_suggestions,
    parse_tl_review,
    parse_tl_task_assignments,
    record_task_event,
    select_bid_candidates,
    team_roster_lines,
    write_blackboard_entries,
)

TL_ASSIGNMENT_INSTRUCTION = (
    "派发任务的唯一方式是输出一个围栏代码块 ```json,内容形如:"
    '{"team_tasks": [{"title": "任务标题", "description": "任务描述", '
    '"assignee_agent_id": "成员的 agent_id"}]}。'
    "assignee_agent_id 必须来自上面的花名册;可以一次派多个任务。"
    "省略 assignee_agent_id 即把任务投入任务池,由成员竞标、你裁决后中标者执行。"
    "注意:只有输出该 JSON 代码块,任务才会被真正创建并交给成员执行;"
    "只用自然语言宣布『已派发』是无效的,系统不会创建任何任务。"
    "如果只是与人讨论、不需要派任务,就不要输出该代码块。"
)

TL_REVIEW_INSTRUCTION = (
    "验收结论的唯一生效方式是输出一个围栏代码块 ```json,内容形如:"
    '{"team_review": {"verdict": "approve", "comment": "验收意见"}}。'
    "verdict 只能是 approve(通过)/ rework(退回重做)/ escalate(升级给人处理)。"
    "注意:只有输出该 JSON 代码块,验收结论才会生效;口头宣布结论是无效的。"
)

TL_ASSIGNMENT_REPAIR_MESSAGE = (
    "系统提示:你的上一条回复没有包含规定的 ```json 任务代码块,因此没有创建任何任务。"
    "如果你刚才的意图是派发任务,请立即输出规定的 JSON 代码块(可只输出代码块);"
    "如果不需要派发任务,请回复「无需派任务」。"
)

TL_REVIEW_REPAIR_MESSAGE = (
    "系统提示:你的上一条回复没有包含规定的 ```json 验收代码块,验收结论未生效。"
    "请立即输出规定的 team_review JSON 代码块(可只输出代码块)。"
)

BID_INSTRUCTION = (
    "请为该任务提交竞标陈述,唯一生效方式是输出一个围栏代码块 ```json,内容形如:"
    '{"bid": {"plan": "执行思路", "estimated_cost": "粗估成本", '
    '"confidence": "high|medium|low"}}。'
    "注意:只有输出该 JSON 代码块,你的竞标才会被正式记录并进入 TL 裁决。"
)

BID_REBUTTAL_NOTE = (
    "以上是各候选的当前血条与上一轮其他存活候选的发言。本轮是反驳轮:你可以针对其他候选的"
    "发言进行反驳、指出其方案的风险,或补强自己的方案;输出格式与上一轮相同(bid 代码块语义不变)。"
)

TL_BID_SCORE_INSTRUCTION = (
    "请按任务理解/可行性/能力匹配/预估成本四个维度,为本轮每位候选发言打分(0-10)。"
    "打分的唯一生效方式是输出一个围栏代码块 ```json,内容形如:"
    '{"bid_scores": {"agent_id": {"score": 8.5, "rationale": "打分理由"}}}。'
    "bid_scores 必须覆盖本轮每位候选;分数会扣减候选血条(HP),HP 归零即淘汰。"
    "本轮只打分、不裁决中标者。"
)

TL_BID_SCORE_REPAIR_MESSAGE = (
    "系统提示:你的上一条回复没有包含规定的 ```json 打分代码块,本轮打分未生效。"
    "请立即输出规定的 bid_scores JSON 代码块(可只输出代码块)。"
)

TL_BID_JUDGE_INSTRUCTION = (
    "请按任务理解/可行性/能力匹配/预估成本四个维度为每位候选打分(0-10),并选出中标者。"
    "裁决的唯一生效方式是输出一个围栏代码块 ```json,内容形如:"
    '{"bid_award": {"winner_agent_id": "候选的 agent_id", '
    '"scores": {"agent_id": {"score": 8.5, "rationale": "打分理由"}}, "comment": "裁决说明"}}。'
    "winner_agent_id 必须来自血条列出的存活候选;只有输出该 JSON 代码块,裁决才会生效。"
)

TL_BID_JUDGE_REPAIR_MESSAGE = (
    "系统提示:你的上一条回复没有包含规定的 ```json 裁决代码块,"
    "或 winner_agent_id 不在候选列表中,裁决未生效。"
    "请立即输出规定的 bid_award JSON 代码块(可只输出代码块)。"
)


def build_tl_chat_context(db: Session, team: Team, user_message: str) -> str:
    """Build server-only TL context without embedding the visible user message."""

    roster = team_roster_lines(db, team)
    open_tasks = open_tasks_summary(db, team)
    lines = [f"你是团队「{team.name}」的 TL(团队负责人),负责拆解需求并指派给团队成员。"]
    if team.description:
        lines.append(f"团队简介:{team.description}")
    lines.append("团队花名册:")
    lines.extend(roster or ["- (暂无成员)"])
    lines.append("当前未闭环任务:")
    lines.extend(open_tasks or ["- (暂无)"])
    blackboard = blackboard_context_lines(db, team, user_message)
    if blackboard:
        lines.append("团队黑板(相关工作记忆):")
        lines.extend(blackboard)
    lines.append(TL_ASSIGNMENT_INSTRUCTION)
    lines.append("人的需求:")
    return "\n".join(lines)


def build_tl_chat_message(db: Session, team: Team, user_message: str) -> str:
    """Compatibility helper returning the complete model input for a TL turn."""

    return f"{build_tl_chat_context(db, team, user_message)}\n{user_message}"


def build_member_task_message(db: Session, team: Team, task: TeamTask, *, rework: bool) -> str:
    """成员执行上下文注入:任务描述(+ 退回意见)+ 黑板 + 报告与黑板建议要求。"""
    lines = [f"你是团队「{team.name}」的成员,请完成以下团队任务。"]
    lines.append(f"任务标题:{task.title}")
    if task.description:
        lines.append(f"任务描述:{task.description}")
    if rework:
        comment = str((task.review_json or {}).get("comment") or "").strip()
        lines.append("该任务已被退回重做。" + (f"退回意见:{comment}" if comment else ""))
    query_text = f"{task.title}\n{task.description or ''}"
    blackboard = blackboard_context_lines(db, team, query_text)
    if blackboard:
        lines.append("团队黑板(相关工作记忆):")
        lines.extend(blackboard)
    lines.append("完成后请输出结构化完成报告,包含:结论、过程要点、交付物。")
    lines.append(
        "如果你在执行中发现了值得全团队记住的信息(关键结论/约定口径/容易踩的坑),"
        "请在报告末尾额外输出一个围栏代码块 ```json,内容形如:"
        '{"blackboard_suggestions": [{"content": "值得记住的事", "tags": ["标签"]}]}。'
        "建议由 TL 验收时裁决后才会真正写入团队黑板;没有值得记录的信息就不要输出该代码块。"
    )
    return "\n".join(lines)


def build_tl_review_message(db: Session, team: Team, task: TeamTask) -> str:
    """TL 验收上下文注入:任务描述 + 成员报告 + 黑板 + 验收输出格式(+ 黑板裁决)。"""
    report = task.report_json if isinstance(task.report_json, dict) else {}
    report_text = str(report.get("full_reply") or report.get("summary") or "(成员未提交报告内容)")
    lines = [f"你是团队「{team.name}」的 TL,请验收成员提交的任务报告。"]
    lines.append(f"任务标题:{task.title}")
    if task.description:
        lines.append(f"任务描述:{task.description}")
    query_text = f"{task.title}\n{task.description or ''}"
    blackboard = blackboard_context_lines(db, team, query_text)
    if blackboard:
        lines.append("团队黑板(相关工作记忆):")
        lines.extend(blackboard)
    lines.append(f"成员报告:{report_text}")
    lines.append(TL_REVIEW_INSTRUCTION)
    suggestions = report.get("blackboard_suggestions")
    if isinstance(suggestions, list) and suggestions:
        lines.append("成员随报告提交了以下黑板建议(认为值得全团队记住的信息):")
        for index, item in enumerate(suggestions, 1):
            if not isinstance(item, dict):
                continue
            tags = item.get("tags")
            tags_text = ",".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
            line = f"{index}. {item.get('content') or ''}"
            if tags_text:
                line += f"(标签: {tags_text})"
            lines.append(line)
        lines.append(
            "请逐条裁决:认可的条目(可修改措辞后)放进同一个 ```json 块的 team_review 里,"
            '增加可选字段 "blackboard_writes": [{"content": "...", "tags": ["..."]}];'
            "未写入 blackboard_writes 的建议即视为拒绝。"
        )
    return "\n".join(lines)


def build_bid_request_message(
    db: Session, team: Team, task: TeamTask, agent: AgentProfile, *, round_: int
) -> str:
    """竞标上下文注入:任务描述 + 黑板 + 竞标指令;反驳轮附各候选血条与上一轮其他存活候选的发言。"""
    lines = [f"你是团队「{team.name}」的成员,以下团队任务正在任务池中开放竞标。"]
    lines.append(f"任务标题:{task.title}")
    if task.description:
        lines.append(f"任务描述:{task.description}")
    query_text = f"{task.title}\n{task.description or ''}"
    blackboard = blackboard_context_lines(db, team, query_text)
    if blackboard:
        lines.append("团队黑板(相关工作记忆):")
        lines.extend(blackboard)
    if round_ >= 2:
        bids = list(db.exec(select(TeamTaskBid).where(TeamTaskBid.task_id == task.id)).all())
        hp = candidate_hp(bids)
        candidate_ids = [bid.agent_id for bid in bids if bid.round == 1]
        if candidate_ids:
            lines.append("各候选当前血条(HP,初始 100,归零淘汰):")
            for candidate_id in dict.fromkeys(candidate_ids):
                other = db.get(AgentProfile, candidate_id)
                name = other.name if other else candidate_id
                lines.append(f"- {name}(agent_id={candidate_id}):HP={hp.get(candidate_id, BID_HP_INITIAL)}")
        others = [
            bid
            for bid in bids
            if bid.round == round_ - 1 and bid.agent_id != agent.id
        ]
        if others:
            lines.append(f"第 {round_ - 1} 轮其他候选的发言:")
            for bid in others:
                other = db.get(AgentProfile, bid.agent_id)
                name = other.name if other else bid.agent_id
                lines.append(f"- {name}:{bid.content}")
            lines.append(BID_REBUTTAL_NOTE)
    lines.append(BID_INSTRUCTION)
    return "\n".join(lines)


def build_bid_score_message(
    db: Session, team: Team, task: TeamTask, bids: list[TeamTaskBid], *, round_: int
) -> str:
    """TL 每轮打分上下文注入:任务描述 + 本轮各候选发言(标注名称与 agent_id)+ 打分指令。"""
    lines = [f"你是团队「{team.name}」的 TL,以下任务竞标的第 {round_} 轮已结束,请为本轮候选打分。"]
    lines.append(f"任务标题:{task.title}")
    if task.description:
        lines.append(f"任务描述:{task.description}")
    lines.append(f"第 {round_} 轮候选发言:")
    for bid in bids:
        agent = db.get(AgentProfile, bid.agent_id)
        name = agent.name if agent else bid.agent_id
        lines.append(f"- {name}(agent_id={bid.agent_id}):{bid.content}")
    lines.append(TL_BID_SCORE_INSTRUCTION)
    return "\n".join(lines)


def build_bid_judge_message(
    db: Session,
    team: Team,
    task: TeamTask,
    bids: list[TeamTaskBid],
    alive_candidate_ids: list[str],
) -> str:
    """TL 竞标裁决上下文注入:任务描述 + 各候选发言(标注名称与 agent_id)+ 血条 + 裁决指令。"""
    lines = [f"你是团队「{team.name}」的 TL,以下任务的竞标已结束,请裁决中标者。"]
    lines.append(f"任务标题:{task.title}")
    if task.description:
        lines.append(f"任务描述:{task.description}")
    hp = candidate_hp(bids)
    if alive_candidate_ids:
        lines.append("各候选当前血条(HP,初始 100,归零淘汰):")
        for candidate_id in alive_candidate_ids:
            agent = db.get(AgentProfile, candidate_id)
            name = agent.name if agent else candidate_id
            lines.append(f"- {name}(agent_id={candidate_id}):HP={hp.get(candidate_id, BID_HP_INITIAL)}")
    lines.append("候选竞标记录:")
    for bid in bids:
        agent = db.get(AgentProfile, bid.agent_id)
        name = agent.name if agent else bid.agent_id
        round_text = "陈述" if bid.kind == "statement" else "反驳"
        score_text = f"(第 {bid.round} 轮得分 {bid.score})" if bid.score is not None else ""
        lines.append(f"- {name}(agent_id={bid.agent_id})的{round_text}{score_text}:{bid.content}")
    lines.append(TL_BID_JUDGE_INSTRUCTION)
    return "\n".join(lines)


def enqueue_wake_event(
    db: Session,
    *,
    team: Team,
    target_agent_id: str,
    trigger_type: str,
    payload: dict | None = None,
) -> TeamWakeEvent:
    event = TeamWakeEvent(
        team_id=team.id,
        tenant_id=team.tenant_id,
        target_agent_id=target_agent_id,
        trigger_type=trigger_type,
        payload_json=dict(payload or {}),
        status="pending",
    )
    db.add(event)
    db.flush()
    return event


def claim_wake_event(db: Session, wake_event_id: str) -> bool:
    """原子认领 pending 唤醒事件;并发/重复触发下只生效一次。"""
    result = db.exec(
        update(TeamWakeEvent)
        .where(TeamWakeEvent.id == wake_event_id, TeamWakeEvent.status == "pending")
        .values(status="claimed", updated_at=utc_now())
    )
    db.commit()
    return result.rowcount == 1


def start_wakeup_async(wake_event_id: str) -> None:
    threading.Thread(
        target=_execute_wakeup_in_background,
        args=(wake_event_id,),
        daemon=True,
    ).start()


def _execute_wakeup_in_background(wake_event_id: str) -> None:
    with Session(engine) as db:
        if not claim_wake_event(db, wake_event_id):
            return
        event = db.get(TeamWakeEvent, wake_event_id)
        if event is None:
            return
        execute_wake_event(db, event)


def _ensure_wake_target_agent(db: Session, event: TeamWakeEvent) -> AgentProfile:
    agent = db.get(AgentProfile, event.target_agent_id)
    if agent is None or agent.tenant_id != event.tenant_id or agent.status != "active":
        raise RuntimeError("唤醒目标员工已不可用;请检查团队配置。")
    return agent


# ---------- 成员执行串行排队 ----------

# 执行类唤醒:占用成员执行额度;竞标/裁决属轻量 turn,直接放行不占额度
EXECUTION_WAKE_TYPES = {"task_assigned", "task_rework"}

# 进程内成员执行额度计数:与 DB in_progress 计数互补,覆盖落库前的并发窗口
_member_slot_counts: dict[tuple[str, str], int] = {}
_member_slot_guard = threading.Lock()


def _try_acquire_member_slot(team_id: str, agent_id: str, limit: int) -> bool:
    with _member_slot_guard:
        running = _member_slot_counts.get((team_id, agent_id), 0)
        if running >= limit:
            return False
        _member_slot_counts[(team_id, agent_id)] = running + 1
        return True


def _release_member_slot(team_id: str, agent_id: str) -> None:
    with _member_slot_guard:
        running = _member_slot_counts.get((team_id, agent_id), 0)
        if running <= 1:
            _member_slot_counts.pop((team_id, agent_id), None)
        else:
            _member_slot_counts[(team_id, agent_id)] = running - 1


def _member_in_progress_count(db: Session, team: Team, agent_id: str) -> int:
    return len(
        db.exec(
            select(TeamTask).where(
                TeamTask.team_id == team.id,
                TeamTask.assignee_agent_id == agent_id,
                TeamTask.status == "in_progress",
            )
        ).all()
    )


def _record_wake_queued(
    db: Session, event: TeamWakeEvent, team: Team, agent: AgentProfile
) -> None:
    """执行类唤醒排队审计:关联任务存在才记任务事件。"""
    task_id = str(event.payload_json.get("task_id") or "")
    if not task_id or db.get(TeamTask, task_id) is None:
        return
    record_task_event(
        db,
        team_id=team.id,
        task_id=task_id,
        actor_type="system",
        actor_id=None,
        event_type="wake_queued",
        payload={"wake_event_id": event.id, "agent_id": agent.id},
    )


def _drain_member_queue(db: Session, team: Team, agent_id: str) -> None:
    """成员任务终态后出队:有空闲额度时拉起该成员本团队最老的 pending 执行类唤醒。"""
    if _member_in_progress_count(db, team, agent_id) >= member_concurrency(team):
        return
    wake = db.exec(
        select(TeamWakeEvent)
        .where(
            TeamWakeEvent.team_id == team.id,
            TeamWakeEvent.target_agent_id == agent_id,
            TeamWakeEvent.status == "pending",
            TeamWakeEvent.trigger_type.in_(sorted(EXECUTION_WAKE_TYPES)),
        )
        .order_by(TeamWakeEvent.created_at)
    ).first()
    if wake is not None:
        start_wakeup_async(wake.id)


def _team_harness_outcome(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    client_turn_id: str,
) -> str:
    """按 Harness v2 持久记录判定唤醒 turn 结果(仿定时任务)。

    返回 "completed" / "needs_input"(成员在等补充信息,合法状态);
    其余未完成形态(失败/取消等)抛异常。
    """
    receipt = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.client_turn_id == client_turn_id,
        )
    ).first()
    if receipt is None or not receipt.user_message_id:
        raise RuntimeError("团队任务未进入 Harness v2,已拒绝按旧链路判定成功。")
    frames = db.exec(
        select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.source_turn_id == receipt.user_message_id,
        )
    ).all()
    if not frames:
        raise RuntimeError("Harness v2 未生成 TaskFrame,团队任务不能判定为成功。")
    statuses: list[str] = []
    for frame in frames:
        result = frame.result_json if isinstance(frame.result_json, dict) else {}
        statuses.append(str(result.get("status") or "").strip() or frame.status)
    if all(status == "completed" for status in statuses):
        return "completed"
    if any(status in {"awaiting_user", "needs_input"} for status in statuses):
        return "needs_input"
    raise RuntimeError("一个或多个 TaskFrame 未完成,团队任务执行失败。")


def collect_turn_reply_fragments(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    client_turn_id: str,
) -> list[str]:
    """取一次 harness turn 内各 TaskFrame 的 reply_fragment。

    最终回复由 ResponseGenerator 面向用户改写,可能丢失围栏 JSON 块;
    frame 级 reply_fragment 保留原始输出,TL 的 JSON 块协议以两者并集为准。
    """
    receipt = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.client_turn_id == client_turn_id,
        )
    ).first()
    if receipt is None or not receipt.user_message_id:
        return []
    frames = db.exec(
        select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.source_turn_id == receipt.user_message_id,
        )
    ).all()
    fragments: list[str] = []
    for frame in frames:
        result = frame.result_json if isinstance(frame.result_json, dict) else {}
        fragment = str(result.get("reply_fragment") or "").strip()
        if fragment:
            fragments.append(fragment)
    return fragments


def run_agent_turn(
    db: Session,
    *,
    team: Team,
    agent: AgentProfile,
    session_id: str,
    wake_event_id: str,
    message: str,
    interaction_mode: Literal["team_task", "team_tl"],
    client_turn_id: str | None = None,
    allow_needs_input: bool = False,
) -> str:
    """在独立会话里执行一轮 agent turn,返回回复文本;执行失败抛异常。

    allow_needs_input=True 时,turn 落在 awaiting_user/needs_input 不抛异常,
    由调用方决定如何安置(如成员任务转人工补充信息)。
    """
    turn_id = client_turn_id or wake_event_id
    request = ChatTurnRequest(
        tenant_id=team.tenant_id,
        session_id=session_id,
        agent_id=agent.id,
        client_turn_id=turn_id,
        user_id=team.owner_user_id,
        message=message,
        channel="team",
        interaction_mode=interaction_mode,
    )
    result: ChatTurnResponse | None = None
    for item in AgentLoop(db).handle_turn_stream(request):
        if item.get("event") in {"complete", "done"} and isinstance(item.get("data"), dict):
            result = ChatTurnResponse.model_validate(item["data"])
    if result is None:
        raise RuntimeError("团队唤醒执行未返回完整结果")
    outcome = _team_harness_outcome(
        db,
        tenant_id=team.tenant_id,
        session_id=session_id,
        client_turn_id=turn_id,
    )
    if outcome == "needs_input" and not allow_needs_input:
        raise RuntimeError("agent 需要补充信息才能继续,当前场景不支持挂起等待。")
    return result.reply


def execute_wake_event(db: Session, event: TeamWakeEvent) -> TeamWakeEvent:
    """唤醒执行体;任务执行/验收失败会把关联任务置为 escalated,不静默丢任务。

    竞标(bid_request)失败例外:只记 bid_failed 审计并尝试推进竞标,不升级任务。
    执行类唤醒(task_assigned/task_rework)受成员串行约束:占不到执行额度时
    事件保持 pending 直接返回(记 wake_queued 审计),由终态出队重新拉起。
    """
    team: Team | None = None
    agent: AgentProfile | None = None
    slot_acquired = False
    try:
        team = db.get(Team, event.team_id)
        if team is None:
            raise RuntimeError("唤醒事件所属团队不存在")
        agent = _ensure_wake_target_agent(db, event)
        if event.trigger_type in EXECUTION_WAKE_TYPES:
            # 成员串行排队:进程内额度 + DB in_progress 计数双重判定,
            # 覆盖落库前的并发窗口与进程重启后的存量执行中任务
            limit = member_concurrency(team)
            slot_acquired = _try_acquire_member_slot(team.id, agent.id, limit)
            if not slot_acquired or _member_in_progress_count(db, team, agent.id) >= limit:
                if slot_acquired:
                    _release_member_slot(team.id, agent.id)
                    slot_acquired = False
                event.status = "pending"
                _record_wake_queued(db, event, team, agent)
                return event
            _execute_member_task(db, event, team, agent)
        elif event.trigger_type == "task_report":
            _execute_tl_review(db, event, team, agent)
        elif event.trigger_type == "bid_request":
            _execute_bid_request(db, event, team, agent)
        elif event.trigger_type == "bid_judge":
            _execute_bid_judge(db, event, team, agent)
        else:
            raise RuntimeError(f"未知唤醒触发类型: {event.trigger_type}")
        event.status = "done"
    except Exception as exc:
        if event.trigger_type == "bid_request":
            _handle_bid_failure(db, event, exc)
        else:
            _escalate_task_on_failure(db, event, exc)
        event.status = "failed"
        event.error = str(exc)
    finally:
        event.updated_at = utc_now()
        db.add(event)
        db.commit()
        db.refresh(event)
        if slot_acquired and team is not None and agent is not None:
            _release_member_slot(team.id, agent.id)
            # 执行额度随终态释放,出队拉起该成员最老的排队唤醒
            _drain_member_queue(db, team, agent.id)
    return event


def _escalate_task_on_failure(db: Session, event: TeamWakeEvent, exc: Exception) -> None:
    task_id = str(event.payload_json.get("task_id") or "")
    if not task_id:
        return
    task = db.get(TeamTask, task_id)
    if task is None or task.status in {"done", "escalated"}:
        return
    try:
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": str(exc), "wake_event_id": event.id},
        )
        db.commit()
    except Exception:
        db.rollback()


def _execute_member_task(db: Session, event: TeamWakeEvent, team: Team, agent: AgentProfile) -> None:
    task_id = str(event.payload_json.get("task_id") or "")
    task = db.get(TeamTask, task_id)
    if task is None or task.team_id != team.id:
        raise RuntimeError("唤醒事件关联的团队任务不存在")
    if task.assignee_agent_id and task.assignee_agent_id != agent.id:
        raise RuntimeError("唤醒目标与任务指派成员不一致")
    rework = event.trigger_type == "task_rework"
    session = ChatSession(
        id=new_id("session"),
        tenant_id=team.tenant_id,
        user_id=team.owner_user_id,
        agent_id=agent.id,
        title=f"团队任务:{task.title}",
        status="active",
        team_id=team.id,
    )
    db.add(session)
    db.flush()
    task.session_id = session.id
    apply_task_transition(
        db,
        task,
        "in_progress",
        actor_type="agent",
        actor_id=agent.id,
        event_type="task_rework_started" if rework else "task_started",
        payload={"wake_event_id": event.id},
    )
    db.commit()
    message = build_member_task_message(db, team, task, rework=rework)
    reply = run_agent_turn(
        db,
        team=team,
        agent=agent,
        session_id=session.id,
        wake_event_id=event.id,
        message=message,
        interaction_mode="team_task",
        allow_needs_input=True,
    )
    outcome = _team_harness_outcome(
        db,
        tenant_id=team.tenant_id,
        session_id=session.id,
        client_turn_id=event.id,
    )
    task.report_json = {
        "summary": reply[:500],
        "full_reply": reply,
        "finished_at": utc_now().isoformat(),
    }
    if outcome == "needs_input":
        # 成员需要补充信息(如索要合同文本):保留提问,升级给人;
        # 人可通过改判/退回重做把补充信息带回给成员(rework 通道即答复通道)
        task.report_json["needs_input"] = True
        db.add(task)
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="agent",
            actor_id=agent.id,
            event_type="task_needs_input",
            payload={"wake_event_id": event.id, "question": reply[:500]},
        )
        db.commit()
        return
    # 成员黑板建议:最终回复 + frame 级 reply_fragment 并集解析;裁决权在 TL,此处只暂存
    suggestions = parse_blackboard_suggestions(reply)
    if not suggestions:
        for fragment in collect_turn_reply_fragments(
            db,
            tenant_id=team.tenant_id,
            session_id=session.id,
            client_turn_id=event.id,
        ):
            suggestions = parse_blackboard_suggestions(fragment)
            if suggestions:
                break
    if suggestions:
        task.report_json["blackboard_suggestions"] = suggestions
    apply_task_transition(
        db,
        task,
        "review",
        actor_type="agent",
        actor_id=agent.id,
        event_type="task_reported",
        payload={"wake_event_id": event.id},
    )
    db.add(task)
    leader = get_team_leader(db, team.id)
    if leader is None:
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": "团队缺少 TL,无法验收"},
        )
        db.commit()
        return
    db.commit()
    wake = enqueue_wake_event(
        db,
        team=team,
        target_agent_id=leader.agent_id,
        trigger_type="task_report",
        payload={"task_id": task.id},
    )
    db.commit()
    start_wakeup_async(wake.id)


def _execute_tl_review(db: Session, event: TeamWakeEvent, team: Team, agent: AgentProfile) -> None:
    task_id = str(event.payload_json.get("task_id") or "")
    task = db.get(TeamTask, task_id)
    if task is None or task.team_id != team.id:
        raise RuntimeError("唤醒事件关联的团队任务不存在")
    if task.status != "review":
        # 任务可能已被人改判或退回,迟到/重复的验收唤醒直接跳过
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="system",
            actor_id=None,
            event_type="tl_review_skipped",
            payload={"wake_event_id": event.id, "task_status": task.status},
        )
        db.commit()
        return
    session = ChatSession(
        id=new_id("session"),
        tenant_id=team.tenant_id,
        user_id=team.owner_user_id,
        agent_id=agent.id,
        title=f"团队任务验收:{task.title}",
        status="active",
        team_id=team.id,
    )
    db.add(session)
    db.commit()
    message = build_tl_review_message(db, team, task)
    reply = run_agent_turn(
        db,
        team=team,
        agent=agent,
        session_id=session.id,
        wake_event_id=event.id,
        message=message,
        interaction_mode="team_tl",
    )
    verdict = parse_tl_review(reply)
    if verdict is None:
        # 最终回复被 ResponseGenerator 改写时,JSON 块可能只存在于 frame 级输出
        for fragment in collect_turn_reply_fragments(
            db,
            tenant_id=team.tenant_id,
            session_id=session.id,
            client_turn_id=event.id,
        ):
            verdict = parse_tl_review(fragment)
            if verdict is not None:
                reply = fragment
                break
    if verdict is None:
        # TL 未输出验收代码块:在原会话内补跑一次格式纠错 turn,再解析一次
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=agent.id,
            event_type="tl_review_unparsed",
            payload={"wake_event_id": event.id},
        )
        db.commit()
        repair_reply = run_agent_turn(
            db,
            team=team,
            agent=agent,
            session_id=session.id,
            wake_event_id=event.id,
            client_turn_id=f"{event.id}-repair",
            message=f"{message}\n\n{TL_REVIEW_REPAIR_MESSAGE}",
            interaction_mode="team_tl",
        )
        verdict = parse_tl_review(repair_reply)
        if verdict is None:
            for fragment in collect_turn_reply_fragments(
                db,
                tenant_id=team.tenant_id,
                session_id=session.id,
                client_turn_id=f"{event.id}-repair",
            ):
                verdict = parse_tl_review(fragment)
                if verdict is not None:
                    repair_reply = fragment
                    break
        if verdict is not None:
            reply = repair_reply
    if verdict is None:
        # 纠错后仍无结论:不改状态,等待人介入
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=agent.id,
            event_type="tl_review_repair_failed",
            payload={"wake_event_id": event.id},
        )
        db.commit()
        return
    target = VERDICT_TARGET_STATUS[verdict["verdict"]]
    task.review_json = {
        "verdict": verdict["verdict"],
        "comment": verdict["comment"],
        "raw_reply": reply,
        "reviewed_at": utc_now().isoformat(),
    }
    apply_task_transition(
        db,
        task,
        target,
        actor_type="agent",
        actor_id=agent.id,
        event_type=f"tl_review_{verdict['verdict']}",
        payload={"comment": verdict["comment"], "wake_event_id": event.id},
    )
    # TL 裁决认可的黑板条目并入本次验收落库,零额外唤醒;未认可的建议即视为拒绝
    blackboard_writes = verdict.get("blackboard_writes") or []
    if blackboard_writes:
        written, skipped = write_blackboard_entries(
            db,
            team=team,
            entries=blackboard_writes,
            source_type="member",
            source_agent_id=task.assignee_agent_id,
            source_task_id=task.id,
        )
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=agent.id,
            event_type="blackboard_written",
            payload={
                "wake_event_id": event.id,
                "written": len(written),
                "entry_ids": [entry.id for entry in written],
                "skipped": skipped,
            },
        )
    db.add(task)
    db.commit()
    if verdict["verdict"] == "rework" and task.assignee_agent_id:
        wake = enqueue_wake_event(
            db,
            team=team,
            target_agent_id=task.assignee_agent_id,
            trigger_type="task_rework",
            payload={"task_id": task.id},
        )
        db.commit()
        start_wakeup_async(wake.id)


# ---------- 任务池竞标 ----------


def start_bidding(db: Session, team: Team, task: TeamTask) -> None:
    """开启任务池竞标:选候选 -> 置 bidding -> 为每个候选入队 round=1 竞标唤醒。

    无候选时任务直接升级给人,不入队任何唤醒。
    """
    candidates = select_bid_candidates(db, team, task)
    if not candidates:
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": "任务池竞标无候选成员"},
        )
        db.commit()
        return
    apply_task_transition(
        db,
        task,
        "bidding",
        actor_type="system",
        actor_id=None,
        event_type="task_bidding_started",
        payload={"candidate_agent_ids": candidates},
    )
    db.commit()
    wakes = [
        enqueue_wake_event(
            db,
            team=team,
            target_agent_id=agent_id,
            trigger_type="bid_request",
            payload={"task_id": task.id, "round": 1},
        )
        for agent_id in candidates
    ]
    db.commit()
    for wake in wakes:
        start_wakeup_async(wake.id)


def _bidding_candidates(db: Session, task: TeamTask) -> list[str]:
    """从 task_bidding_started 审计 payload 取候选列表(推进/裁决的候选集依据)。"""
    rows = db.exec(
        select(TeamTaskEvent)
        .where(
            TeamTaskEvent.task_id == task.id,
            TeamTaskEvent.event_type == "task_bidding_started",
        )
        .order_by(TeamTaskEvent.created_at)
    ).all()
    if not rows:
        return []
    payload = rows[-1].payload_json if isinstance(rows[-1].payload_json, dict) else {}
    return [str(item) for item in payload.get("candidate_agent_ids") or []]


def _bid_failed_agents(db: Session, task: TeamTask, round_: int) -> set[str]:
    """指定轮次竞标执行失败的候选集合(以 bid_failed 审计为准)。"""
    rows = db.exec(
        select(TeamTaskEvent).where(
            TeamTaskEvent.task_id == task.id, TeamTaskEvent.event_type == "bid_failed"
        )
    ).all()
    failed: set[str] = set()
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        if int(payload.get("round") or 1) == round_ and row.actor_id:
            failed.add(str(row.actor_id))
    return failed


def _wake_pending_for_task(
    db: Session,
    team: Team,
    task_id: str,
    trigger_type: str,
    round_: int | None = None,
    mode: str | None = None,
) -> bool:
    """同类唤醒是否已入队(pending/claimed);竞标推进的幂等护栏,防并发重复入队。"""
    rows = db.exec(
        select(TeamWakeEvent).where(
            TeamWakeEvent.team_id == team.id, TeamWakeEvent.trigger_type == trigger_type
        )
    ).all()
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        if str(payload.get("task_id") or "") != task_id:
            continue
        if round_ is not None and int(payload.get("round") or 1) != round_:
            continue
        if mode is not None and str(payload.get("mode") or "award") != mode:
            continue
        if row.status in {"pending", "claimed"}:
            return True
    return False


def _enqueue_bid_judge(
    db: Session, team: Team, task: TeamTask, *, mode: str, round_: int | None = None
) -> None:
    """入队 TL 裁决唤醒:mode=score(第 round_ 轮打分)/ award(最终裁决);幂等护栏防重复。"""
    if _wake_pending_for_task(db, team, task.id, "bid_judge", round_=round_, mode=mode):
        return
    leader = get_team_leader(db, team.id)
    if leader is None:
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": "团队缺少 TL,无法裁决竞标"},
        )
        db.commit()
        return
    payload: dict = {"task_id": task.id, "mode": mode}
    if round_ is not None:
        payload["round"] = round_
    wake = enqueue_wake_event(
        db,
        team=team,
        target_agent_id=leader.agent_id,
        trigger_type="bid_judge",
        payload=payload,
    )
    db.commit()
    start_wakeup_async(wake.id)


def _alive_bid_candidates(candidates: list[str], bids: list[TeamTaskBid]) -> list[str]:
    """存活候选:陈述轮已应标且血条未归零(HP>0)。"""
    stated = {bid.agent_id for bid in bids if bid.round == 1}
    hp = candidate_hp(bids)
    return [
        agent_id
        for agent_id in candidates
        if agent_id in stated and hp.get(agent_id, BID_HP_INITIAL) > 0
    ]


def _maybe_advance_bidding(db: Session, team: Team, task: TeamTask) -> None:
    """竞标推进检查:每轮存活候选全部应答(或失败)后,TL 打分(非末轮)/裁决(末轮)。

    轮次语义:round 1 = 陈述,round 2..N = 反驳(N=bid_rebuttal_rounds);
    每轮打分扣减血条,存活 ≤1 人时直接进裁决;无人应标则升级给人。
    """
    if task.status != "bidding":
        return
    candidates = _bidding_candidates(db, task)
    if not candidates:
        return
    bids = list(db.exec(select(TeamTaskBid).where(TeamTaskBid.task_id == task.id)).all())

    stated = {bid.agent_id for bid in bids if bid.round == 1}
    failed_r1 = _bid_failed_agents(db, task, 1)
    if not all(agent_id in stated or agent_id in failed_r1 for agent_id in candidates):
        return  # 陈述轮未齐,继续等待其余候选
    valid = [agent_id for agent_id in candidates if agent_id in stated]
    if not valid:
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": "任务池竞标无人应标"},
        )
        db.commit()
        return
    total_rounds = bid_rebuttal_rounds(team)
    alive = _alive_bid_candidates(candidates, bids)
    # 辩论关闭(0/1 轮)或有效应标不足两人:陈述后直接裁决(兼容旧行为)
    if total_rounds <= 1 or len(valid) < 2:
        _enqueue_bid_judge(db, team, task, mode="award")
        return
    for round_ in range(1, total_rounds + 1):
        if round_ > 1:
            answered = {bid.agent_id for bid in bids if bid.round == round_}
            failed = _bid_failed_agents(db, task, round_)
            if not all(agent_id in answered or agent_id in failed for agent_id in alive):
                # 本轮未齐:首次集齐上轮打分后为存活候选入队本轮竞标
                if not _wake_pending_for_task(db, team, task.id, "bid_request", round_=round_):
                    wakes = [
                        enqueue_wake_event(
                            db,
                            team=team,
                            target_agent_id=agent_id,
                            trigger_type="bid_request",
                            payload={"task_id": task.id, "round": round_},
                        )
                        for agent_id in alive
                        if agent_id not in answered and agent_id not in failed
                    ]
                    db.commit()
                    for wake in wakes:
                        start_wakeup_async(wake.id)
                return
        if round_ == total_rounds:
            # 末轮已齐:直接裁决
            _enqueue_bid_judge(db, team, task, mode="award")
            return
        round_bids = [
            bid for bid in bids if bid.round == round_ and bid.agent_id in set(alive)
        ]
        if round_bids and not all(bid.score is not None for bid in round_bids):
            # 非末轮已齐未打分:先入队 TL 打分,打分落库后由打分执行体再次推进
            _enqueue_bid_judge(db, team, task, mode="score", round_=round_)
            return
        # 本轮打分完成:重算血条,存活 ≤1 人提前进裁决
        alive = _alive_bid_candidates(candidates, bids)
        if len(alive) <= 1:
            _enqueue_bid_judge(db, team, task, mode="award")
            return


def _handle_bid_failure(db: Session, event: TeamWakeEvent, exc: Exception) -> None:
    """竞标候选执行失败:记 bid_failed 审计并尝试推进竞标,不升级任务。"""
    task_id = str(event.payload_json.get("task_id") or "")
    if not task_id:
        return
    task = db.get(TeamTask, task_id)
    team = db.get(Team, event.team_id)
    if task is None or team is None:
        return
    if task.status != "bidding":
        # 竞标期间已被人改判/裁决,迟到的失败只留跳过审计
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="system",
            actor_id=None,
            event_type="bid_skipped",
            payload={"wake_event_id": event.id, "task_status": task.status},
        )
        db.commit()
        return
    try:
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=event.target_agent_id,
            event_type="bid_failed",
            payload={
                "wake_event_id": event.id,
                "round": int(event.payload_json.get("round") or 1),
                "reason": str(exc),
            },
        )
        db.commit()
        _maybe_advance_bidding(db, team, task)
    except Exception:
        db.rollback()


def _execute_bid_request(
    db: Session, event: TeamWakeEvent, team: Team, agent: AgentProfile
) -> None:
    """候选竞标执行体:独立会话输出竞标块,落 team_task_bids 并推进竞标。"""
    task_id = str(event.payload_json.get("task_id") or "")
    round_ = int(event.payload_json.get("round") or 1)
    kind = "statement" if round_ <= 1 else "rebuttal"
    task = db.get(TeamTask, task_id)
    if task is None or task.team_id != team.id:
        raise RuntimeError("唤醒事件关联的团队任务不存在")
    if task.status != "bidding":
        # 任务已被人改判或已完成裁决,迟到的竞标唤醒直接跳过
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="system",
            actor_id=None,
            event_type="bid_skipped",
            payload={"wake_event_id": event.id, "task_status": task.status, "round": round_},
        )
        db.commit()
        return
    duplicate = db.exec(
        select(TeamTaskBid).where(
            TeamTaskBid.task_id == task.id,
            TeamTaskBid.agent_id == agent.id,
            TeamTaskBid.round == round_,
            TeamTaskBid.kind == kind,
        )
    ).first()
    if duplicate is not None:
        # 重复触发:同轮同类型竞标已落库,直接跳过
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="system",
            actor_id=None,
            event_type="bid_skipped",
            payload={"wake_event_id": event.id, "reason": "duplicate", "round": round_},
        )
        db.commit()
        return
    session = ChatSession(
        id=new_id("session"),
        tenant_id=team.tenant_id,
        user_id=team.owner_user_id,
        agent_id=agent.id,
        title=f"团队竞标:{task.title}",
        status="active",
        team_id=team.id,
    )
    db.add(session)
    db.commit()
    message = build_bid_request_message(db, team, task, agent, round_=round_)
    reply = run_agent_turn(
        db,
        team=team,
        agent=agent,
        session_id=session.id,
        wake_event_id=event.id,
        message=message,
        interaction_mode="team_task",
    )
    bid = parse_bid(reply)
    if bid is None:
        # 最终回复被 ResponseGenerator 改写时,竞标块可能只存在于 frame 级输出
        for fragment in collect_turn_reply_fragments(
            db,
            tenant_id=team.tenant_id,
            session_id=session.id,
            client_turn_id=event.id,
        ):
            bid = parse_bid(fragment)
            if bid is not None:
                break
    content = bid["plan"] if bid else reply.strip()
    if not content:
        raise RuntimeError("候选未给出任何竞标内容")
    db.add(
        TeamTaskBid(
            task_id=task.id,
            team_id=team.id,
            tenant_id=team.tenant_id,
            agent_id=agent.id,
            round=round_,
            kind=kind,
            content=content,
        )
    )
    record_task_event(
        db,
        team_id=team.id,
        task_id=task.id,
        actor_type="agent",
        actor_id=agent.id,
        event_type="bid_submitted",
        payload={"wake_event_id": event.id, "round": round_, "kind": kind},
    )
    db.commit()
    _maybe_advance_bidding(db, team, task)


def _parse_bid_award_with_fragments(
    db: Session,
    *,
    team: Team,
    session_id: str,
    client_turn_id: str,
    reply: str,
    candidate_ids: set[str],
) -> dict | None:
    """裁决块解析:最终回复优先,缺失时回退 frame 级 reply_fragment 并集。"""
    award = parse_bid_award(reply, candidate_ids)
    if award is not None:
        return award
    for fragment in collect_turn_reply_fragments(
        db,
        tenant_id=team.tenant_id,
        session_id=session_id,
        client_turn_id=client_turn_id,
    ):
        award = parse_bid_award(fragment, candidate_ids)
        if award is not None:
            return award
    return None


def _parse_bid_scores_with_fragments(
    db: Session,
    *,
    team: Team,
    session_id: str,
    client_turn_id: str,
    reply: str,
    candidate_ids: set[str],
) -> dict | None:
    """打分块解析:最终回复优先,缺失时回退 frame 级 reply_fragment 并集。"""
    scores = parse_bid_scores(reply, candidate_ids)
    if scores is not None:
        return scores
    for fragment in collect_turn_reply_fragments(
        db,
        tenant_id=team.tenant_id,
        session_id=session_id,
        client_turn_id=client_turn_id,
    ):
        scores = parse_bid_scores(fragment, candidate_ids)
        if scores is not None:
            return scores
    return None


def _execute_bid_judge(
    db: Session, event: TeamWakeEvent, team: Team, agent: AgentProfile
) -> None:
    """TL 竞标裁决入口:按 payload.mode 分发——score(每轮打分)/ award(最终裁决)。"""
    task_id = str(event.payload_json.get("task_id") or "")
    task = db.get(TeamTask, task_id)
    if task is None or task.team_id != team.id:
        raise RuntimeError("唤醒事件关联的团队任务不存在")
    if task.status != "bidding":
        # 任务已被人改判,迟到的裁决唤醒直接跳过
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="system",
            actor_id=None,
            event_type="bid_skipped",
            payload={"wake_event_id": event.id, "task_status": task.status},
        )
        db.commit()
        return
    mode = str(event.payload_json.get("mode") or "award")
    if mode == "score":
        _execute_bid_score(db, event, team, agent, task)
        return
    _execute_bid_award(db, event, team, agent, task)


def _execute_bid_score(
    db: Session, event: TeamWakeEvent, team: Team, agent: AgentProfile, task: TeamTask
) -> None:
    """TL 每轮打分执行体:分数写回该轮 bid,血条归零审计淘汰,再推进竞标。

    打分解析失败补一次格式纠错 turn;再失败全员记兜底分(5 分)并审计
    bid_score_fallback,不阻塞流程。
    """
    round_ = int(event.payload_json.get("round") or 1)
    round_bids = list(
        db.exec(
            select(TeamTaskBid)
            .where(TeamTaskBid.task_id == task.id, TeamTaskBid.round == round_)
            .order_by(TeamTaskBid.created_at)
        ).all()
    )
    if not round_bids:
        # 本轮无人应标(全部失败):无分可打,直接推进
        _maybe_advance_bidding(db, team, task)
        return
    if all(bid.score is not None for bid in round_bids):
        # 重复触发:本轮已打分,直接跳过
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="system",
            actor_id=None,
            event_type="bid_skipped",
            payload={"wake_event_id": event.id, "reason": "duplicate_score", "round": round_},
        )
        db.commit()
        return
    candidate_ids = {bid.agent_id for bid in round_bids}
    session = ChatSession(
        id=new_id("session"),
        tenant_id=team.tenant_id,
        user_id=team.owner_user_id,
        agent_id=agent.id,
        title=f"团队竞标打分:{task.title}",
        status="active",
        team_id=team.id,
    )
    db.add(session)
    db.commit()
    message = build_bid_score_message(db, team, task, round_bids, round_=round_)
    reply = run_agent_turn(
        db,
        team=team,
        agent=agent,
        session_id=session.id,
        wake_event_id=event.id,
        message=message,
        interaction_mode="team_tl",
    )
    scores = _parse_bid_scores_with_fragments(
        db,
        team=team,
        session_id=session.id,
        client_turn_id=event.id,
        reply=reply,
        candidate_ids=candidate_ids,
    )
    if scores is None:
        # 未输出有效打分块:带完整上下文补一次格式纠错 turn
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=agent.id,
            event_type="bid_score_unparsed",
            payload={"wake_event_id": event.id, "round": round_},
        )
        db.commit()
        repair_reply = run_agent_turn(
            db,
            team=team,
            agent=agent,
            session_id=session.id,
            wake_event_id=event.id,
            client_turn_id=f"{event.id}-repair",
            message=f"{message}\n\n{TL_BID_SCORE_REPAIR_MESSAGE}",
            interaction_mode="team_tl",
        )
        scores = _parse_bid_scores_with_fragments(
            db,
            team=team,
            session_id=session.id,
            client_turn_id=f"{event.id}-repair",
            reply=repair_reply,
            candidate_ids=candidate_ids,
        )
    if scores is None:
        # 纠错后仍无有效打分:全员记兜底分,审计兜底,不阻塞竞标流程
        scores = {}
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="system",
            actor_id=None,
            event_type="bid_score_fallback",
            payload={"wake_event_id": event.id, "round": round_, "score": BID_SCORE_FALLBACK},
        )
    for bid in round_bids:
        scored = scores.get(bid.agent_id) or {"score": BID_SCORE_FALLBACK, "rationale": ""}
        bid.score = scored["score"]
        bid.score_rationale = scored["rationale"] or None
        db.add(bid)
    record_task_event(
        db,
        team_id=team.id,
        task_id=task.id,
        actor_type="agent",
        actor_id=agent.id,
        event_type="bid_scored",
        payload={
            "wake_event_id": event.id,
            "round": round_,
            "scores": {bid.agent_id: bid.score for bid in round_bids},
        },
    )
    # 血条归零淘汰审计(同一候选只记一次)
    all_bids = list(db.exec(select(TeamTaskBid).where(TeamTaskBid.task_id == task.id)).all())
    hp = candidate_hp(all_bids)
    eliminated_rows = db.exec(
        select(TeamTaskEvent).where(
            TeamTaskEvent.task_id == task.id, TeamTaskEvent.event_type == "bid_eliminated"
        )
    ).all()
    already_eliminated = {str(row.actor_id) for row in eliminated_rows if row.actor_id}
    for agent_id, value in hp.items():
        if value > 0 or agent_id in already_eliminated:
            continue
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=agent_id,
            event_type="bid_eliminated",
            payload={"round": round_, "hp": value, "wake_event_id": event.id},
        )
    db.commit()
    _maybe_advance_bidding(db, team, task)


def _execute_bid_award(
    db: Session, event: TeamWakeEvent, team: Team, agent: AgentProfile, task: TeamTask
) -> None:
    """TL 最终裁决执行体:末轮打分写回,中标者(须在存活候选中)走 task_assigned 链路。"""
    candidates = _bidding_candidates(db, task)
    bids = list(
        db.exec(
            select(TeamTaskBid)
            .where(TeamTaskBid.task_id == task.id)
            .order_by(TeamTaskBid.round, TeamTaskBid.created_at)
        ).all()
    )
    alive = _alive_bid_candidates(candidates, bids)
    if not alive:
        # 存活候选为空(全部淘汰/无人应标):升级给人,不静默丢任务
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": "竞标无存活候选,无法裁决", "wake_event_id": event.id},
        )
        db.commit()
        return
    session = ChatSession(
        id=new_id("session"),
        tenant_id=team.tenant_id,
        user_id=team.owner_user_id,
        agent_id=agent.id,
        title=f"团队竞标裁决:{task.title}",
        status="active",
        team_id=team.id,
    )
    db.add(session)
    db.commit()
    message = build_bid_judge_message(db, team, task, bids, alive)
    reply = run_agent_turn(
        db,
        team=team,
        agent=agent,
        session_id=session.id,
        wake_event_id=event.id,
        message=message,
        interaction_mode="team_tl",
    )
    award = _parse_bid_award_with_fragments(
        db,
        team=team,
        session_id=session.id,
        client_turn_id=event.id,
        reply=reply,
        candidate_ids=set(alive),
    )
    if award is None:
        # 未输出有效裁决块(含 winner 不在存活候选):带完整上下文补一次格式纠错 turn
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=agent.id,
            event_type="bid_award_unparsed",
            payload={"wake_event_id": event.id},
        )
        db.commit()
        repair_reply = run_agent_turn(
            db,
            team=team,
            agent=agent,
            session_id=session.id,
            wake_event_id=event.id,
            client_turn_id=f"{event.id}-repair",
            message=f"{message}\n\n{TL_BID_JUDGE_REPAIR_MESSAGE}",
            interaction_mode="team_tl",
        )
        award = _parse_bid_award_with_fragments(
            db,
            team=team,
            session_id=session.id,
            client_turn_id=f"{event.id}-repair",
            reply=repair_reply,
            candidate_ids=set(alive),
        )
    if award is None:
        # 纠错后仍无有效裁决:升级给人,不静默丢任务
        apply_task_transition(
            db,
            task,
            "escalated",
            actor_type="system",
            actor_id=None,
            event_type="task_escalated",
            payload={"reason": "TL 竞标裁决失败", "wake_event_id": event.id},
        )
        db.commit()
        return
    winner = award["winner_agent_id"]
    for bid in bids:
        # 各轮打分已在 score 模式写回,裁决分数只补未打分的 bid(通常是末轮)
        if bid.score is not None:
            continue
        scored = award["scores"].get(bid.agent_id)
        if scored is None:
            continue
        bid.score = scored["score"]
        bid.score_rationale = scored["rationale"] or None
        db.add(bid)
    task.assignee_agent_id = winner
    apply_task_transition(
        db,
        task,
        "pending",
        actor_type="agent",
        actor_id=agent.id,
        event_type="task_awarded",
        payload={
            "winner_agent_id": winner,
            "comment": award["comment"],
            "wake_event_id": event.id,
        },
    )
    db.add(task)
    db.commit()
    wake = enqueue_wake_event(
        db,
        team=team,
        target_agent_id=winner,
        trigger_type="task_assigned",
        payload={"task_id": task.id},
    )
    db.commit()
    start_wakeup_async(wake.id)


# ---------- TL 对话轮次后处理(tl_chat 端点与主聊天端共用) ----------


def process_tl_reply(
    db: Session,
    *,
    team: Team,
    session: ChatSession,
    user: User,
    user_message: str,
    reply: str,
    client_turn_id: str | None,
) -> list[TeamTask]:
    """TL 对话轮次后处理:解析派任务块并创建任务(直派唤醒/投池竞标)。

    最终回复被 ResponseGenerator 改写时回退 frame 级 reply_fragment;
    意在派发但未输出代码块时,带完整上下文补一次格式纠错 turn。
    返回本次创建的任务列表(纯对话时为空)。
    """
    leader = get_team_leader(db, team.id)
    if leader is None:
        return []
    tl_agent = db.get(AgentProfile, leader.agent_id)
    if tl_agent is None:
        return []
    assignments = parse_tl_task_assignments(reply)
    if not assignments:
        # 最终回复被 ResponseGenerator 改写时,JSON 块可能只存在于 frame 级输出
        for fragment in collect_turn_reply_fragments(
            db,
            tenant_id=team.tenant_id,
            session_id=session.id,
            client_turn_id=client_turn_id or "",
        ):
            assignments = parse_tl_task_assignments(fragment)
            if assignments:
                break
    if not assignments and looks_like_dispatch_intent(user_message, reply):
        # TL 口头声称派发但未输出 JSON 代码块:带完整上下文补一次格式纠错 turn
        repair_turn = ChatTurnRequest(
            tenant_id=team.tenant_id,
            session_id=session.id,
            agent_id=tl_agent.id,
            client_turn_id=new_id("teamturn"),
            user_id=user.id,
            message=TL_ASSIGNMENT_REPAIR_MESSAGE,
            context_injection=build_tl_chat_message(db, team, user_message),
            message_visibility="internal",
            channel="team",
            interaction_mode="team_tl",
        )
        repair_reply = AgentLoop(db).handle_turn(repair_turn).reply or ""
        repair_assignments = parse_tl_task_assignments(repair_reply)
        if not repair_assignments:
            for fragment in collect_turn_reply_fragments(
                db,
                tenant_id=team.tenant_id,
                session_id=session.id,
                client_turn_id=repair_turn.client_turn_id,
            ):
                repair_assignments = parse_tl_task_assignments(fragment)
                if repair_assignments:
                    break
        if repair_assignments:
            assignments = repair_assignments
    member_ids = {item.agent_id for item in list_team_members(db, team.id)}
    created: list[TeamTask] = []
    wake_ids: list[str] = []
    bidding_tasks: list[TeamTask] = []
    for item in assignments:
        assignee = item.get("assignee_agent_id") or ""
        if assignee and assignee not in member_ids:
            # TL 指派的 agent 不在花名册:跳过,不建任务
            continue
        task = TeamTask(
            team_id=team.id,
            tenant_id=team.tenant_id,
            title=item["title"],
            description=item.get("description"),
            status="pending",
            created_by_user_id=user.id,
            created_by_tl=True,
            assignee_agent_id=assignee or None,
        )
        db.add(task)
        db.flush()
        record_task_event(
            db,
            team_id=team.id,
            task_id=task.id,
            actor_type="agent",
            actor_id=tl_agent.id,
            event_type="task_created",
            payload={"title": task.title, "assignee_agent_id": assignee or None},
        )
        if assignee:
            wake = enqueue_wake_event(
                db,
                team=team,
                target_agent_id=assignee,
                trigger_type="task_assigned",
                payload={"task_id": task.id},
            )
            wake_ids.append(wake.id)
        else:
            # 未指定负责人:投入任务池,走竞标流程
            bidding_tasks.append(task)
        created.append(task)
    db.commit()
    for task in created:
        db.refresh(task)
    for wake_id in wake_ids:
        start_wakeup_async(wake_id)
    for task in bidding_tasks:
        start_bidding(db, team, task)
    return created
