from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.models import (
    AgentProfile,
    ChatSession,
    Team,
    TeamBlackboardEntry,
    TeamMember,
    TeamRun,
    TeamTask,
    TeamTaskBid,
    TeamTaskEvent,
    TeamWakeEvent,
    utc_now,
)
from app.session.cleanup import (
    purge_chat_session_records,
    remove_chat_session_workspace,
)

TEAM_MEMBER_ROLES = {"leader", "member"}

# 任务状态机:bidding 由竞标流程驱动,bidding -> pending 为中标/改判后待执行。
TASK_STATUSES = {
    "blocked",
    "pending",
    "bidding",
    "in_progress",
    "review",
    "done",
    "rework",
    "escalated",
}
TASK_TRANSITIONS: dict[str, set[str]] = {
    "blocked": {"pending", "escalated"},
    "pending": {"bidding", "in_progress", "escalated"},
    "in_progress": {"review", "escalated"},
    "review": {"done", "rework", "escalated"},
    "rework": {"in_progress", "escalated"},
    "bidding": {"pending", "escalated"},
    "done": set(),
    "escalated": set(),
}

# 人/TL 验收结论 -> 目标状态
VERDICT_TARGET_STATUS = {"approve": "done", "rework": "rework", "escalate": "escalated"}

_JSON_BLOCK_RE = re.compile(r"```json\s*\n?(.*?)```", re.DOTALL)


class TeamTaskTransitionError(ValueError):
    pass


def extract_json_blocks(text: str) -> list[dict[str, Any]]:
    """从回复中提取所有 ```json 围栏代码块,坏 JSON 块直接跳过。"""
    blocks: list[dict[str, Any]] = []
    for match in _JSON_BLOCK_RE.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            blocks.append(data)
    return blocks


def strip_json_blocks(text: str) -> str:
    """剔除回复中的 ```json 围栏块,用于对人展示。"""
    return _JSON_BLOCK_RE.sub("", text or "").strip()


