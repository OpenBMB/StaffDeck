import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';

import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '../components/AppHeader';
import BiddingArena from '../components/BiddingArena';
import EmployeeAvatar from '../components/EmployeeAvatar';
import { employeeDisplayName } from '../employee';
import { EnterpriseRoute } from '../enums/routes';
import { formatClientDateTime, parseBackendDateTime } from '../lib/timezone';
import type {
  AgentProfileRead,
  TeamBlackboardEntryRead,
  TeamEventRead,
  TeamMemberRead,
  TeamRead,
  TeamReviewVerdict,
  TeamTaskBidRead,
  TeamTaskRead,
} from '../types';

import { relativeTimeLabel, teamStatusLabel } from './TeamsPage';

const TEAM_EVENT_TYPE_LABELS: Record<string, string> = {
  task_created: '任务创建',
  task_started: '任务开始',
  task_rework_started: '退回重做',
  task_reported: '提交报告',
  task_escalated: '任务升级',
  task_needs_input: '需要补充信息',
  task_bidding_started: '竞标开始',
  task_awarded: '竞标定标',
  bid_submitted: '提交竞标',
  bid_skipped: '跳过竞标',
  bid_failed: '竞标失败',
  bid_award_unparsed: '定标解析失败',
  tl_review_skipped: 'TL 免验收',
  tl_review_unparsed: 'TL 验收解析失败',
  tl_review_repair_failed: 'TL 验收修复失败',
  tl_review_approve: 'TL 验收通过',
  tl_review_rework: 'TL 退回重做',
  tl_review_escalate: 'TL 升级',
  review_override_approve: '人工改判通过',
  review_override_rework: '人工改判退回',
  review_override_escalate: '人工改判升级',
  blackboard_written: '写入黑板',
};

export function teamEventTypeLabel(eventType: string): string {
  return TEAM_EVENT_TYPE_LABELS[eventType] || eventType;
}

const TASK_STATUS_COLUMNS: { status: string; label: string }[] = [
  { status: 'bidding', label: '竞标中' },
  { status: 'pending', label: '待认领' },
  { status: 'in_progress', label: '进行中' },
  { status: 'review', label: '待验收' },
  { status: 'done', label: '已完成' },
  { status: 'rework', label: '已退回' },
  { status: 'escalated', label: '已升级' },
];

const OVERRIDABLE_STATUSES = new Set(['review', 'escalated']);

const AWARD_OVERRIDABLE_STATUSES = new Set(['bidding', 'pending']);

const POOL_ASSIGNEE_VALUE = '__pool__';

export function taskPriorityLabel(priority: string): string {
  if (priority === 'high' || priority === 'urgent') return '高';
  if (priority === 'medium' || priority === 'normal') return '中';
  if (priority === 'low') return '低';
  return priority;
}

const REVIEW_BANNERS: Record<string, { label: string; bannerClass: string; quoteClass: string }> = {
  approve: {
    label: '验收通过',
    bannerClass: 'border-[#bfe6cf] bg-[#eefaf3] text-[#1e7a4c]',
    quoteClass: 'border-[#35b26f]',
  },
  rework: {
    label: '退回重做',
    bannerClass: 'border-[#f5ddba] bg-[#fdf6ea] text-[#a3620a]',
    quoteClass: 'border-[#f5a83b]',
  },
  escalate: {
    label: '已升级',
    bannerClass: 'border-[#f6c8c4] bg-[#fdeeec] text-[#c0342b]',
    quoteClass: 'border-[#f5483b]',
  },
};

const DEFAULT_REVIEW_BANNER = {
  label: '',
  bannerClass: 'border-[#e3e7f1] bg-[#f8f9fb] text-[#464c5e]',
  quoteClass: 'border-[#a7adbb]',
};

function textField(source: Record<string, unknown> | undefined, key: string): string {
  const value = source?.[key];
  return typeof value === 'string' ? value : '';
}

