from __future__ import annotations

import base64
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel import Session, select

from app.api.sessions import (
    _json_download_response,
    _safe_filename_part,
    _session_details_payload,
)
from app.async_jobs import enqueue_async_job
from app.core import AgentLoop
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    KnowledgeBase,
    KnowledgeBaseVersion,
    Message,
    Team,
    TeamBlackboardEntry,
    TeamKnowledgeBaseBinding,
    TeamKnowledgeBaseGrant,
    TeamTask,
    TeamTaskBid,
    TeamTaskEvent,
    TeamWakeEvent,
    User,
    new_id,
    utc_now,
)
from app.knowledge.errors import KnowledgeError
from app.knowledge.service import IngestPayload, KnowledgeService
from app.security.auth import get_current_user
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant
from app.session.message_read import message_read
from app.session.message_visibility import visible_message_content, visible_message_rows
from app.session.session_schema import ChatTurnRequest
from app.teams import service as team_service
from app.teams.schema import (
    AwardOverrideRequest,
    ReviewOverrideRequest,
    TeamBlackboardEntryArchiveRequest,
    TeamBlackboardEntryCreateRequest,
    TeamBlackboardEntryRead,
    TeamBlackboardEntryUpdateRequest,
    TeamBlackboardPromoteRequest,
    TeamBlackboardPromoteResponse,
    TeamBlackboardWriteResponse,
    TeamConversationKind,
    TeamConversationMessageRead,
    TeamConversationRead,
    TeamConversationsResponse,
    TeamConversationStreamRead,
    TeamConversationTLRead,
    TeamCreateRequest,
    TeamEventRead,
    TeamKnowledgeBindingRead,
    TeamKnowledgeBindingUpdateRequest,
    TeamKnowledgeBindRequest,
    TeamKnowledgeGrantRead,
    TeamKnowledgeGrantsUpdateRequest,
    TeamKnowledgeUnbindRequest,
    TeamLeaderUpdateRequest,
    TeamMemberAddRequest,
    TeamMemberRead,
    TeamRead,
    TeamTaskBidRead,
    TeamTaskCreateRequest,
    TeamTaskEventRead,
    TeamTaskRead,
    TeamTaskResumeRequest,
    TeamThreadRead,
    TeamTLChatRequest,
    TeamTLChatResponse,
    TeamTLSessionRequest,
    TeamTLSessionResponse,
    TeamUpdateRequest,
)
from app.teams.service import (
    VERDICT_TARGET_STATUS,
    add_member,
    apply_task_transition,
    create_team,
    delete_team,
    get_team,
    get_team_leader,
    list_team_knowledge_bindings,
    list_team_members,
    normalize_blackboard_content,
    normalize_blackboard_tags,
    record_task_event,
    remove_member,
    replace_team_knowledge_grants,
    set_leader,
    set_team_default_knowledge_base,
    strip_json_blocks,
    unbind_team_knowledge_base,
    write_blackboard_entries,
)
from app.teams.wakeup import (
    activate_ready_tasks,
    build_tl_chat_context,
    enqueue_wake_event,
    process_tl_reply,
    start_bidding,
    start_wakeup_async,
)

router = APIRouter(prefix="/api/enterprise/teams", tags=["enterprise:teams"])

TEAM_LOG_EXPORT_SCHEMA = "staffdeck.team-conversation-log.v1"

# 可被人改判的任务状态:TL 验收后(review)或已升级(escalated)
OVERRIDABLE_STATUSES = {"review", "escalated"}

# 可被人推翻判罚(改派中标者)的任务状态:竞标中(bidding)或执行开始前(pending)
AWARD_OVERRIDABLE_STATUSES = {"bidding", "pending"}


def _ensure_request_tenant(tenant_id: str, user: User) -> None:
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _ensure_team_manager(team: Team, user: User) -> None:
    """写操作权限:团队创建者(owner)或管理员。"""
    if team.owner_user_id != user.id and not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only team owner or administrator can manage this team")


def _member_read(db: Session, member) -> TeamMemberRead:
    agent = db.get(AgentProfile, member.agent_id)
    return TeamMemberRead(
        id=member.id,
        team_id=member.team_id,
        agent_id=member.agent_id,
        role=member.role,
        agent_name=agent.name if agent else None,
        created_at=member.created_at,
    )