def strip_team_control_blocks(text: str) -> str:
    """只隐藏团队运行控制块，保留普通 JSON 代码示例供人阅读。"""

    def replace(match: re.Match[str]) -> str:
        try:
            value = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            return match.group(0)
        if isinstance(value, dict) and any(
            key in value
            for key in ("team_tasks", "team_review", "blackboard_suggestions", "bid", "bid_scores", "bid_award")
        ):
            return ""
        return match.group(0)

    return _JSON_BLOCK_RE.sub(replace, text or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def parse_tl_task_assignments(reply: str) -> list[dict[str, Any]]:
    """解析 TL 派任务块，并保留稳定引用、依赖与通用激活条件。

    assignee_agent_id 缺省/为空表示投入任务池由成员竞标;无块或结构非法时
    返回空列表(纯对话,不改状态)。
    """
    tasks: list[dict[str, Any]] = []
    for block in extract_json_blocks(reply):
        raw_tasks = block.get("team_tasks")
        if not isinstance(raw_tasks, list):
            continue
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            task: dict[str, Any] = {"title": title}
            assignee = str(item.get("assignee_agent_id") or "").strip()
            if assignee:
                task["assignee_agent_id"] = assignee
            description = str(item.get("description") or "").strip()
            if description:
                task["description"] = description
            client_ref = str(item.get("client_ref") or "").strip()
            if client_ref:
                task["client_ref"] = client_ref
            depends_on = _string_list(item.get("depends_on"))
            if depends_on:
                task["depends_on"] = depends_on
            depends_on_task_ids = _string_list(item.get("depends_on_task_ids"))
            if depends_on_task_ids:
                task["depends_on_task_ids"] = depends_on_task_ids
            condition = item.get("activation_condition")
            if isinstance(condition, str):
                condition = {"type": condition}
            if isinstance(condition, dict):
                task["activation_condition"] = dict(condition)
            tasks.append(task)
    return tasks


def task_activation_state(db: Session, task: TeamTask) -> str:
    """计算 blocked 任务的通用激活结果:ready / blocked / impossible。"""
    dependency_ids = list(task.depends_on_task_ids_json or [])
    if not dependency_ids:
        return "ready"
    dependencies = [db.get(TeamTask, task_id) for task_id in dependency_ids]
    if any(item is None or item.team_id != task.team_id for item in dependencies):
        return "impossible"
    rows = [item for item in dependencies if item is not None]
    statuses = [item.status for item in rows]
    # escalated 也承载“等待用户补充信息”这一可恢复状态，不能把它当成依赖终态。
    waiting_for_input = [
        item.status == "escalated" and bool((item.report_json or {}).get("needs_input"))
        for item in rows
    ]
    terminal = [
        status == "done" or (status == "escalated" and not waiting)
        for status, waiting in zip(statuses, waiting_for_input, strict=True)
    ]
    succeeded = sum(status == "done" for status in statuses)
    condition = dict(task.activation_condition_json or {})
    condition_type = str(condition.get("type") or "all_succeeded")
    if condition_type == "any_succeeded":
        if succeeded:
            return "ready"
        return "impossible" if all(terminal) else "blocked"
    if condition_type == "minimum_succeeded":
        try:
            minimum = max(1, min(len(rows), int(condition.get("minimum") or 1)))
        except (TypeError, ValueError):
            minimum = 1
        if succeeded >= minimum:
            return "ready"
        remaining = sum(not item_terminal for item_terminal in terminal)
        return "impossible" if succeeded + remaining < minimum else "blocked"
    if condition_type == "all_terminal":
        return "ready" if all(terminal) else "blocked"
    if succeeded == len(statuses):
        return "ready"
    if any(
        item_terminal and status != "done"
        for item_terminal, status in zip(terminal, statuses, strict=True)
    ):
        return "impossible"
    return "blocked"


def parse_bid(reply: str) -> dict[str, str] | None:
    """解析竞标块:{"bid": {"plan": "...", "estimated_cost"?: ..., "confidence"?: ...}}。

    无块或 plan 为空时返回 None(调用方以整条回复作为竞标内容兜底)。
    """
    for block in extract_json_blocks(reply):
        raw = block.get("bid")
        if not isinstance(raw, dict):
            continue
        plan = str(raw.get("plan") or "").strip()
        if not plan:
            continue
        bid: dict[str, str] = {"plan": plan}
        for key in ("estimated_cost", "confidence"):
            value = str(raw.get(key) or "").strip()
            if value:
                bid[key] = value
        return bid
    return None


def parse_bid_scores(reply: str, candidate_ids: set[str]) -> dict[str, dict[str, Any]] | None:
    """解析 TL 每轮打分块:{"bid_scores": {"agent_id": {"score": 8.5, "rationale": "..."}}}。

    分数截断到 0-10;至少一名候选有合法分数才视为解析成功,
    否则返回 None(交由格式纠错重试)。
    """
    for block in extract_json_blocks(reply):
        raw = block.get("bid_scores")
        if not isinstance(raw, dict):
            continue
        scores: dict[str, dict[str, Any]] = {}
        for agent_id, item in raw.items():
            if str(agent_id) not in candidate_ids or not isinstance(item, dict):
                continue
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            scores[str(agent_id)] = {
                "score": min(10.0, max(0.0, score)),
                "rationale": str(item.get("rationale") or "").strip(),
            }
        if scores:
            return scores
    return None


def parse_bid_award(reply: str, candidate_ids: set[str]) -> dict[str, Any] | None:
    """解析 TL 竞标裁决块:{"bid_award": {"winner_agent_id", "scores"?, "comment"?}}。

    winner 必须来自候选集,否则视为未解析(交由格式纠错重试)。
    """
    for block in extract_json_blocks(reply):
        raw = block.get("bid_award")
        if not isinstance(raw, dict):
            continue
        winner = str(raw.get("winner_agent_id") or "").strip()
        if winner not in candidate_ids:
            continue
        scores: dict[str, dict[str, Any]] = {}
        raw_scores = raw.get("scores")
        if isinstance(raw_scores, dict):
            for agent_id, item in raw_scores.items():
                if not isinstance(item, dict):
                    continue
                try:
                    score = float(item.get("score"))
                except (TypeError, ValueError):
                    continue
                scores[str(agent_id)] = {
                    "score": score,
                    "rationale": str(item.get("rationale") or "").strip(),
                }
        return {
            "winner_agent_id": winner,
            "scores": scores,
            "comment": str(raw.get("comment") or "").strip(),
        }
    return None


def parse_tl_review(reply: str) -> dict[str, Any] | None:
    """解析 TL 验收块:{"team_review": {"verdict": "approve|rework|escalate", ...}}。

    无块或 verdict 非法时返回 None(不改状态)。team_review 里可选的
    blackboard_writes(TL 裁决认可的黑板条目)在存在时一并返回。
    """
    for block in extract_json_blocks(reply):
        raw_review = block.get("team_review")
        if not isinstance(raw_review, dict):
            continue
        verdict = str(raw_review.get("verdict") or "").strip()
        if verdict not in VERDICT_TARGET_STATUS:
            continue
        result: dict[str, Any] = {
            "verdict": verdict,
            "comment": str(raw_review.get("comment") or "").strip(),
        }
        raw_writes = raw_review.get("blackboard_writes")
        if isinstance(raw_writes, list):
            result["blackboard_writes"] = [
                item for item in raw_writes if isinstance(item, dict)
            ]
        return result
    return None


def parse_blackboard_suggestions(reply: str) -> list[dict[str, Any]]:
    """解析成员报告末尾的黑板建议块:{"blackboard_suggestions": [{content, tags?}...]}。"""
    suggestions: list[dict[str, Any]] = []
    for block in extract_json_blocks(reply):
        raw = block.get("blackboard_suggestions")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            suggestion: dict[str, Any] = {"content": content}
            tags = item.get("tags")
            if isinstance(tags, list):
                suggestion["tags"] = [str(tag) for tag in tags]
            suggestions.append(suggestion)
    return suggestions


def get_team(db: Session, tenant_id: str, team_id: str) -> Team:
    team = db.get(Team, team_id)
    if team is None or team.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


def list_team_members(db: Session, team_id: str) -> list[TeamMember]:
    return list(db.exec(select(TeamMember).where(TeamMember.team_id == team_id)).all())


def get_team_leader(db: Session, team_id: str) -> TeamMember | None:
    return db.exec(
        select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.role == "leader")
    ).first()