function parseTags(raw: string): string[] {
  return raw
    .split(/[,，]/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

export default function TeamDetailPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onLogout?: () => void;
}) {
  const { teamId = '' } = useParams<{ teamId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [team, setTeam] = useState<TeamRead | null>(null);
  const [tasks, setTasks] = useState<TeamTaskRead[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [addAgentId, setAddAgentId] = useState('');
  const [addingMember, setAddingMember] = useState(false);
  const [activeTask, setActiveTask] = useState<TeamTaskRead | null>(null);
  const [overrideComment, setOverrideComment] = useState('');
  const [overriding, setOverriding] = useState(false);
  const [boardEntries, setBoardEntries] = useState<TeamBlackboardEntryRead[]>([]);
  const [boardContent, setBoardContent] = useState('');
  const [boardTags, setBoardTags] = useState('');
  const [postingEntry, setPostingEntry] = useState(false);
  const [editingEntry, setEditingEntry] = useState<TeamBlackboardEntryRead | null>(null);
  const [editContent, setEditContent] = useState('');
  const [editTags, setEditTags] = useState('');
  const [savingEntry, setSavingEntry] = useState(false);
  const [taskDialogOpen, setTaskDialogOpen] = useState(false);
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskDescription, setNewTaskDescription] = useState('');
  const [newTaskPriority, setNewTaskPriority] = useState('medium');
  const [newTaskAssignee, setNewTaskAssignee] = useState(POOL_ASSIGNEE_VALUE);
  const [creatingTask, setCreatingTask] = useState(false);
  const [awardAgentId, setAwardAgentId] = useState('');
  const [awardComment, setAwardComment] = useState('');
  const [awarding, setAwarding] = useState(false);
  const [teamEvents, setTeamEvents] = useState<TeamEventRead[]>([]);
  const [configConcurrency, setConfigConcurrency] = useState('1');
  const [configTaskTimeout, setConfigTaskTimeout] = useState('30');
  const [configBidRounds, setConfigBidRounds] = useState('1');
  const [savingConfig, setSavingConfig] = useState(false);
  const [promotingEntryId, setPromotingEntryId] = useState<string | null>(null);
  const openedTaskParamRef = useRef<string | null>(null);

  const loadTeam = useCallback(async () => {
    try {
      const detail = await api.get<TeamRead>(`/api/enterprise/teams/${teamId}?tenant_id=${TENANT_ID}`);
      setTeam(detail);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载团队详情失败');
    }
  }, [teamId]);

  const loadTasks = useCallback(async () => {
    try {
      const rows = await api.get<TeamTaskRead[]>(`/api/enterprise/teams/${teamId}/tasks?tenant_id=${TENANT_ID}`);
      setTasks(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载任务失败');
    }
  }, [teamId]);

  const loadBoard = useCallback(async () => {
    try {
      const rows = await api.get<TeamBlackboardEntryRead[]>(
        `/api/enterprise/teams/${teamId}/blackboard?tenant_id=${TENANT_ID}&status=active`,
      );
      setBoardEntries(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载黑板失败');
    }
  }, [teamId]);

  const loadEvents = useCallback(async () => {
    try {
      const rows = await api.get<TeamEventRead[]>(
        `/api/enterprise/teams/${teamId}/events?tenant_id=${TENANT_ID}&limit=50`,
      );
      setTeamEvents(rows);
    } catch {
      setTeamEvents([]);
    }
  }, [teamId]);

  useEffect(() => {
    setLoading(true);
    void Promise.all([
      loadTeam(),
      loadTasks(),
      loadBoard(),
      loadEvents(),
      api
        .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
        .then(setAgents)
        .catch(() => setAgents([])),
    ]).finally(() => setLoading(false));
  }, [loadTeam, loadTasks, loadBoard, loadEvents]);

  useEffect(() => {
    const config = team?.config || {};
    setConfigConcurrency(String(config.member_concurrency ?? 1));
    setConfigTaskTimeout(String(config.task_timeout_minutes ?? 30));
    setConfigBidRounds(String(config.bid_rebuttal_rounds ?? 1));
  }, [team]);

  const taskParam = searchParams.get('task');
  useEffect(() => {
    if (!taskParam || openedTaskParamRef.current === taskParam) return;
    const target = tasks.find((item) => item.id === taskParam);
    if (!target) return;
    openedTaskParamRef.current = taskParam;
    void openTask(target);
  }, [taskParam, tasks]);

  const memberNameByAgentId = useMemo(() => {
    const map = new Map<string, string>();
    (team?.members || []).forEach((member) => {
      if (member.agent_name) map.set(member.agent_id, member.agent_name);
    });
    agents.forEach((agent) => {
      if (!map.has(agent.id)) map.set(agent.id, employeeDisplayName(agent));
    });
    return map;
  }, [team, agents]);

  function assigneeName(task: TeamTaskRead): string {
    if (!task.assignee_agent_id) return '未分配';
    return memberNameByAgentId.get(task.assignee_agent_id) || task.assignee_agent_id;
  }

  const candidateAgents = useMemo(() => {
    const memberIds = new Set((team?.members || []).map((member) => member.agent_id));
    return agents.filter((agent) => !agent.is_overall && !memberIds.has(agent.id));
  }, [agents, team]);

  async function addMember() {
    if (!addAgentId) {
      notify.error('请选择要添加的员工');
      return;
    }
    setAddingMember(true);
    try {
      await api.post(`/api/enterprise/teams/${teamId}/members`, {
        tenant_id: TENANT_ID,
        agent_id: addAgentId,
      });
      notify.success('成员已添加');
      setAddAgentId('');
      await loadTeam();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '添加成员失败');
    } finally {
      setAddingMember(false);
    }
  }

  async function removeMember(agentId: string) {
    try {
      await api.delete(`/api/enterprise/teams/${teamId}/members/${agentId}?tenant_id=${TENANT_ID}`);
      notify.success('成员已移除');
      await loadTeam();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '移除成员失败');
    }
  }

  async function promoteLeader(agentId: string) {
    try {
      await api.put(`/api/enterprise/teams/${teamId}/leader`, {
        tenant_id: TENANT_ID,
        agent_id: agentId,
      });
      notify.success('已更换团队 TL');
      await loadTeam();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更换 TL 失败');
    }
  }

  function openTlSession() {
    // TL 会话的创建在新页面内按需触发（tl.session_id 为空时先建会话再进入）
    navigate(`/enterprise/teams/${teamId}/chat`);
  }

  async function createTask() {
    const title = newTaskTitle.trim();
    if (!title) {
      notify.error('请输入任务标题');
      return;
    }
    if (creatingTask) return;
    setCreatingTask(true);
    try {
      await api.post<TeamTaskRead>(`/api/enterprise/teams/${teamId}/tasks`, {
        tenant_id: TENANT_ID,
        title,
        description: newTaskDescription.trim() || undefined,
        priority: newTaskPriority,
        assignee_agent_id: newTaskAssignee === POOL_ASSIGNEE_VALUE ? undefined : newTaskAssignee,
      });
      notify.success('任务已创建');
      setTaskDialogOpen(false);
      setNewTaskTitle('');
      setNewTaskDescription('');
      setNewTaskPriority('medium');
      setNewTaskAssignee(POOL_ASSIGNEE_VALUE);
      await loadTasks();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建任务失败');
    } finally {
      setCreatingTask(false);
    }
  }

  async function addBoardEntry() {
    const content = boardContent.trim();
    if (!content) {
      notify.error('请输入黑板内容');
      return;
    }
    if (postingEntry) return;
    setPostingEntry(true);
    try {
      await api.post(`/api/enterprise/teams/${teamId}/blackboard`, {
        tenant_id: TENANT_ID,
        content,
        tags: parseTags(boardTags),
      });
      notify.success('黑板条目已添加');
      setBoardContent('');
      setBoardTags('');
      await loadBoard();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '添加黑板条目失败');
    } finally {
      setPostingEntry(false);
    }
  }

  async function togglePinEntry(entry: TeamBlackboardEntryRead) {
    try {
      await api.put(`/api/enterprise/teams/${teamId}/blackboard/${entry.id}`, {
        tenant_id: TENANT_ID,
        pinned: !entry.pinned,
      });
      notify.success(entry.pinned ? '已取消置顶' : '已置顶');
      await loadBoard();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新黑板条目失败');
    }
  }

  function openEditEntry(entry: TeamBlackboardEntryRead) {
    setEditingEntry(entry);
    setEditContent(entry.content);
    setEditTags(entry.tags.join(', '));
  }

  async function saveEditEntry() {
    const entry = editingEntry;
    if (!entry || savingEntry) return;
    const content = editContent.trim();
    if (!content) {
      notify.error('请输入黑板内容');
      return;
    }
    setSavingEntry(true);
    try {
      await api.put(`/api/enterprise/teams/${teamId}/blackboard/${entry.id}`, {
        tenant_id: TENANT_ID,
        content,
        tags: parseTags(editTags),
      });
      notify.success('黑板条目已保存');
      setEditingEntry(null);
      await loadBoard();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存黑板条目失败');
    } finally {
      setSavingEntry(false);
    }
  }

  async function archiveBoardEntry(entry: TeamBlackboardEntryRead) {
    if (!window.confirm('确认归档该黑板条目？归档后不再展示。')) return;
    try {
      await api.post(`/api/enterprise/teams/${teamId}/blackboard/${entry.id}/archive`, {
        tenant_id: TENANT_ID,
      });
      notify.success('黑板条目已归档');
      await loadBoard();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '归档黑板条目失败');
    }
  }

  function boardSourceLabel(entry: TeamBlackboardEntryRead): string {
    if (entry.source_type === 'human') return '人';
    if (entry.source_type === 'leader') return 'TL';
    if (entry.source_agent_id) {
      return memberNameByAgentId.get(entry.source_agent_id) || entry.source_agent_id;
    }
    return '成员';
  }

  async function promoteBoardEntry(entry: TeamBlackboardEntryRead) {
    if (promotingEntryId) return;
    setPromotingEntryId(entry.id);
    try {
      await api.post(`/api/enterprise/teams/${teamId}/blackboard/${entry.id}/promote`, {
        tenant_id: TENANT_ID,
      });
      notify.success('已沉淀到知识库');
      await loadBoard();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '沉淀到知识库失败');
    } finally {
      setPromotingEntryId(null);
    }
  }

  function eventActorLabel(event: TeamEventRead): string {
    if (event.actor_id) {
      const name = memberNameByAgentId.get(event.actor_id);
      if (name) return name;
    }
    if (event.actor_type === 'user') return '用户';
    if (event.actor_type === 'system') return '系统';
    if (event.actor_type === 'tl') return 'TL';
    return event.actor_type;
  }

  async function saveTeamConfig() {
    if (!team || savingConfig) return;
    const concurrency = Number(configConcurrency);
    const timeoutMinutes = Number(configTaskTimeout);
    const rebuttalRounds = Number(configBidRounds);
    const valid =
      Number.isInteger(concurrency) && concurrency >= 1 &&
      Number.isInteger(timeoutMinutes) && timeoutMinutes >= 1 &&
      Number.isInteger(rebuttalRounds) && rebuttalRounds >= 0;
    if (!valid) {
      notify.error('请输入有效的数字');
      return;
    }
    setSavingConfig(true);
    try {
      await api.put(`/api/enterprise/teams/${teamId}`, {
        tenant_id: TENANT_ID,
        config: {
          ...(team.config || {}),
          member_concurrency: concurrency,
          task_timeout_minutes: timeoutMinutes,
          bid_rebuttal_rounds: rebuttalRounds,
        },
      });
      notify.success('团队设置已保存');
      await loadTeam();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存团队设置失败');
    } finally {
      setSavingConfig(false);
    }
  }

  async function openTask(task: TeamTaskRead) {
    setActiveTask(task);
    setOverrideComment('');
    setAwardAgentId('');
    setAwardComment('');
    try {
      const detail = await api.get<TeamTaskRead>(
        `/api/enterprise/teams/${teamId}/tasks/${task.id}?tenant_id=${TENANT_ID}`,
      );
      setActiveTask(detail);
    } catch {
      // 详情加载失败时保留列表中的概要数据
    }
  }

  async function awardOverride() {
    const task = activeTask;
    if (!task || awarding) return;
    if (!awardAgentId) {
      notify.error('请选择执行者');
      return;
    }
    setAwarding(true);
    try {
      await api.post<TeamTaskRead>(
        `/api/enterprise/teams/${teamId}/tasks/${task.id}/award-override`,
        {
          tenant_id: TENANT_ID,
          agent_id: awardAgentId,
          comment: awardComment.trim() || undefined,
        },
      );
      notify.success('已提交改判');
      setActiveTask(null);
      await loadTasks();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '改判失败');
    } finally {
      setAwarding(false);
    }
  }

  async function overrideTask(verdict: TeamReviewVerdict) {
    const task = activeTask;
    if (!task || overriding) return;
    setOverriding(true);
    try {
      await api.post<TeamTaskRead>(
        `/api/enterprise/teams/${teamId}/tasks/${task.id}/override`,
        {
          tenant_id: TENANT_ID,
          verdict,
          comment: overrideComment.trim() || undefined,
        },
      );
      notify.success('已提交改判');
      setActiveTask(null);
      await loadTasks();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '改判失败');
    } finally {
      setOverriding(false);
    }
  }

  const tasksByStatus = useMemo(() => {
    const grouped = new Map<string, TeamTaskRead[]>();
    TASK_STATUS_COLUMNS.forEach((column) => grouped.set(column.status, []));
    tasks.forEach((task) => {
      const bucket = grouped.get(task.status) || [];
      bucket.push(task);
      grouped.set(task.status, bucket);
    });
    grouped.forEach((bucket) => {
      bucket.sort(
        (a, b) => parseBackendDateTime(b.created_at).getTime() - parseBackendDateTime(a.created_at).getTime(),
      );
    });
    return grouped;
  }, [tasks]);

  const agentById = useMemo(() => {
    return new Map(agents.map((agent) => [agent.id, agent]));
  }, [agents]);

  type EventGroup = {
    key: string;
    task: TeamTaskRead | null;
    title: string;
    events: TeamEventRead[];
    latest: number;
  };

  const eventGroups = useMemo(() => {
    const groups = new Map<string, EventGroup>();
    teamEvents.forEach((event) => {
      const key = event.task_id || '__other__';
      let group = groups.get(key);
      if (!group) {
        const task = event.task_id
          ? tasks.find((item) => item.id === event.task_id) || null
          : null;
        group = {
          key,
          task,
          title: event.task_id ? event.task_title || task?.title || '未命名任务' : '其他',
          events: [],
          latest: 0,
        };
        groups.set(key, group);
      }
      group.events.push(event);
    });
    const result = [...groups.values()];
    result.forEach((group) => {
      group.events.sort(
        (a, b) => parseBackendDateTime(b.created_at).getTime() - parseBackendDateTime(a.created_at).getTime(),
      );
      group.latest = group.events[0]
        ? parseBackendDateTime(group.events[0].created_at).getTime() || 0
        : 0;
    });
    result.sort((a, b) => b.latest - a.latest);
    return result;
  }, [teamEvents, tasks]);

  const sortedBoardEntries = useMemo(() => {
    return [...boardEntries].sort((a, b) => Number(b.pinned) - Number(a.pinned));
  }, [boardEntries]);

  const bidRounds = useMemo(() => {
    const grouped = new Map<number, TeamTaskBidRead[]>();
    (activeTask?.bids || []).forEach((bid) => {
      const list = grouped.get(bid.round) || [];
      list.push(bid);
      grouped.set(bid.round, list);
    });
    return [...grouped.entries()].sort((a, b) => a[0] - b[0]);
  }, [activeTask]);

  // 已裁决：存在竞标记录且已有负责人（竞标中状态视为未裁决）
  const biddingWinnerId =
    activeTask && activeTask.status !== 'bidding' && bidRounds.length > 0
      ? activeTask.assignee_agent_id || null
      : null;

  const awardCandidates = useMemo(() => {
    const members = team?.members || [];
    const bidderIds = new Set((activeTask?.bids || []).map((bid) => bid.agent_id));
    return [...members].sort(
      (a, b) => Number(bidderIds.has(b.agent_id)) - Number(bidderIds.has(a.agent_id)),
    );
  }, [team, activeTask]);

  const reportSummary = textField(activeTask?.report, 'summary');
  const reportFullReply = textField(activeTask?.report, 'full_reply');
  const reviewVerdict = textField(activeTask?.review, 'verdict');
  const reviewComment = textField(activeTask?.review, 'comment');

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title={team?.name || '团队详情'}
        description={team?.description || undefined}
      />

      <div className="mt-[16px]">
        <Button
          type="button"
          variant="outline"
          onClick={() => navigate(EnterpriseRoute.Teams)}
          className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[12px] text-[12px] font-normal text-[#464c5e]"
        >
          返回团队列表
        </Button>
      </div>

      <div className="mt-[16px] grid grid-cols-1 gap-[20px] lg:grid-cols-2">
        <section aria-label="成员管理" className="rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
          <div className="mb-[12px] flex items-center justify-between">
            <h2 className="text-[16px] font-medium text-[#18181a]">成员管理</h2>
            <Badge variant="secondary" className="rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]">
              {team ? teamStatusLabel(team.status) : ''}
            </Badge>
          </div>
          <div className="flex flex-col gap-[8px]">
            {(() => {
              const members = team?.members || [];
              const leader = members.find((member) => member.role === 'leader');
              const others = members.filter((member) => member.role !== 'leader');

              function memberNode(member: TeamMemberRead, isLeader: boolean) {
                return (
                  <div className="flex w-[132px] shrink-0 flex-col items-center gap-[6px] rounded-[12px] border border-[#eef1f6] bg-white px-[10px] py-[10px]">
                    <EmployeeAvatar agent={agentById.get(member.agent_id)} size={48} radius={14} />
                    <span
                      className="max-w-full truncate text-[13px] font-medium text-[#18181a]"
                      title={member.agent_name || member.agent_id}
                    >
                      {member.agent_name || member.agent_id}
                    </span>
                    <Badge
                      variant="secondary"
                      className={cn(
                        'shrink-0 rounded-full text-[12px] font-normal',
                        isLeader ? 'bg-[#e8f0ff] text-[#1a71ff]' : 'bg-[#f2f3f7] text-[#858b9c]',
                      )}
                    >
                      {isLeader ? 'TL' : '成员'}
                    </Badge>
                    <div className="flex items-center gap-[2px]">
                      {!isLeader && (
                        <button
                          type="button"
                          onClick={() => void promoteLeader(member.agent_id)}
                          className="rounded-[8px] px-[6px] py-[4px] text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6]"
                        >
                          设为 TL
                        </button>
                      )}
                      <button
                        type="button"
                        aria-label={`移除成员 ${member.agent_name || member.agent_id}`}
                        onClick={() => void removeMember(member.agent_id)}
                        className="rounded-[8px] px-[6px] py-[4px] text-[12px] text-[#858b9c] transition-colors hover:bg-[#fce7e7] hover:text-[#f5483b]"
                      >
                        移除
                      </button>
                    </div>
                  </div>
                );
              }

              return (
                <div className="flex flex-col items-center">
                  {leader && memberNode(leader, true)}
                  {leader && others.length > 0 && <div className="h-[14px] w-px bg-[#dbe1ec]" />}
                  {others.length > 0 && (
                    <div className="flex max-w-full justify-center gap-[12px] overflow-x-auto">
                      {others.map((member, index) => (
                        <div key={member.id} className="flex flex-col items-center">
                          {leader && (
                            <>
                              <div className="flex w-full">
                                <div
                                  className={cn(
                                    '-mr-[6px] h-px w-[calc(50%+6px)]',
                                    index > 0 && 'bg-[#dbe1ec]',
                                  )}
                                />
                                <div
                                  className={cn(
                                    '-ml-[6px] h-px w-[calc(50%+6px)]',
                                    index < others.length - 1 && 'bg-[#dbe1ec]',
                                  )}
                                />
                              </div>
                              <div className="h-[12px] w-px bg-[#dbe1ec]" />
                            </>
                          )}
                          {memberNode(member, false)}
                        </div>
                      ))}
                    </div>
                  )}
                  {team && members.length === 0 && (
                    <p className="py-[12px] text-center text-[12px] text-[#a7adbb]">暂无成员</p>
                  )}
                </div>
              );
            })()}
          </div>
          <div className="mt-[12px] flex items-center gap-[8px]">
            <Select value={addAgentId} onValueChange={setAddAgentId}>
              <SelectTrigger aria-label="选择员工" className="h-[36px] flex-1 rounded-[10px] border-[#e3e7f1] text-[14px]">
                <SelectValue placeholder="选择员工" />
              </SelectTrigger>
              <SelectContent>
                {candidateAgents.map((agent) => (
                  <SelectItem key={agent.id} value={agent.id}>
                    {employeeDisplayName(agent)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              type="button"
              disabled={addingMember}
              onClick={() => void addMember()}
              className="h-[36px] shrink-0 rounded-[10px] bg-[#18181a] px-[16px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              添加成员
            </Button>
          </div>
        </section>

        <section aria-label="TL 对话" className="flex flex-col rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
          <h2 className="mb-[12px] text-[16px] font-medium text-[#18181a]">TL 对话</h2>
          <div className="flex min-h-[240px] flex-1 flex-col items-center justify-center gap-[12px] rounded-[12px] bg-[#f8f9fb] p-[24px] text-center">
            <p className="max-w-[360px] text-[13px] leading-[20px] text-[#464c5e]">
              向 TL 描述目标，TL 会拆解并派发任务。对话在团队专属聊天室中进行，可查看完整的执行过程与产出。
            </p>
            <Button
              type="button"
              onClick={openTlSession}
              className="h-[36px] rounded-[10px] bg-[#18181a] px-[16px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              打开 TL 对话
            </Button>
          </div>
        </section>

        <section aria-label="团队设置" className="rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
          <h2 className="mb-[12px] text-[16px] font-medium text-[#18181a]">团队设置</h2>
          <div className="grid grid-cols-1 gap-[12px] sm:grid-cols-3">
            <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
              成员并发上限
              <Input
                type="number"
                min={1}
                step={1}
                value={configConcurrency}
                onChange={(event) => setConfigConcurrency(event.target.value)}
                aria-label="成员并发上限"
                className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[14px]"
              />
            </label>
            <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
              任务超时分钟
              <Input
                type="number"
                min={1}
                step={1}
                value={configTaskTimeout}
                onChange={(event) => setConfigTaskTimeout(event.target.value)}
                aria-label="任务超时分钟"
                className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[14px]"
              />
            </label>
            <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
              竞标反驳轮数
              <Input
                type="number"
                min={0}
                step={1}
                value={configBidRounds}
                onChange={(event) => setConfigBidRounds(event.target.value)}
                aria-label="竞标反驳轮数"
                className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[14px]"
              />
            </label>
          </div>
          <div className="mt-[12px] flex justify-end">
            <Button
              type="button"
              disabled={savingConfig || !team}
              onClick={() => void saveTeamConfig()}
              className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] font-normal text-white hover:bg-[#303030]"
            >
              {savingConfig ? '保存中…' : '保存设置'}
            </Button>
          </div>
        </section>
      </div>

      <section aria-label="团队黑板" className="mt-[20px] rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
        <h2 className="mb-[12px] text-[16px] font-medium text-[#18181a]">团队黑板</h2>
        <div className="flex flex-col gap-[8px]">
          {sortedBoardEntries.map((entry) => {
            const taskTitle = textField(entry.citation, 'task_title');
            const promoted = Boolean(textField(entry.citation, 'knowledge_base_id'));
            return (
              <div
                key={entry.id}
                className="rounded-[12px] border border-[#eef1f6] px-[12px] py-[10px]"
              >
                <div className="flex items-start gap-[8px]">
                  <p className="min-w-0 flex-1 text-[14px] leading-[20px] whitespace-pre-wrap text-[#18181a]">
                    {entry.content}
                  </p>
                  {entry.pinned && (
                    <Badge variant="secondary" className="shrink-0 rounded-full bg-[#e8f0ff] text-[12px] font-normal text-[#1a71ff]">
                      置顶
                    </Badge>
                  )}
                </div>
                {entry.tags.length > 0 && (
                  <div className="mt-[6px] flex flex-wrap gap-[6px]">
                    {entry.tags.map((tag) => (
                      <Badge
                        key={tag}
                        variant="secondary"
                        className="rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]"
                      >
                        {tag}
                      </Badge>
                    ))}
                  </div>
                )}
                <div className="mt-[8px] flex flex-wrap items-center justify-between gap-[8px]">
                  <span className="text-[12px] text-[#a7adbb]">
                    {boardSourceLabel(entry)}
                    {taskTitle ? ` · 关联任务：${taskTitle}` : ''}
                    {` · ${formatClientDateTime(entry.updated_at)}`}
                  </span>
                  <span className="flex items-center gap-[4px]">
                    <button
                      type="button"
                      disabled={promoted || promotingEntryId === entry.id}
                      onClick={() => void promoteBoardEntry(entry)}
                      className="rounded-[8px] px-[8px] py-[4px] text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6] disabled:cursor-not-allowed disabled:text-[#a7adbb]"
                    >
                      {promoted ? '已沉淀' : promotingEntryId === entry.id ? '沉淀中…' : '沉淀到知识库'}
                    </button>
                    <button
                      type="button"
                      onClick={() => void togglePinEntry(entry)}
                      className="rounded-[8px] px-[8px] py-[4px] text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6]"
                    >
                      {entry.pinned ? '取消置顶' : '置顶'}
                    </button>
                    <button
                      type="button"
                      onClick={() => openEditEntry(entry)}
                      className="rounded-[8px] px-[8px] py-[4px] text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6]"
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => void archiveBoardEntry(entry)}
                      className="rounded-[8px] px-[8px] py-[4px] text-[12px] text-[#858b9c] transition-colors hover:bg-[#fce7e7] hover:text-[#f5483b]"
                    >
                      归档
                    </button>
                  </span>
                </div>
              </div>
            );
          })}
          {sortedBoardEntries.length === 0 && (
            <p className="py-[12px] text-center text-[12px] text-[#a7adbb]">暂无黑板条目</p>
          )}
        </div>
        <div className="mt-[12px] flex items-center gap-[8px]">
          <Input
            value={boardContent}
            onChange={(event) => setBoardContent(event.target.value)}
            placeholder="输入黑板内容"
            aria-label="输入黑板内容"
            disabled={postingEntry}
            className="h-[36px] flex-1 rounded-[10px] border-[#e3e7f1] text-[14px]"
          />
          <Input
            value={boardTags}
            onChange={(event) => setBoardTags(event.target.value)}
            placeholder="标签（逗号分隔，可选）"
            aria-label="标签（逗号分隔，可选）"
            disabled={postingEntry}
            className="h-[36px] w-[200px] shrink-0 rounded-[10px] border-[#e3e7f1] text-[14px]"
          />
          <Button
            type="button"
            disabled={postingEntry || !boardContent.trim()}
            onClick={() => void addBoardEntry()}
            className="h-[36px] shrink-0 rounded-[10px] bg-[#18181a] px-[16px] text-[14px] font-normal text-white hover:bg-[#303030]"
          >
            {postingEntry ? '添加中…' : '添加'}
          </Button>
        </div>
      </section>

      <section aria-label="任务看板" className="mt-[20px] rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
        <div className="mb-[12px] flex items-center justify-between">
          <h2 className="text-[16px] font-medium text-[#18181a]">任务看板</h2>
          <Button
            type="button"
            onClick={() => setTaskDialogOpen(true)}
            className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] font-normal text-white hover:bg-[#303030]"
          >
            新建任务
          </Button>
        </div>
        <div className="grid grid-cols-1 gap-[12px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-7">
          {TASK_STATUS_COLUMNS.map((column) => {
            const columnTasks = tasksByStatus.get(column.status) || [];
            return (
              <div key={column.status} className="flex min-h-[120px] flex-col gap-[8px] rounded-[12px] bg-[#f8f9fb] p-[8px]">
                <div className="flex items-center justify-between px-[4px]">
                  <span className="text-[12px] font-medium text-[#464c5e]">{column.label}</span>
                  <span className="text-[12px] text-[#a7adbb]">{columnTasks.length}</span>
                </div>
                {columnTasks.map((task) => (
                  <button
                    key={task.id}
                    type="button"
                    onClick={() => void openTask(task)}
                    className="flex flex-col gap-[6px] rounded-[10px] bg-white p-[10px] text-left shadow-[0_0_4px_rgba(0,0,0,0.04)] transition-shadow hover:shadow-[0_8px_16px_rgba(0,0,0,0.08)]"
                  >
                    <span className="text-[13px] font-medium leading-[18px] text-[#18181a]">{task.title}</span>
                    <span className="flex items-center justify-between text-[11px] text-[#858b9c]">
                      <span className="truncate">{assigneeName(task)}</span>
                      <Badge variant="secondary" className="shrink-0 rounded-full bg-[#f2f3f7] text-[10px] font-normal text-[#464c5e]">
                        {taskPriorityLabel(task.priority)}
                      </Badge>
                    </span>
                    <span className="text-[10px] text-[#a7adbb]">{`创建于 ${relativeTimeLabel(task.created_at)}`}</span>
                  </button>
                ))}
                {columnTasks.length === 0 && (
                  <p className="py-[12px] text-center text-[11px] text-[#c3c8d4]">暂无任务</p>
                )}
              </div>
            );
          })}
        </div>
      </section>

      <section aria-label="团队动态" className="mt-[20px] rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
        <h2 className="mb-[12px] text-[16px] font-medium text-[#18181a]">团队动态</h2>
        {teamEvents.length === 0 ? (
          <p className="py-[12px] text-center text-[12px] text-[#a7adbb]">暂无团队动态</p>
        ) : (
          <div className="flex flex-col gap-[10px]">
            {eventGroups.map((group) => (
              <div
                key={group.key}
                className="rounded-[12px] border border-[#eef1f6] px-[12px] py-[10px]"
              >
                {group.task ? (
                  <button
                    type="button"
                    onClick={() => void openTask(group.task as TeamTaskRead)}
                    className="mb-[6px] max-w-full truncate rounded-[8px] text-left text-[13px] font-medium text-[#18181a] transition-colors hover:text-[#1a71ff]"
                    title={group.title}
                  >
                    {group.title}
                  </button>
                ) : (
                  <p className="mb-[6px] text-[13px] font-medium text-[#464c5e]">{group.title}</p>
                )}
                <ol className="flex flex-col gap-[4px]">
                  {group.events.map((event) => (
                    <li
                      key={event.id}
                      className="flex items-baseline gap-[8px] px-[2px] text-[12px] leading-[18px]"
                    >
                      <span className="shrink-0 text-[#464c5e]">{teamEventTypeLabel(event.event_type)}</span>
                      <span className="shrink-0 text-[#a7adbb]">{eventActorLabel(event)}</span>
                      <span className="ml-auto shrink-0 text-[#a7adbb]">
                        {relativeTimeLabel(event.created_at)}
                      </span>
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </div>
        )}
      </section>

      <Dialog
        open={Boolean(activeTask)}
        onOpenChange={(open) => {
          if (!open) setActiveTask(null);
        }}
      >
        <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[640px]">
          <DialogTitle className="shrink-0 px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            {activeTask?.title || '任务详情'}
          </DialogTitle>
          {activeTask && (
            <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[24px] pb-[16px]">
              <div className="flex flex-wrap items-center gap-[8px] text-[12px] text-[#757f9c]">
                <Badge variant="secondary" className="rounded-full bg-[#f2f3f7] font-normal text-[#464c5e]">
                  {TASK_STATUS_COLUMNS.find((column) => column.status === activeTask.status)?.label || activeTask.status}
                </Badge>
                <span>{`负责人：${assigneeName(activeTask)}`}</span>
                {biddingWinnerId && (
                  <Badge variant="secondary" className="rounded-full bg-[#e8f0ff] font-normal text-[#1a71ff]">
                    竞标胜出
                  </Badge>
                )}
                <span>{`优先级：${taskPriorityLabel(activeTask.priority)}`}</span>
                {activeTask.session_id && (
                  <button
                    type="button"
                    onClick={() => navigate(`${EnterpriseRoute.Chat}/${activeTask.session_id}`)}
                    className="text-[12px] text-[#1a71ff] transition-colors hover:underline"
                  >
                    查看执行会话
                  </button>
                )}
              </div>

              {activeTask.description && (
                <section aria-label="任务描述">
                  <h3 className="mb-[4px] text-[13px] font-medium text-[#464c5e]">描述</h3>
                  <p className="text-[13px] leading-[20px] whitespace-pre-wrap text-[#18181a]">{activeTask.description}</p>
                </section>
              )}

              {(reportSummary || reportFullReply) && (
                <section aria-label="执行报告">
                  <h3 className="mb-[4px] text-[13px] font-medium text-[#464c5e]">执行报告</h3>
                  {reportSummary && (
                    <p className="text-[13px] leading-[20px] whitespace-pre-wrap text-[#18181a]">{reportSummary}</p>
                  )}
                  {reportFullReply && (
                    <pre className="mt-[6px] max-h-[200px] overflow-y-auto rounded-[10px] bg-[#f8f9fb] p-[10px] text-[12px] leading-[18px] whitespace-pre-wrap text-[#464c5e]">
                      {reportFullReply}
                    </pre>
                  )}
                </section>
              )}

              {reviewVerdict && (() => {
                const banner = REVIEW_BANNERS[reviewVerdict] || {
                  ...DEFAULT_REVIEW_BANNER,
                  label: reviewVerdict,
                };
                return (
                  <section aria-label="验收结论">
                    <div className={cn('rounded-[12px] border px-[14px] py-[12px]', banner.bannerClass)}>
                      <p className="text-[15px] font-semibold">{banner.label}</p>
                      {reviewComment && (
                        <blockquote
                          className={cn(
                            'mt-[8px] border-l-4 pl-[10px] text-[14px] leading-[22px] whitespace-pre-wrap',
                            banner.quoteClass,
                          )}
                        >
                          {reviewComment}
                        </blockquote>
                      )}
                    </div>
                  </section>
                );
              })()}

              {bidRounds.length > 0 && (
                <section aria-label="竞标竞技场">
                  <h3 className="mb-[4px] text-[13px] font-medium text-[#464c5e]">竞标竞技场</h3>
                  <BiddingArena
                    bids={activeTask.bids || []}
                    winnerId={biddingWinnerId}
                    agents={agents}
                    resolveName={(agentId) => memberNameByAgentId.get(agentId) || ''}
                  />
                </section>
              )}

              {AWARD_OVERRIDABLE_STATUSES.has(activeTask.status) && (
                <section aria-label="改判执行者" className="rounded-[12px] border border-[#eef1f6] p-[12px]">
                  <h3 className="mb-[8px] text-[13px] font-medium text-[#464c5e]">改判执行者</h3>
                  <div className="flex flex-col gap-[8px]">
                    <Select value={awardAgentId} onValueChange={setAwardAgentId}>
                      <SelectTrigger
                        aria-label="选择执行者"
                        className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[13px]"
                      >
                        <SelectValue placeholder="选择执行者" />
                      </SelectTrigger>
                      <SelectContent>
                        {awardCandidates.map((member) => (
                          <SelectItem key={member.agent_id} value={member.agent_id}>
                            {member.agent_name || member.agent_id}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Textarea
                      value={awardComment}
                      onChange={(event) => setAwardComment(event.target.value)}
                      placeholder="改判说明（可选）"
                      aria-label="改判说明（可选）"
                      rows={2}
                      className="text-[13px]"
                    />
                    <div>
                      <Button
                        type="button"
                        disabled={awarding || !awardAgentId}
                        onClick={() => void awardOverride()}
                        className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] font-normal text-white hover:bg-[#303030]"
                      >
                        {awarding ? '提交中…' : '确认改判'}
                      </Button>
                    </div>
                  </div>
                </section>
              )}

              <section aria-label="事件时间线">
                <h3 className="mb-[4px] text-[13px] font-medium text-[#464c5e]">事件时间线</h3>
                {(activeTask.events || []).length === 0 ? (
                  <p className="text-[12px] text-[#a7adbb]">暂无事件</p>
                ) : (
                  <ol className="flex flex-col gap-[6px]">
                    {(activeTask.events || []).map((event) => (
                      <li key={event.id} className="flex items-baseline gap-[8px] text-[12px] leading-[18px]">
                        <span className="shrink-0 text-[#a7adbb]">
                          {formatClientDateTime(event.created_at)}
                        </span>
                        <span className="text-[#464c5e]">{event.event_type}</span>
                        <span className="text-[#a7adbb]">{event.actor_type}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </section>

              {OVERRIDABLE_STATUSES.has(activeTask.status) && (
                <section aria-label="人工改判" className="rounded-[12px] border border-[#eef1f6] p-[12px]">
                  <h3 className="mb-[8px] text-[13px] font-medium text-[#464c5e]">人工改判</h3>
                  <Textarea
                    value={overrideComment}
                    onChange={(event) => setOverrideComment(event.target.value)}
                    placeholder="改判意见（可选）"
                    aria-label="改判意见（可选）"
                    rows={2}
                    className="mb-[8px] text-[13px]"
                  />
                  <div className="flex items-center gap-[8px]">
                    <Button
                      type="button"
                      disabled={overriding}
                      onClick={() => void overrideTask('approve')}
                      className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] font-normal text-white hover:bg-[#303030]"
                    >
                      通过
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      disabled={overriding}
                      onClick={() => void overrideTask('rework')}
                      className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[16px] text-[13px] font-normal text-[#464c5e]"
                    >
                      退回重做
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      disabled={overriding}
                      onClick={() => void overrideTask('escalate')}
                      className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[16px] text-[13px] font-normal text-[#464c5e]"
                    >
                      升级
                    </Button>
                  </div>
                </section>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={taskDialogOpen}
        onOpenChange={(open) => {
          if (!open) setTaskDialogOpen(false);
        }}
      >
        <DialogContent className="w-[calc(100%-32px)] rounded-[16px] sm:max-w-[480px]">
          <DialogTitle className="text-[16px] font-semibold text-foreground">新建任务</DialogTitle>
          <div className="flex flex-col gap-[12px]">
            <Input
              value={newTaskTitle}
              onChange={(event) => setNewTaskTitle(event.target.value)}
              placeholder="任务标题"
              aria-label="任务标题"
              className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[14px]"
            />
            <Textarea
              value={newTaskDescription}
              onChange={(event) => setNewTaskDescription(event.target.value)}
              placeholder="任务描述（可选）"
              aria-label="任务描述（可选）"
              rows={3}
              className="text-[14px]"
            />
            <Select value={newTaskPriority} onValueChange={setNewTaskPriority}>
              <SelectTrigger aria-label="优先级" className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[14px]">
                <SelectValue placeholder="优先级" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="high">高</SelectItem>
                <SelectItem value="medium">中</SelectItem>
                <SelectItem value="low">低</SelectItem>
              </SelectContent>
            </Select>
            <Select value={newTaskAssignee} onValueChange={setNewTaskAssignee}>
              <SelectTrigger aria-label="执行者" className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[14px]">
                <SelectValue placeholder="执行者" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={POOL_ASSIGNEE_VALUE}>投入任务池竞标</SelectItem>
                {(team?.members || []).map((member) => (
                  <SelectItem key={member.agent_id} value={member.agent_id}>
                    {member.agent_name || member.agent_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="flex items-center justify-end gap-[8px]">
              <Button
                type="button"
                variant="outline"
                disabled={creatingTask}
                onClick={() => setTaskDialogOpen(false)}
                className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[16px] text-[13px] font-normal text-[#464c5e]"
              >
                取消
              </Button>
              <Button
                type="button"
                disabled={creatingTask || !newTaskTitle.trim()}
                onClick={() => void createTask()}
                className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] font-normal text-white hover:bg-[#303030]"
              >
                {creatingTask ? '创建中…' : '创建'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(editingEntry)}
        onOpenChange={(open) => {
          if (!open) setEditingEntry(null);
        }}
      >
        <DialogContent className="w-[calc(100%-32px)] rounded-[16px] sm:max-w-[480px]">
          <DialogTitle className="text-[16px] font-semibold text-foreground">编辑黑板条目</DialogTitle>
          <div className="flex flex-col gap-[12px]">
            <Textarea
              value={editContent}
              onChange={(event) => setEditContent(event.target.value)}
              placeholder="黑板内容"
              aria-label="黑板内容"
              rows={3}
              className="text-[14px]"
            />
            <Input
              value={editTags}
              onChange={(event) => setEditTags(event.target.value)}
              placeholder="标签（逗号分隔，可选）"
              aria-label="编辑标签（逗号分隔，可选）"
              className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[14px]"
            />
            <div className="flex items-center justify-end gap-[8px]">
              <Button
                type="button"
                variant="outline"
                disabled={savingEntry}
                onClick={() => setEditingEntry(null)}
                className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[16px] text-[13px] font-normal text-[#464c5e]"
              >
                取消
              </Button>
              <Button
                type="button"
                disabled={savingEntry || !editContent.trim()}
                onClick={() => void saveEditEntry()}
                className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] font-normal text-white hover:bg-[#303030]"
              >
                {savingEntry ? '保存中…' : '保存'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