def _team_read(db: Session, team: Team) -> TeamRead:
    """投影团队详情，并公开其当前默认共享写入目标。"""
    members = [_member_read(db, item) for item in list_team_members(db, team.id)]
    return TeamRead(
        id=team.id,
        tenant_id=team.tenant_id,
        name=team.name,
        description=team.description,
        owner_user_id=team.owner_user_id,
        default_knowledge_base_id=team.default_knowledge_base_id,
        config=dict(team.config_json or {}),
        status=team.status,
        members=members,
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


def _task_read(db: Session, task: TeamTask, *, with_events: bool = False) -> TeamTaskRead:
    events: list[TeamTaskEventRead] = []
    bids: list[TeamTaskBidRead] = []
    if with_events:
        rows = db.exec(
            select(TeamTaskEvent)
            .where(TeamTaskEvent.task_id == task.id)
            .order_by(TeamTaskEvent.created_at)
        ).all()
        events = [
            TeamTaskEventRead(
                id=row.id,
                task_id=row.task_id,
                team_id=row.team_id,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                event_type=row.event_type,
                payload=dict(row.payload_json or {}),
                created_at=row.created_at,
            )
            for row in rows
        ]
        bid_rows = db.exec(
            select(TeamTaskBid)
            .where(TeamTaskBid.task_id == task.id)
            .order_by(TeamTaskBid.round, TeamTaskBid.created_at)
        ).all()
        bids = [
            TeamTaskBidRead(
                id=row.id,
                task_id=row.task_id,
                agent_id=row.agent_id,
                agent_name=(agent.name if (agent := db.get(AgentProfile, row.agent_id)) else None),
                round=row.round,
                kind=row.kind,
                content=row.content,
                score=row.score,
                score_rationale=row.score_rationale,
                created_at=row.created_at,
            )
            for row in bid_rows
        ]
    return TeamTaskRead(
        id=task.id,
        team_id=task.team_id,
        tenant_id=task.tenant_id,
        team_run_id=task.team_run_id,
        source_turn_id=task.source_turn_id,
        parent_task_id=task.parent_task_id,
        title=task.title,
        description=task.description,
        priority=task.priority,
        status=task.status,
        created_by_user_id=task.created_by_user_id,
        created_by_tl=task.created_by_tl,
        assignee_agent_id=task.assignee_agent_id,
        session_id=task.session_id,
        depends_on_task_ids=list(task.depends_on_task_ids_json or []),
        activation_condition=dict(task.activation_condition_json or {}),
        report=dict(task.report_json or {}),
        review=dict(task.review_json or {}),
        version=task.version,
        events=events,
        bids=bids,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _get_team_task(db: Session, team: Team, task_id: str) -> TeamTask:
    task = db.get(TeamTask, task_id)
    if task is None or task.team_id != team.id:
        raise HTTPException(status_code=404, detail="Team task not found")
    return task


@router.post("", response_model=TeamRead)
def create_team_endpoint(
    request: TeamCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamRead:
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    try:
        team = create_team(
            db,
            tenant_id=request.tenant_id,
            name=request.name,
            description=request.description,
            owner_user_id=current_user.id,
            config=request.config,
            knowledge_bases=request.knowledge_bases,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    return _team_read(db, team)


def _knowledge_http_error(exc: KnowledgeError) -> HTTPException:
    """把共享知识库领域错误映射为稳定 HTTP 载荷，不暴露受保护内容。"""
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, **exc.details},
    )


def _team_knowledge_binding_read(
    db: Session,
    team: Team,
    binding: TeamKnowledgeBaseBinding,
) -> TeamKnowledgeBindingRead:
    """将绑定、正式版本与当前有效授权合并成管理界面读取模型。"""
    knowledge_base = db.get(KnowledgeBase, binding.knowledge_base_id)
    if knowledge_base is None or knowledge_base.tenant_id != team.tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    version = (
        db.get(KnowledgeBaseVersion, knowledge_base.published_version_id)
        if knowledge_base.published_version_id
        else None
    )
    grants = db.exec(
        select(TeamKnowledgeBaseGrant)
        .where(
            TeamKnowledgeBaseGrant.tenant_id == team.tenant_id,
            TeamKnowledgeBaseGrant.team_id == team.id,
            TeamKnowledgeBaseGrant.knowledge_base_id == knowledge_base.id,
            TeamKnowledgeBaseGrant.status == "active",
        )
        .order_by(TeamKnowledgeBaseGrant.agent_id)
    ).all()
    return TeamKnowledgeBindingRead(
        id=binding.id,
        team_id=team.id,
        knowledge_base_id=knowledge_base.id,
        knowledge_base_name=knowledge_base.name,
        status=binding.status,
        revision=binding.revision,
        is_default=team.default_knowledge_base_id == knowledge_base.id,
        published_version_id=knowledge_base.published_version_id,
        published_version=version.version if version else None,
        grants=[
            TeamKnowledgeGrantRead(
                agent_id=grant.agent_id,
                permission=grant.permission,
                status=grant.status,
            )
            for grant in grants
        ],
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


@router.get("/{team_id}/knowledge-bases", response_model=list[TeamKnowledgeBindingRead])
def list_team_knowledge_bases(
    team_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> list[TeamKnowledgeBindingRead]:
    """列出团队的共享知识绑定、默认目标、修订号与当前授权矩阵。"""
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    return [
        _team_knowledge_binding_read(db, team, binding)
        for binding in list_team_knowledge_bindings(db, team)
    ]


@router.post("/{team_id}/knowledge-bases", response_model=TeamKnowledgeBindingRead)
def bind_team_knowledge_base_endpoint(
    team_id: str,
    request: TeamKnowledgeBindRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> TeamKnowledgeBindingRead:
    """为现有团队新增一个共享知识库绑定，或在同一事务中新建并绑定。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    try:
        binding = team_service.bind_team_knowledge_base(
            db,
            team=team,
            selection=request,
            actor_user_id=current_user.id,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    return _team_knowledge_binding_read(db, team, binding)


@router.put(
    "/{team_id}/knowledge-bases/{knowledge_base_id}",
    response_model=TeamKnowledgeBindingRead,
)
def update_team_knowledge_binding_endpoint(
    team_id: str,
    knowledge_base_id: str,
    request: TeamKnowledgeBindingUpdateRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> TeamKnowledgeBindingRead:
    """按预期修订号设置或清除团队默认写入目标。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    try:
        binding = set_team_default_knowledge_base(
            db,
            team=team,
            knowledge_base_id=knowledge_base_id,
            is_default=request.is_default,
            expected_revision=request.expected_revision,
            actor_user_id=current_user.id,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    return _team_knowledge_binding_read(db, team, binding)


@router.delete(
    "/{team_id}/knowledge-bases/{knowledge_base_id}",
    response_model=TeamKnowledgeBindingRead,
)
def remove_team_knowledge_binding_endpoint(
    team_id: str,
    knowledge_base_id: str,
    request: TeamKnowledgeUnbindRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> TeamKnowledgeBindingRead:
    """撤销团队绑定、全部团队内授权及对应默认目标，但保留共享库本身。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    try:
        binding = unbind_team_knowledge_base(
            db,
            team=team,
            knowledge_base_id=knowledge_base_id,
            expected_revision=request.expected_revision,
            actor_user_id=current_user.id,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    return _team_knowledge_binding_read(db, team, binding)


@router.put(
    "/{team_id}/knowledge-bases/{knowledge_base_id}/grants",
    response_model=TeamKnowledgeBindingRead,
)
def replace_team_knowledge_grants_endpoint(
    team_id: str,
    knowledge_base_id: str,
    request: TeamKnowledgeGrantsUpdateRequest,
    db: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> TeamKnowledgeBindingRead:
    """按绑定修订号原子替换一个共享库的团队员工权限矩阵。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    try:
        binding = replace_team_knowledge_grants(
            db,
            team=team,
            knowledge_base_id=knowledge_base_id,
            expected_revision=request.expected_revision,
            grants={grant.agent_id: grant.permission for grant in request.grants},
            actor_user_id=current_user.id,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    return _team_knowledge_binding_read(db, team, binding)


@router.get("", response_model=list[TeamRead])
def list_teams(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[TeamRead]:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    rows = db.exec(
        select(Team).where(Team.tenant_id == tenant_id).order_by(Team.updated_at.desc())
    ).all()
    return [_team_read(db, row) for row in rows]


@router.get("/{team_id}", response_model=TeamRead)
def get_team_endpoint(
    team_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamRead:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    return _team_read(db, get_team(db, tenant_id, team_id))


@router.put("/{team_id}", response_model=TeamRead)
def update_team_endpoint(
    team_id: str,
    request: TeamUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamRead:
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Team name cannot be empty")
        existing = db.exec(
            select(Team).where(
                Team.tenant_id == team.tenant_id, Team.name == name, Team.id != team.id
            )
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Team name already exists")
        team.name = name
    if request.description is not None:
        team.description = request.description
    if request.status is not None:
        team.status = request.status
    if request.config is not None:
        team.config_json = dict(request.config)
    team.updated_at = utc_now()
    db.add(team)
    db.commit()
    db.refresh(team)
    return _team_read(db, team)


@router.delete("/{team_id}")
def delete_team_endpoint(
    team_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    delete_team(db, team)
    return {"ok": True}


@router.post("/{team_id}/members", response_model=TeamMemberRead)
def add_member_endpoint(
    team_id: str,
    request: TeamMemberAddRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamMemberRead:
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    member = add_member(db, team, agent_id=request.agent_id, role=request.role)
    return _member_read(db, member)


@router.delete("/{team_id}/members/{agent_id}")
def remove_member_endpoint(
    team_id: str,
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    remove_member(db, team, agent_id, actor_user_id=current_user.id)
    return {"ok": True}


@router.put("/{team_id}/leader", response_model=TeamMemberRead)
def set_leader_endpoint(
    team_id: str,
    request: TeamLeaderUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamMemberRead:
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    member = set_leader(db, team, request.agent_id)
    return _member_read(db, member)


@router.post("/{team_id}/tl/chat", response_model=TeamTLChatResponse)
def tl_chat_endpoint(
    team_id: str,
    request: TeamTLChatRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamTLChatResponse:
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    leader = get_team_leader(db, team.id)
    if leader is None:
        raise HTTPException(status_code=400, detail="Team has no leader (TL) yet")
    tl_agent = db.get(AgentProfile, leader.agent_id)
    if tl_agent is None or tl_agent.tenant_id != team.tenant_id or tl_agent.status != "active":
        raise HTTPException(status_code=400, detail="Team leader agent is unavailable")
    if request.session_id:
        session = db.get(ChatSession, request.session_id)
        # 同一 Agent 可同时担任多个团队的 TL,必须同时校验 team_id 与「TL 对话」类型,
        # 否则会把 A 团队的会话写进 B 团队的上下文(任务/审计串线)
        if (
            session is None
            or session.tenant_id != team.tenant_id
            or session.team_id != team.id
            or session.agent_id != tl_agent.id
            or "TL 对话" not in (session.title or "")
        ):
            raise HTTPException(status_code=404, detail="TL chat session not found")
    else:
        session = ChatSession(
            id=new_id("session"),
            tenant_id=team.tenant_id,
            user_id=current_user.id,
            agent_id=tl_agent.id,
            title=f"团队 {team.name} · TL 对话",
            status="active",
            team_id=team.id,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
    turn = ChatTurnRequest(
        tenant_id=team.tenant_id,
        session_id=session.id,
        agent_id=tl_agent.id,
        client_turn_id=new_id("teamturn"),
        user_id=current_user.id,
        message=request.message,
        context_injection=build_tl_chat_context(db, team, request.message),
        channel="team",
        interaction_mode="team_tl",
    )
    response = AgentLoop(db).handle_turn(turn)
    reply = response.reply or ""
    created = process_tl_reply(
        db,
        team=team,
        session=session,
        user=current_user,
        user_message=request.message,
        reply=reply,
        client_turn_id=turn.client_turn_id,
    )
    clean_reply = strip_json_blocks(reply)
    return TeamTLChatResponse(
        reply=clean_reply or reply,
        session_id=session.id,
        created_tasks=[_task_read(db, task) for task in created],
    )


@router.post("/{team_id}/tl/session", response_model=TeamTLSessionResponse)
def tl_session_endpoint(
    team_id: str,
    request: TeamTLSessionRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamTLSessionResponse:
    """get-or-create 团队 TL 会话,供前端跳转工作区聊天(幂等)。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    leader = get_team_leader(db, team.id)
    if leader is None:
        raise HTTPException(status_code=400, detail="Team has no leader (TL) yet")
    tl_agent = db.get(AgentProfile, leader.agent_id)
    if tl_agent is None or tl_agent.tenant_id != team.tenant_id or tl_agent.status != "active":
        raise HTTPException(status_code=400, detail="Team leader agent is unavailable")
    # 每个团队只有一个人类群聊。项目领导变更时沿用同一会话并更新承接 Agent，
    # 避免把同一团队拆成多个与普通单聊冲突的会话。
    session = db.exec(
        select(ChatSession)
        .where(
            ChatSession.tenant_id == team.tenant_id,
            ChatSession.team_id == team.id,
            ChatSession.title.like("%TL 对话%"),
        )
        .order_by(ChatSession.created_at)
    ).first()
    if session is None:
        session = ChatSession(
            id=new_id("session"),
            tenant_id=team.tenant_id,
            user_id=current_user.id,
            agent_id=tl_agent.id,
            title=f"团队 {team.name} · TL 对话",
            status="active",
            team_id=team.id,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
    elif session.agent_id != tl_agent.id:
        session.agent_id = tl_agent.id
        session.updated_at = utc_now()
        db.add(session)
        db.commit()
        db.refresh(session)
    return TeamTLSessionResponse(session_id=session.id)


# ---------- 团队 TL 工作台聊天室(按团队维度查询会话与消息) ----------


def _conversation_kind(session: ChatSession) -> TeamConversationKind:
    """会话种类按标题前缀判定,与 wakeup.py / tl_chat 端点的命名约定一一对应(测试锁定):

    「团队任务验收:」-> tl_review、「团队任务:」-> member_task、
    「团队竞标」(竞标:/竞标打分:/竞标裁决:)-> member_bid、
    其余(「团队 xx · TL 对话」)-> tl_chat。
    标题前缀是当前唯一的持久化判据(session 无 kind 列),改命名约定需同步改这里。
    """
    title = session.title or ""
    if title.startswith("团队任务验收:"):
        return "tl_review"
    if title.startswith("团队任务:"):
        return "member_task"
    if title.startswith("团队竞标"):
        return "member_bid"
    return "tl_chat"


def _tl_conversation_session(db: Session, team: Team) -> ChatSession | None:
    """已有 TL 对话会话(与 tl/session 端点同判据:「TL 对话」标题,取最早一个)。"""
    return db.exec(
        select(ChatSession)
        .where(
            ChatSession.tenant_id == team.tenant_id,
            ChatSession.team_id == team.id,
            ChatSession.title.like("%TL 对话%"),
        )
        .order_by(ChatSession.created_at)
    ).first()


@router.get("/{team_id}/conversations", response_model=TeamConversationsResponse)
def list_team_conversations(
    team_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamConversationsResponse:
    """团队会话列表:本租户登录用户可读(对齐 GET tasks);严格按 team_id 过滤,不串团队。"""
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    leader = get_team_leader(db, team.id)
    tl: TeamConversationTLRead | None = None
    if leader is not None:
        tl_agent = db.get(AgentProfile, leader.agent_id)
        tl_session = _tl_conversation_session(db, team)
        tl = TeamConversationTLRead(
            agent_id=leader.agent_id,
            agent_name=tl_agent.name if tl_agent else None,
            session_id=tl_session.id if tl_session else None,
        )
    sessions = list(
        db.exec(
            select(ChatSession).where(
                ChatSession.tenant_id == tenant_id,
                ChatSession.team_id == team.id,
            )
        ).all()
    )
    # 每个会话只用对人可见的消息做摘要；成员执行会话优先取 assistant 回复，
    # 避免把注入给成员的任务提示误显示成“成员回复”。
    messages_by_session: dict[str, list[Message]] = {}
    if sessions:
        message_rows = db.exec(
            select(Message)
            .where(Message.session_id.in_([item.id for item in sessions]))
            .order_by(Message.created_at)
        ).all()
        for row in message_rows:
            messages_by_session.setdefault(row.session_id, []).append(row)
    # member_task 会话由 task.session_id 反向关联任务
    task_by_session = {
        str(task.session_id): task
        for task in db.exec(
            select(TeamTask).where(
                TeamTask.team_id == team.id,
                TeamTask.session_id.in_([item.id for item in sessions]),
            )
        ).all()
    } if sessions else {}
    agent_ids = {item.agent_id for item in sessions if item.agent_id}
    agent_names = {
        agent.id: agent.name
        for agent in db.exec(select(AgentProfile).where(AgentProfile.id.in_(agent_ids))).all()
    } if agent_ids else {}
    conversations: list[TeamConversationRead] = []
    for item in sessions:
        kind = _conversation_kind(item)
        visible_rows = visible_message_rows(messages_by_session.get(item.id, []))
        if kind in {"member_task", "member_bid", "tl_review"}:
            visible_rows = [row for row in visible_rows if row.role == "assistant"]
        last = visible_rows[-1] if visible_rows else None
        task = task_by_session.get(item.id)
        report = dict(task.report_json or {}) if task is not None else {}
        needs_input = bool(task is not None and report.get("needs_input"))
        pending_question = str(
            report.get("full_reply") or report.get("summary") or ""
        ).strip() if needs_input else ""
        conversations.append(
            TeamConversationRead(
                session_id=item.id,
                kind=kind,
                agent_id=item.agent_id,
                agent_name=agent_names.get(item.agent_id or ""),
                task_id=task.id if task is not None else None,
                task_status=task.status if task is not None else None,
                needs_input=needs_input,
                pending_question=pending_question or None,
                title=item.title or "",
                preview=last.content[:80] if last else "",
                created_at=item.created_at,
                updated_at=last.created_at if last else item.created_at,
            )
        )
    conversations.sort(key=lambda entry: entry.updated_at, reverse=True)
    return TeamConversationsResponse(
        team_id=team.id,
        team_name=team.name,
        tl=tl,
        conversations=conversations,
    )


@router.get(
    "/{team_id}/conversations/{session_id}/messages",
    response_model=list[TeamConversationMessageRead],
)
def list_team_conversation_messages(
    team_id: str,
    session_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[TeamConversationMessageRead]:
    """团队会话消息:会话必须属于本团队(物理隔离),跨团队 sessionId 一律 404。"""
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    session = db.get(ChatSession, session_id)
    if session is None or session.tenant_id != tenant_id or session.team_id != team.id:
        raise HTTPException(status_code=404, detail="Team conversation not found")
    rows = db.exec(
        select(Message).where(Message.session_id == session.id).order_by(Message.created_at)
    ).all()
    rows = visible_message_rows(rows)
    result: list[TeamConversationMessageRead] = []
    for row in rows:
        serialized = message_read(
            row,
            db=db,
            content_override=team_service.strip_team_control_blocks(visible_message_content(row)),
        )
        result.append(
            TeamConversationMessageRead(
                id=serialized.id,
                role=serialized.role,
                content=serialized.content,
                metadata=dict(serialized.metadata or {}),
                turn_id=serialized.turn_id,
                created_at=row.created_at,
            )
        )
    return result


@router.get(
    "/{team_id}/conversations/{session_id}/stream",
    response_model=TeamConversationStreamRead,
)
def get_team_conversation_stream(
    team_id: str,
    session_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamConversationStreamRead:
    """Return the latest member turn's incremental reply without exposing injected prompts."""
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    session = db.get(ChatSession, session_id)
    if session is None or session.tenant_id != tenant_id or session.team_id != team.id:
        raise HTTPException(status_code=404, detail="Team conversation not found")

    rows = list(
        reversed(
            db.exec(
                select(AgentEvent)
                .where(
                    AgentEvent.tenant_id == tenant_id,
                    AgentEvent.session_id == session.id,
                )
                .order_by(AgentEvent.created_at.desc())
                .limit(500)
            ).all()
        )
    )
    start_index = next(
        (
            index
            for index in range(len(rows) - 1, -1, -1)
            if rows[index].event_type == "user_message_received"
        ),
        None,
    )
    if start_index is None:
        return TeamConversationStreamRead()

    start_payload = dict(rows[start_index].payload_json or {})
    turn_id = str(start_payload.get("turn_id") or start_payload.get("message_id") or "").strip()
    content = ""
    final_reply = ""
    phase: str | None = None
    status: str = "running"
    updated_at = rows[start_index].created_at
    for row in rows[start_index + 1 :]:
        payload = dict(row.payload_json or {})
        data = payload.get("data")
        event_data = data if isinstance(data, dict) else payload
        event_turn_id = str(
            event_data.get("turn_id")
            or event_data.get("user_message_id")
            or payload.get("turn_id")
            or payload.get("user_message_id")
            or ""
        ).strip()
        if event_turn_id and turn_id and event_turn_id != turn_id:
            continue
        updated_at = row.created_at
        if row.event_type == "stream_status":
            next_phase = str(event_data.get("text") or event_data.get("phase") or "").strip()
            phase = next_phase or phase
        elif row.event_type == "stream_replace":
            content = str(event_data.get("content") or "")
        elif row.event_type in {"stream_delta", "token"}:
            content += str(event_data.get("content") or event_data.get("text") or "")
        elif row.event_type == "assistant_message_created":
            final_reply = str(event_data.get("reply") or "")
        elif row.event_type == "stream_end":
            status = "completed"
        elif row.event_type in {"stream_cancelled", "stream_interrupted", "error_occurred"}:
            status = "failed"

    if not content and status != "running":
        content = final_reply
    return TeamConversationStreamRead(
        status=status,
        content=content,
        phase=phase,
        updated_at=updated_at,
    )


@router.post("/{team_id}/tasks", response_model=TeamTaskRead)
def create_team_task_endpoint(
    team_id: str,
    request: TeamTaskCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamTaskRead:
    """人直接建任务:指定 assignee 直派(同 TL 直派链路);省略则投入任务池竞标。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Task title cannot be empty")
    assignee = (request.assignee_agent_id or "").strip()
    if assignee:
        member_ids = {item.agent_id for item in list_team_members(db, team.id)}
        if assignee not in member_ids:
            raise HTTPException(status_code=404, detail="Agent is not a team member")
    task = TeamTask(
        team_id=team.id,
        tenant_id=team.tenant_id,
        title=title,
        description=request.description,
        priority=request.priority or "normal",
        status="pending",
        created_by_user_id=current_user.id,
        created_by_tl=False,
        assignee_agent_id=assignee or None,
    )
    db.add(task)
    db.flush()
    record_task_event(
        db,
        team_id=team.id,
        task_id=task.id,
        actor_type="user",
        actor_id=current_user.id,
        event_type="task_created",
        payload={"title": task.title, "assignee_agent_id": assignee or None},
    )
    wake_id: str | None = None
    if assignee:
        wake = enqueue_wake_event(
            db,
            team=team,
            target_agent_id=assignee,
            trigger_type="task_assigned",
            payload={"task_id": task.id},
        )
        wake_id = wake.id
    db.commit()
    db.refresh(task)
    if wake_id is not None:
        start_wakeup_async(wake_id)
    else:
        start_bidding(db, team, task)
    return _task_read(db, task, with_events=True)


@router.get("/{team_id}/export")
def export_team_conversation_log(
    team_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Response:
    """下载团队完整审计日志，包含任务调度与全部关联成员会话的原始执行记录。"""
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    _ensure_team_manager(team, current_user)

    task_rows = db.exec(
        select(TeamTask)
        .where(TeamTask.team_id == team.id)
        .order_by(TeamTask.created_at)
    ).all()
    wake_rows = db.exec(
        select(TeamWakeEvent)
        .where(TeamWakeEvent.team_id == team.id)
        .order_by(TeamWakeEvent.created_at)
    ).all()
    blackboard_rows = db.exec(
        select(TeamBlackboardEntry)
        .where(TeamBlackboardEntry.team_id == team.id)
        .order_by(TeamBlackboardEntry.created_at)
    ).all()

    # 兼容旧数据：早期成员执行会话没有 team_id，只通过 TeamTask.session_id 关联。
    session_rows = db.exec(
        select(ChatSession)
        .where(ChatSession.tenant_id == tenant_id, ChatSession.team_id == team.id)
        .order_by(ChatSession.created_at)
    ).all()
    session_by_id = {row.id: row for row in session_rows}
    for task in task_rows:
        if task.session_id and task.session_id not in session_by_id:
            row = db.get(ChatSession, task.session_id)
            if row is not None and row.tenant_id == tenant_id:
                session_by_id[row.id] = row
    ordered_sessions = sorted(session_by_id.values(), key=lambda row: row.created_at)

    payload = {
        "schema_version": TEAM_LOG_EXPORT_SCHEMA,
        "exported_at": datetime.now(UTC).isoformat(),
        "team": _team_read(db, team).model_dump(mode="json"),
        "summary": {
            "task_count": len(task_rows),
            "wake_event_count": len(wake_rows),
            "blackboard_entry_count": len(blackboard_rows),
            "session_count": len(ordered_sessions),
        },
        "tasks": [
            _task_read(db, task, with_events=True).model_dump(mode="json")
            for task in task_rows
        ],
        "wake_events": [row.model_dump(mode="json") for row in wake_rows],
        "blackboard_entries": [row.model_dump(mode="json") for row in blackboard_rows],
        # 与普通对话日志完全复用同一构建链路：消息、反馈、公开 Trace、原始 AgentEvent、
        # 模型输入/输出/reasoning 以及工具参数/结果均由 session detail 统一导出。
        "sessions": _session_details_payload(db, tenant_id, ordered_sessions),
    }
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"staffdeck-team-log-{_safe_filename_part(team.id)}-{timestamp}.json"
    return _json_download_response(payload, filename)


@router.get("/{team_id}/events", response_model=list[TeamEventRead])
def list_team_events(
    team_id: str,
    tenant_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[TeamEventRead]:
    """团队级审计流水:全团队 task_events 按 created_at 倒序聚合,含任务标题。"""
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    rows = db.exec(
        select(TeamTaskEvent)
        .where(TeamTaskEvent.team_id == team.id)
        .order_by(TeamTaskEvent.created_at.desc())
        .limit(limit)
    ).all()
    task_titles = {
        task.id: task.title
        for task in db.exec(select(TeamTask).where(TeamTask.team_id == team.id)).all()
    }
    return [
        TeamEventRead(
            id=row.id,
            task_id=row.task_id,
            team_id=row.team_id,
            task_title=task_titles.get(row.task_id),
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            event_type=row.event_type,
            payload=dict(row.payload_json or {}),
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/{team_id}/tasks", response_model=list[TeamTaskRead])
def list_team_tasks(
    team_id: str,
    tenant_id: str = Query(...),
    status: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[TeamTaskRead]:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    if status is not None and status not in team_service.TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown task status: {status}")
    statement = select(TeamTask).where(TeamTask.team_id == team.id)
    if status is not None:
        statement = statement.where(TeamTask.status == status)
    rows = db.exec(statement.order_by(TeamTask.updated_at.desc())).all()
    return [_task_read(db, row) for row in rows]


@router.get("/{team_id}/tasks/{task_id}", response_model=TeamTaskRead)
def get_team_task(
    team_id: str,
    task_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamTaskRead:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    return _task_read(db, _get_team_task(db, team, task_id), with_events=True)


@router.post("/{team_id}/tasks/{task_id}/award-override", response_model=TeamTaskRead)
def override_task_award(
    team_id: str,
    task_id: str,
    request: AwardOverrideRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamTaskRead:
    """人推翻竞标判罚(HITL):竞标中或执行开始前可改写中标者并重新派发。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    task = _get_team_task(db, team, task_id)
    if task.status not in AWARD_OVERRIDABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task in status {task.status} cannot be award-overridden",
        )
    member_ids = {item.agent_id for item in list_team_members(db, team.id)}
    if request.agent_id not in member_ids:
        raise HTTPException(status_code=404, detail="Agent is not a team member")
    previous = task.assignee_agent_id
    task.assignee_agent_id = request.agent_id
    apply_task_transition(
        db,
        task,
        "pending",
        actor_type="user",
        actor_id=current_user.id,
        event_type="award_overridden",
        payload={
            "previous_assignee_agent_id": previous,
            "winner_agent_id": request.agent_id,
            "comment": request.comment or "",
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    wake = enqueue_wake_event(
        db,
        team=team,
        target_agent_id=request.agent_id,
        trigger_type="task_assigned",
        payload={"task_id": task.id},
    )
    db.commit()
    start_wakeup_async(wake.id)
    return _task_read(db, task, with_events=True)


@router.post("/{team_id}/tasks/{task_id}/override", response_model=TeamTaskRead)
def override_task_review(
    team_id: str,
    task_id: str,
    request: ReviewOverrideRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamTaskRead:
    """人改判 TL 的验收结论(HITL):approve->done / rework->退回重做 / escalate->升级。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    task = _get_team_task(db, team, task_id)
    if task.status not in OVERRIDABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task in status {task.status} cannot be overridden",
        )
    target = VERDICT_TARGET_STATUS[request.verdict]
    payload = {"verdict": request.verdict, "comment": request.comment or "", "override": True}
    if target in team_service.TASK_TRANSITIONS.get(task.status, set()) or target == task.status:
        apply_task_transition(
            db,
            task,
            target,
            actor_type="user",
            actor_id=current_user.id,
            event_type=f"review_override_{request.verdict}",
            payload=payload,
        )
    else:
        # 人已升级(escalated)的任务改判不走状态机,直接落库并写审计
        previous = task.status
        task.status = target
        task.version += 1
        task.updated_at = utc_now()
        db.add(task)
        record_task_event(
            db,
            team_id=task.team_id,
            task_id=task.id,
            actor_type="user",
            actor_id=current_user.id,
            event_type=f"review_override_{request.verdict}",
            payload={"from_status": previous, "to_status": target, **payload},
        )
    task.review_json = {
        **dict(task.review_json or {}),
        "verdict": request.verdict,
        "comment": request.comment or "",
        "overridden_by_user_id": current_user.id,
        "reviewed_at": utc_now().isoformat(),
    }
    db.add(task)
    db.commit()
    db.refresh(task)
    if target in {"done", "escalated"}:
        activate_ready_tasks(db, team)
    if request.verdict == "rework" and task.assignee_agent_id:
        wake = enqueue_wake_event(
            db,
            team=team,
            target_agent_id=task.assignee_agent_id,
            trigger_type="task_rework",
            payload={"task_id": task.id},
        )
        db.commit()
        start_wakeup_async(wake.id)
    return _task_read(db, task, with_events=True)


@router.post("/{team_id}/tasks/{task_id}/resume", response_model=TeamTaskRead)
def resume_team_task(
    team_id: str,
    task_id: str,
    request: TeamTaskResumeRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamTaskRead:
    """把用户补充信息送回原成员任务,沿用同一个 TeamTask 继续执行。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    task = _get_team_task(db, team, task_id)
    if task.created_by_user_id != current_user.id:
        _ensure_team_manager(team, current_user)
    answer = request.answer.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="Answer is required")
    report = dict(task.report_json or {})
    if task.status != "escalated" or not report.get("needs_input"):
        raise HTTPException(status_code=409, detail="Task is not waiting for user input")
    if not task.assignee_agent_id:
        raise HTTPException(status_code=409, detail="Task has no assigned member")

    now = utc_now()
    previous = task.status
    task.status = "rework"
    task.version += 1
    task.updated_at = now
    task.report_json = {**report, "needs_input": False, "answered_at": now.isoformat()}
    task.review_json = {
        **dict(task.review_json or {}),
        "comment": answer,
        "input_provided_by_user_id": current_user.id,
        "input_provided_at": now.isoformat(),
    }
    db.add(task)
    record_task_event(
        db,
        team_id=task.team_id,
        task_id=task.id,
        actor_type="user",
        actor_id=current_user.id,
        event_type="task_input_provided",
        payload={
            "from_status": previous,
            "to_status": "rework",
            "answer": answer,
        },
    )
    wake = enqueue_wake_event(
        db,
        team=team,
        target_agent_id=task.assignee_agent_id,
        trigger_type="task_rework",
        payload={"task_id": task.id},
    )
    db.commit()
    db.refresh(task)
    start_wakeup_async(wake.id)
    return _task_read(db, task, with_events=True)


def _blackboard_entry_read(entry: TeamBlackboardEntry) -> TeamBlackboardEntryRead:
    return TeamBlackboardEntryRead(
        id=entry.id,
        team_id=entry.team_id,
        tenant_id=entry.tenant_id,
        content=entry.content,
        tags=list(entry.tags_json or []),
        source_type=entry.source_type,
        source_agent_id=entry.source_agent_id,
        source_task_id=entry.source_task_id,
        citation=dict(entry.citation_json or {}),
        status=entry.status,
        pinned=entry.pinned,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _get_blackboard_entry(db: Session, team: Team, entry_id: str) -> TeamBlackboardEntry:
    entry = db.get(TeamBlackboardEntry, entry_id)
    if entry is None or entry.team_id != team.id:
        raise HTTPException(status_code=404, detail="Blackboard entry not found")
    return entry


@router.get("/{team_id}/blackboard", response_model=list[TeamBlackboardEntryRead])
def list_blackboard_entries(
    team_id: str,
    tenant_id: str = Query(...),
    status: str = Query("active"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[TeamBlackboardEntryRead]:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    team = get_team(db, tenant_id, team_id)
    if status not in team_service.BLACKBOARD_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown blackboard status: {status}")
    rows = db.exec(
        select(TeamBlackboardEntry)
        .where(TeamBlackboardEntry.team_id == team.id, TeamBlackboardEntry.status == status)
        .order_by(TeamBlackboardEntry.pinned.desc(), TeamBlackboardEntry.updated_at.desc())
    ).all()
    return [_blackboard_entry_read(row) for row in rows]


@router.post("/{team_id}/blackboard", response_model=TeamBlackboardWriteResponse)
def create_blackboard_entry(
    team_id: str,
    request: TeamBlackboardEntryCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamBlackboardWriteResponse:
    """人直写黑板:与 TL 裁决写入走同一条轻量流水线。"""
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    written, skipped = write_blackboard_entries(
        db,
        team=team,
        entries=[{"content": request.content, "tags": request.tags}],
        source_type="human",
    )
    db.commit()
    for entry in written:
        db.refresh(entry)
    return TeamBlackboardWriteResponse(
        entries=[_blackboard_entry_read(entry) for entry in written],
        skipped=skipped,
    )


@router.put("/{team_id}/blackboard/{entry_id}", response_model=TeamBlackboardEntryRead)
def update_blackboard_entry(
    team_id: str,
    entry_id: str,
    request: TeamBlackboardEntryUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamBlackboardEntryRead:
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    entry = _get_blackboard_entry(db, team, entry_id)
    if request.content is not None:
        content = normalize_blackboard_content(request.content)
        if not content:
            raise HTTPException(status_code=400, detail="Blackboard content cannot be empty")
        entry.content = content
    if request.tags is not None:
        entry.tags_json = normalize_blackboard_tags(request.tags)
    if request.pinned is not None:
        entry.pinned = request.pinned
    entry.updated_at = utc_now()
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _blackboard_entry_read(entry)


@router.post("/{team_id}/blackboard/{entry_id}/archive", response_model=TeamBlackboardEntryRead)
def archive_blackboard_entry(
    team_id: str,
    entry_id: str,
    request: TeamBlackboardEntryArchiveRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamBlackboardEntryRead:
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    entry = _get_blackboard_entry(db, team, entry_id)
    entry.status = "archived"
    entry.updated_at = utc_now()
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _blackboard_entry_read(entry)


@router.post(
    "/{team_id}/blackboard/{entry_id}/promote",
    response_model=TeamBlackboardPromoteResponse,
)
def promote_blackboard_entry(
    team_id: str,
    entry_id: str,
    request: TeamBlackboardPromoteRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TeamBlackboardPromoteResponse:
    """黑板条目沉淀到知识库:拼成 markdown 作为原始资料建 ingest job,异步执行。

    幂等:已沉淀的条目直接返回 citation 里的既有引用,不重复建 job。
    """
    ensure_tenant(db, request.tenant_id)
    _ensure_request_tenant(request.tenant_id, current_user)
    team = get_team(db, request.tenant_id, team_id)
    _ensure_team_manager(team, current_user)
    entry = _get_blackboard_entry(db, team, entry_id)
    citation = dict(entry.citation_json or {})
    existing_job_id = str(citation.get("ingest_job_id") or "")
    if existing_job_id:
        return TeamBlackboardPromoteResponse(
            entry=_blackboard_entry_read(entry),
            knowledge_base_id=str(citation.get("knowledge_base_id") or ""),
            ingest_job_id=existing_job_id,
            already_promoted=True,
        )
    service = KnowledgeService(db)
    try:
        knowledge_base = team_service.resolve_team_knowledge_write_base(
            db,
            team=team,
            knowledge_base_id=request.knowledge_base_id,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    source_task_title = ""
    if entry.source_task_id:
        source_task = db.get(TeamTask, entry.source_task_id)
        source_task_title = source_task.title if source_task else ""
    # 内容 + tags 拼成一段 markdown,标注来源团队/任务
    lines = [f"# 团队黑板沉淀 · {team.name}", ""]
    lines.append(f"> 来源团队:{team.name}(team_id={team.id})")
    if entry.source_task_id:
        lines.append(f"> 来源任务:{source_task_title or entry.source_task_id}(task_id={entry.source_task_id})")
    if entry.tags_json:
        lines.append(f"> 标签:{', '.join(str(tag) for tag in entry.tags_json)}")
    lines.extend(["", entry.content, ""])
    markdown = "\n".join(lines)
    filename = f"team-blackboard-{entry.id}.md"
    job = service.create_ingest_job(
        IngestPayload(
            tenant_id=team.tenant_id,
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=knowledge_base.published_version_id,
            filename=filename,
            content_base64=base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
            title=f"团队黑板:{entry.content[:30]}",
            metadata={
                "source": "team_blackboard",
                "team_id": team.id,
                "blackboard_entry_id": entry.id,
            },
        )
    )
    entry.citation_json = {
        **citation,
        "knowledge_base_id": knowledge_base.id,
        "ingest_job_id": job.id,
    }
    entry.updated_at = utc_now()
    db.add(entry)
    db.commit()
    db.refresh(entry)
    # 异步执行与知识库文档上传同款:进程内 AsyncJob 队列
    enqueue_async_job(
        "knowledge_ingest",
        service.run_ingest_job,
        job.id,
        metadata={"tenant_id": team.tenant_id, "filename": filename},
    )
    return TeamBlackboardPromoteResponse(
        entry=_blackboard_entry_read(entry),
        knowledge_base_id=knowledge_base.id,
        ingest_job_id=job.id,
        already_promoted=False,
    )


# 跨团队统一线程列表:独立前缀 /api/enterprise/team-threads,与 /teams/{team_id} 无冲突
threads_router = APIRouter(prefix="/api/enterprise/team-threads", tags=["enterprise:teams"])


@threads_router.get("", response_model=list[TeamThreadRead])
def list_team_threads(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[TeamThreadRead]:
    """TL 对话会话 + 任务执行会话,按 updated_at 倒序取 50 条。"""
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    teams = list(db.exec(select(Team).where(Team.tenant_id == tenant_id)).all())
    threads: list[TeamThreadRead] = []
    for team in teams:
        leader = get_team_leader(db, team.id)
        if leader is not None:
            tl_sessions = db.exec(
                select(ChatSession).where(
                    ChatSession.tenant_id == tenant_id,
                    ChatSession.team_id == team.id,
                    ChatSession.agent_id == leader.agent_id,
                    ChatSession.title.like("%TL 对话%"),
                )
            ).all()
            for session in tl_sessions:
                threads.append(
                    TeamThreadRead(
                        team_id=team.id,
                        team_name=team.name,
                        kind="tl_chat",
                        session_id=session.id,
                        title=session.title or f"团队 {team.name} · TL 对话",
                        updated_at=session.updated_at,
                    )
                )
        task_rows = db.exec(
            select(TeamTask).where(
                TeamTask.team_id == team.id, TeamTask.session_id.is_not(None)
            )
        ).all()
        for task in task_rows:
            threads.append(
                TeamThreadRead(
                    team_id=team.id,
                    team_name=team.name,
                    kind="task",
                    session_id=str(task.session_id),
                    task_id=task.id,
                    title=task.title,
                    task_status=task.status,
                    updated_at=task.updated_at,
                )
            )
    threads.sort(key=lambda item: item.updated_at, reverse=True)
    return threads[:50]