def create_team(
    db: Session,
    *,
    tenant_id: str,
    name: str,
    description: str | None,
    owner_user_id: str,
    config: dict[str, Any] | None = None,
) -> Team:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Team name cannot be empty")
    existing = db.exec(
        select(Team).where(Team.tenant_id == tenant_id, Team.name == name)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Team name already exists")
    team = Team(
        tenant_id=tenant_id,
        name=name,
        description=description,
        owner_user_id=owner_user_id,
        config_json=dict(config or {}),
        status="active",
    )
    db.add(team)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Team name already exists") from exc
    db.refresh(team)
    return team


def delete_team(db: Session, team: Team) -> None:
    """删除团队并级联清理成员/运行/任务/竞标/审计/唤醒事件/黑板/团队会话。"""
    members = list_team_members(db, team.id)
    runs = list(db.exec(select(TeamRun).where(TeamRun.team_id == team.id)).all())
    tasks = list(db.exec(select(TeamTask).where(TeamTask.team_id == team.id)).all())
    bids = list(db.exec(select(TeamTaskBid).where(TeamTaskBid.team_id == team.id)).all())
    events = list(db.exec(select(TeamTaskEvent).where(TeamTaskEvent.team_id == team.id)).all())
    wakes = list(db.exec(select(TeamWakeEvent).where(TeamWakeEvent.team_id == team.id)).all())
    entries = list(
        db.exec(select(TeamBlackboardEntry).where(TeamBlackboardEntry.team_id == team.id)).all()
    )
    sessions = list(
        db.exec(select(ChatSession).where(ChatSession.team_id == team.id)).all()
    )
    workspace_keys = [(session.tenant_id, session.id) for session in sessions]
    for row in [*members, *runs, *tasks, *bids, *events, *wakes, *entries]:
        db.delete(row)
    for session in sessions:
        purge_chat_session_records(db, session)
    db.delete(team)
    db.commit()
    for tenant_id, session_id in workspace_keys:
        remove_chat_session_workspace(tenant_id=tenant_id, session_id=session_id, db=db)


def _ensure_team_agent(db: Session, team: Team, agent_id: str) -> AgentProfile:
    agent = db.get(AgentProfile, agent_id)
    if agent is None or agent.tenant_id != team.tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found in this tenant")
    if agent.status != "active":
        raise HTTPException(status_code=400, detail="Agent is not active")
    return agent


def add_member(db: Session, team: Team, *, agent_id: str, role: str = "member") -> TeamMember:
    if role not in TEAM_MEMBER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid team member role: {role}")
    _ensure_team_agent(db, team, agent_id)
    existing = db.exec(
        select(TeamMember).where(
            TeamMember.team_id == team.id, TeamMember.agent_id == agent_id
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Agent is already a team member")
    member = TeamMember(team_id=team.id, agent_id=agent_id, role="member")
    db.add(member)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Agent is already a team member") from exc
    db.refresh(member)
    if role == "leader":
        # 一个团队至多一名 TL:新 leader 上任即换任
        set_leader(db, team, agent_id)
        db.refresh(member)
    return member


def remove_member(db: Session, team: Team, agent_id: str) -> None:
    member = db.exec(
        select(TeamMember).where(
            TeamMember.team_id == team.id, TeamMember.agent_id == agent_id
        )
    ).first()
    if member is None:
        raise HTTPException(status_code=404, detail="Team member not found")
    db.delete(member)
    db.commit()


def set_leader(db: Session, team: Team, agent_id: str) -> TeamMember:
    """换任 TL:原 leader 降为 member,保证一个团队至多一名 leader。"""
    member = db.exec(
        select(TeamMember).where(
            TeamMember.team_id == team.id, TeamMember.agent_id == agent_id
        )
    ).first()
    if member is None:
        raise HTTPException(status_code=404, detail="Agent is not a team member")
    current = get_team_leader(db, team.id)
    if current and current.agent_id != agent_id:
        current.role = "member"
        db.add(current)
    member.role = "leader"
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


def record_task_event(
    db: Session,
    *,
    team_id: str,
    task_id: str,
    actor_type: str,
    actor_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> TeamTaskEvent:
    event = TeamTaskEvent(
        task_id=task_id,
        team_id=team_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        payload_json=dict(payload or {}),
    )
    db.add(event)
    return event


def apply_task_transition(
    db: Session,
    task: TeamTask,
    new_status: str,
    *,
    actor_type: str,
    actor_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> TeamTask:
    """按状态机迁移任务状态并写审计;非法流转抛 TeamTaskTransitionError。"""
    if new_status not in TASK_STATUSES:
        raise TeamTaskTransitionError(f"未知任务状态: {new_status}")
    planner_run_completed = bool(
        task.team_run_id and task.status == "in_progress" and new_status == "done"
    )
    if (
        new_status != task.status
        and new_status not in TASK_TRANSITIONS.get(task.status, set())
        and not planner_run_completed
    ):
        raise TeamTaskTransitionError(f"任务不允许从 {task.status} 流转到 {new_status}")
    previous = task.status
    task.status = new_status
    task.version += 1
    task.updated_at = utc_now()
    db.add(task)
    record_task_event(
        db,
        team_id=task.team_id,
        task_id=task.id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        payload={"from_status": previous, "to_status": new_status, **dict(payload or {})},
    )
    return task


def team_roster_lines(db: Session, team: Team) -> list[str]:
    """团队花名册文本行:agent_id、名称、角色、能力标签,供注入 TL/成员上下文。"""
    lines: list[str] = []
    for member in list_team_members(db, team.id):
        agent = db.get(AgentProfile, member.agent_id)
        if agent is None:
            continue
        metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
        tags = metadata.get("expertise_tags")
        tags_text = ",".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
        role_text = "TL" if member.role == "leader" else "成员"
        line = f"- agent_id={agent.id} 名称={agent.name} 角色={role_text}"
        if tags_text:
            line += f" 能力标签={tags_text}"
        lines.append(line)
    return lines


def open_tasks_summary(db: Session, team: Team) -> list[str]:
    """当前未闭环任务摘要,供 TL 对话上下文。"""
    rows = db.exec(
        select(TeamTask).where(
            TeamTask.team_id == team.id,
            TeamTask.status.in_(["blocked", "pending", "in_progress", "review", "rework"]),
        )
    ).all()
    result: list[str] = []
    for row in rows:
        line = (
            f"- task_id={row.id} 标题={row.title} 状态={row.status} "
            f"负责人={row.assignee_agent_id or '未指派'}"
        )
        dependencies = list(row.depends_on_task_ids_json or [])
        if dependencies:
            line += f" 前置任务={','.join(dependencies)} 激活条件={row.activation_condition_json or {}}"
        result.append(line)
    return result


# ---------- 团队黑板 ----------

BLACKBOARD_SOURCE_TYPES = {"member", "leader", "human"}
BLACKBOARD_STATUSES = {"active", "archived"}
BLACKBOARD_INJECTION_LIMIT = 10


def normalize_blackboard_content(content: Any) -> str:
    """黑板内容规范化:压缩全部空白,供去重/合并比较。"""
    return " ".join(str(content or "").split())


def normalize_blackboard_tags(tags: Any) -> list[str]:
    """黑板标签规范化:小写、去空白、去重,只保留字符串。"""
    if not isinstance(tags, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        text = str(tag).strip().lower()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def write_blackboard_entries(
    db: Session,
    *,
    team: Team,
    entries: list[dict[str, Any]],
    source_type: str,
    source_agent_id: str | None = None,
    source_task_id: str | None = None,
) -> tuple[list[TeamBlackboardEntry], list[str]]:
    """轻量黑板写入流水线:规范化 -> 去重合并 -> 结构化写入(带引用)。

    黑板是活文档:与同团队既有 active 条目完全相同或为其子串时不新增;
    新内容是既有条目超集时合并更新既有条目(content/tags/citation/updated_at)。
    调用方负责 commit。返回 (写入/更新的条目列表, 跳过原因列表)。
    """
    if source_type not in BLACKBOARD_SOURCE_TYPES:
        raise ValueError(f"未知黑板来源类型: {source_type}")
    citation: dict[str, Any] = {}
    if source_task_id:
        citation["task_id"] = source_task_id
        task = db.get(TeamTask, source_task_id)
        if task is not None:
            citation["task_title"] = task.title
    existing_rows = list(
        db.exec(
            select(TeamBlackboardEntry).where(
                TeamBlackboardEntry.team_id == team.id,
                TeamBlackboardEntry.status == "active",
            )
        ).all()
    )
    existing_by_norm = {normalize_blackboard_content(row.content): row for row in existing_rows}
    written: list[TeamBlackboardEntry] = []
    skipped: list[str] = []
    seen_in_batch: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            skipped.append("条目结构非法,已跳过")
            continue
        content = normalize_blackboard_content(raw.get("content"))
        if not content:
            skipped.append("内容为空,已跳过")
            continue
        if content in seen_in_batch:
            skipped.append(f"与本批次内其他条目重复: {content[:50]}")
            continue
        seen_in_batch.add(content)
        tags = normalize_blackboard_tags(raw.get("tags"))
        if content in existing_by_norm:
            skipped.append(f"与黑板既有条目重复: {content[:50]}")
            continue
        # 子串关系:新内容是既有条目的子串 -> 跳过;既有条目是新内容的子串 -> 合并更新
        is_substring = any(content in norm for norm in existing_by_norm)
        if is_substring:
            skipped.append(f"黑板已有更完整条目: {content[:50]}")
            continue
        superseded: TeamBlackboardEntry | None = None
        superseded_norm = ""
        for norm, row in existing_by_norm.items():
            if norm in content and len(norm) > len(superseded_norm):
                superseded = row
                superseded_norm = norm
        if superseded is not None:
            superseded.content = content
            superseded.tags_json = normalize_blackboard_tags([*superseded.tags_json, *tags])
            if citation:
                superseded.citation_json = dict(citation)
            superseded.updated_at = utc_now()
            db.add(superseded)
            del existing_by_norm[superseded_norm]
            existing_by_norm[content] = superseded
            written.append(superseded)
            continue
        entry = TeamBlackboardEntry(
            team_id=team.id,
            tenant_id=team.tenant_id,
            content=content,
            tags_json=tags,
            source_type=source_type,
            source_agent_id=source_agent_id,
            source_task_id=source_task_id,
            citation_json=dict(citation),
        )
        db.add(entry)
        existing_by_norm[content] = entry
        written.append(entry)
    return written, skipped


def blackboard_context_lines(
    db: Session, team: Team, query_text: str, *, limit: int = BLACKBOARD_INJECTION_LIMIT
) -> list[str]:
    """团队黑板 top-K 注入行:按 query 与条目 tags 的关键词重叠打分,

    再按 pinned 优先、updated_at 倒序;无 active 条目时返回空列表(不注入该区)。
    """
    rows = list(
        db.exec(
            select(TeamBlackboardEntry).where(
                TeamBlackboardEntry.team_id == team.id,
                TeamBlackboardEntry.status == "active",
            )
        ).all()
    )
    if not rows:
        return []
    query = (query_text or "").lower()

    def sort_key(entry: TeamBlackboardEntry) -> tuple[int, bool, float]:
        score = sum(
            1 for tag in entry.tags_json if isinstance(tag, str) and tag and tag in query
        )
        return (-score, not entry.pinned, -entry.updated_at.timestamp())

    rows.sort(key=sort_key)
    lines: list[str] = []
    for entry in rows[: max(1, limit)]:
        tags_text = ",".join(entry.tags_json)
        lines.append(f"- [{tags_text}] {entry.content}" if tags_text else f"- {entry.content}")
    return lines


# ---------- 任务池竞标 ----------

DEFAULT_MEMBER_CONCURRENCY = 1


def member_concurrency(team: Team) -> int:
    """团队成员执行并发上限:默认 1(同团队内同一成员串行),非法配置回退默认。"""
    config = team.config_json if isinstance(team.config_json, dict) else {}
    try:
        return max(1, int(config.get("member_concurrency", DEFAULT_MEMBER_CONCURRENCY)))
    except (TypeError, ValueError):
        return DEFAULT_MEMBER_CONCURRENCY


BID_CANDIDATE_LIMIT = 3
# 竞标总轮数:round 1 = 陈述,round 2..N = 反驳;默认 3(1 陈述 + 2 反驳)。
# 0/1 均表示关闭辩论,陈述后直接由 TL 裁决(兼容旧配置读取)。
DEFAULT_BID_REBUTTAL_ROUNDS = 3

# 竞标血条:HP 初始 100,每轮结束后按 TL 打分扣减 (10 - 得分) x 3,下限 0,归零淘汰
BID_HP_INITIAL = 100
BID_HP_LOSS_PER_POINT = 3
# TL 打分解析失败(含纠错重试)时的兜底分,不阻塞竞标流程
BID_SCORE_FALLBACK = 5.0


def candidate_hp(bids: list[TeamTaskBid]) -> dict[str, int]:
    """由各轮打分计算候选血条:HP = 100 - Σ(10 - 每轮得分) x 3,下限 0。

    只统计已打分(score 非空)的 bid;未打分候选不在返回中(调用方按初始 HP 处理)。
    """
    hp: dict[str, float] = {}
    for bid in sorted(bids, key=lambda item: (item.round, item.created_at)):
        if bid.score is None:
            continue
        current = hp.get(bid.agent_id, float(BID_HP_INITIAL))
        loss = max(0.0, 10.0 - bid.score) * BID_HP_LOSS_PER_POINT
        hp[bid.agent_id] = max(0.0, current - loss)
    return {agent_id: round(value) for agent_id, value in hp.items()}


def bid_rebuttal_rounds(team: Team) -> int:
    """团队配置的竞标总轮数:默认 3,0/1 表示关闭辩论、陈述后直接裁决。"""
    config = team.config_json if isinstance(team.config_json, dict) else {}
    try:
        return max(0, int(config.get("bid_rebuttal_rounds", DEFAULT_BID_REBUTTAL_ROUNDS)))
    except (TypeError, ValueError):
        return DEFAULT_BID_REBUTTAL_ROUNDS


def select_bid_candidates(db: Session, team: Team, task: TeamTask) -> list[str]:
    """选竞标候选:按成员 expertise_tags 与任务文本的子串重叠计分,排除 TL,封顶 3 人。

    全部 0 分时取除 TL 外全部成员(仍封顶 3);无成员可选时返回空列表。
    """
    leader = get_team_leader(db, team.id)
    leader_agent_id = leader.agent_id if leader else None
    query = f"{task.title}\n{task.description or ''}".lower()
    scored: list[tuple[int, str]] = []
    for member in list_team_members(db, team.id):
        if member.agent_id == leader_agent_id:
            continue
        agent = db.get(AgentProfile, member.agent_id)
        if agent is None:
            continue
        metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
        raw_tags = metadata.get("expertise_tags")
        tags = [str(tag).lower() for tag in raw_tags] if isinstance(raw_tags, list) else []
        score = sum(1 for tag in tags if tag and tag in query)
        scored.append((score, member.agent_id))
    if not scored:
        return []
    scored.sort(key=lambda item: (-item[0], item[1]))
    positive = [agent_id for score, agent_id in scored if score > 0]
    pool = positive if positive else [agent_id for _, agent_id in scored]
    return pool[:BID_CANDIDATE_LIMIT]
