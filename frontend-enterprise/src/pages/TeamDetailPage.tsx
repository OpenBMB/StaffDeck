import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Crown, Download, Eye, FileJson, LoaderCircle, MessageCircle } from 'lucide-react';
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
import { MarkdownMessage } from './chat/chatHelpers';
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
  tl_review_skipped: '项目领导免验收',
  tl_review_unparsed: '项目领导验收解析失败',
  tl_review_repair_failed: '项目领导验收修复失败',
  tl_review_approve: '项目领导验收通过',
  tl_review_rework: '项目领导退回重做',
  tl_review_escalate: '项目领导升级',
  review_override_approve: '人工改判通过',
  review_override_rework: '人工改判退回',
  review_override_escalate: '人工改判升级',
  blackboard_written: '写入黑板',
  wake_claimed: '任务唤醒已认领',
  wake_completed: '任务唤醒已完成',
  wake_failed: '任务唤醒失败',
  wake_recovered: '恢复中断的任务唤醒',
  member_execution_resumed: '恢复成员执行',
  member_execution_skipped: '跳过重复成员执行',
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

type TeamLogMessage = {
  id?: string;
  role?: string;
  content?: string;
  created_at?: string;
};

type TeamLogSession = {
  session?: Record<string, unknown>;
  messages?: TeamLogMessage[];
  feedback?: Array<Record<string, unknown>>;
  traces?: unknown[];
  events?: Array<Record<string, unknown>>;
  tool_invocations?: Array<Record<string, unknown>>;
};

type TeamLogPayload = {
  schema_version?: string;
  exported_at?: string;
  team?: Record<string, unknown>;
  summary?: {
    task_count?: number;
    wake_event_count?: number;
    blackboard_entry_count?: number;
    session_count?: number;
  };
  tasks?: unknown[];
  wake_events?: unknown[];
  blackboard_entries?: unknown[];
  sessions?: TeamLogSession[];
};

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
  const [startingChat, setStartingChat] = useState(false);
  const [teamLogOpen, setTeamLogOpen] = useState(false);
  const [teamLog, setTeamLog] = useState<TeamLogPayload | null>(null);
  const [loadingTeamLog, setLoadingTeamLog] = useState(false);
  const [promotingEntryId, setPromotingEntryId] = useState<string | null>(null);
  const openedTaskParamRef = useRef<string | null>(null);
  const memberScrollRef = useRef<HTMLDivElement | null>(null);
  const [memberScrollEdges, setMemberScrollEdges] = useState({
    overflow: false,
    left: false,
    right: false,
  });

  const teamMemberKey = useMemo(
    () => (team?.members || []).map((member) => `${member.id}:${member.role}`).join('|'),
    [team?.members],
  );

  const updateMemberScrollEdges = useCallback(() => {
    const node = memberScrollRef.current;
    if (!node) return;
    const maxScrollLeft = Math.max(0, node.scrollWidth - node.clientWidth);
    const overflow = maxScrollLeft > 1;
    const nextEdges = {
      overflow,
      left: overflow && node.scrollLeft > 1,
      right: overflow && node.scrollLeft < maxScrollLeft - 1,
    };
    setMemberScrollEdges((current) => (
      current.overflow === nextEdges.overflow
      && current.left === nextEdges.left
      && current.right === nextEdges.right
        ? current
        : nextEdges
    ));
  }, []);

  useEffect(() => {
    const node = memberScrollRef.current;
    if (!node) return;
    node.scrollLeft = 0;
    updateMemberScrollEdges();

    node.addEventListener('scroll', updateMemberScrollEdges, { passive: true });
    window.addEventListener('resize', updateMemberScrollEdges);
    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(updateMemberScrollEdges);
    resizeObserver?.observe(node);
    if (node.firstElementChild instanceof HTMLElement) {
      resizeObserver?.observe(node.firstElementChild);
    }
    return () => {
      node.removeEventListener('scroll', updateMemberScrollEdges);
      window.removeEventListener('resize', updateMemberScrollEdges);
      resizeObserver?.disconnect();
    };
  }, [teamMemberKey, updateMemberScrollEdges]);

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
      notify.success('已更换项目领导');
      await loadTeam();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更换项目领导失败');
    }
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

  async function startTeamChat() {
    if (!teamId || startingChat) return;
    setStartingChat(true);
    try {
      const result = await api.post<{ session_id: string }>(
        `/api/enterprise/teams/${teamId}/tl/session`,
        { tenant_id: TENANT_ID },
      );
      if (!result.session_id) throw new Error('未返回团队群聊');
      navigate(`${EnterpriseRoute.Chat}/${result.session_id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '开始团队对话失败');
    } finally {
      setStartingChat(false);
    }
  }

  async function openTeamLog() {
    if (!teamId || loadingTeamLog) return;
    setTeamLogOpen(true);
    setLoadingTeamLog(true);
    try {
      const payload = await api.get<TeamLogPayload>(
        `/api/enterprise/teams/${teamId}/export?tenant_id=${encodeURIComponent(TENANT_ID)}`,
      );
      setTeamLog(payload);
    } catch (error) {
      setTeamLogOpen(false);
      notify.error(error instanceof Error ? error.message : '加载完整日志失败');
    } finally {
      setLoadingTeamLog(false);
    }
  }

  function downloadTeamLog() {
    if (!teamLog) return;
    const blob = new Blob([JSON.stringify(teamLog, null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    const safeName = (team?.name || teamId).replace(/[^\w\-\u4e00-\u9fff]+/g, '-');
    anchor.href = url;
    anchor.download = `staffdeck-team-log-${safeName || teamId}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    notify.success('群聊完整日志已下载');
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
    if (entry.source_type === 'leader') return '项目领导';
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
    if (event.actor_type === 'tl') return '项目领导';
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

      <div className="mt-[16px] flex items-center justify-between gap-[12px]">
        <Button
          type="button"
          variant="outline"
          onClick={() => navigate(EnterpriseRoute.Teams)}
          className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[12px] text-[12px] font-normal text-[#464c5e]"
        >
          返回团队列表
        </Button>
        <div className="flex items-center gap-[8px]">
          <Button
            type="button"
            variant="outline"
            disabled={loadingTeamLog || !team}
            onClick={() => void openTeamLog()}
            className="h-[34px] gap-[6px] rounded-[10px] border-[#e3e7f1] px-[14px] text-[12px] font-normal text-[#464c5e]"
          >
            {loadingTeamLog ? <LoaderCircle className="size-[14px] animate-spin" /> : <Eye className="size-[14px]" />}
            {loadingTeamLog ? '加载中…' : '查看完整日志'}
          </Button>
          <Button
            type="button"
            disabled={startingChat || !team}
            onClick={() => void startTeamChat()}
            className="h-[34px] gap-[6px] rounded-[10px] bg-[#18181a] px-[14px] text-[12px] font-normal text-white hover:bg-[#303030]"
          >
            <MessageCircle className="size-[14px]" />
            {startingChat ? '进入中…' : '开始对话'}
          </Button>
        </div>
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
                  <div className={cn(
                    'relative flex w-[156px] shrink-0 flex-col items-center gap-[7px] rounded-[12px] border border-[#eef1f6] bg-white px-[12px] py-[12px]',
                    isLeader && 'mt-[12px] border-[#d9e5ff] pt-[17px] shadow-[0_5px_16px_rgba(26,113,255,0.08)]',
                  )}>
                    {isLeader && (
                      <span className="absolute -top-[12px] inline-flex h-[24px] items-center gap-[4px] rounded-full border border-[#cfe0ff] bg-[#f2f6ff] px-[9px] text-[11px] font-medium text-[#1a71ff] shadow-[0_2px_7px_rgba(26,113,255,0.12)]">
                        <Crown className="size-[12px]" />
                        项目领导
                      </span>
                    )}
                    <EmployeeAvatar agent={agentById.get(member.agent_id)} size={48} radius={14} />
                    <span
                      className="max-w-full truncate text-[13px] font-medium text-[#18181a]"
                      title={member.agent_name || member.agent_id}
                    >
                      {member.agent_name || member.agent_id}
                    </span>
                    {!isLeader && (
                      <Badge
                        variant="secondary"
                        className="shrink-0 rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#858b9c]"
                      >
                        成员
                      </Badge>
                    )}
                    <div className="flex min-h-[28px] w-full items-center justify-center gap-[4px]">
                      {!isLeader && (
                        <button
                          type="button"
                          onClick={() => void promoteLeader(member.agent_id)}
                          className="shrink-0 whitespace-nowrap rounded-[8px] px-[6px] py-[4px] text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6]"
                        >
                          设为项目领导
                        </button>
                      )}
                      <button
                        type="button"
                        aria-label={`移除成员 ${member.agent_name || member.agent_id}`}
                        onClick={() => void removeMember(member.agent_id)}
                        className="shrink-0 whitespace-nowrap rounded-[8px] px-[6px] py-[4px] text-[12px] text-[#858b9c] transition-colors hover:bg-[#fce7e7] hover:text-[#f5483b]"
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
                    <div className="relative w-full">
                      {memberScrollEdges.left && (
                        <div
                          aria-hidden="true"
                          data-scroll-edge="left"
                          className="pointer-events-none absolute inset-y-0 left-0 z-10 w-[44px] bg-gradient-to-r from-white via-white/85 to-transparent"
                        />
                      )}
                      {memberScrollEdges.right && (
                        <div
                          aria-hidden="true"
                          data-scroll-edge="right"
                          className="pointer-events-none absolute inset-y-0 right-0 z-10 w-[44px] bg-gradient-to-l from-white via-white/85 to-transparent"
                        />
                      )}
                      <div
                        ref={memberScrollRef}
                        role="region"
                        aria-label="团队成员列表"
                        aria-describedby={memberScrollEdges.overflow ? 'team-member-scroll-hint' : undefined}
                        tabIndex={0}
                        className="max-w-full overflow-x-auto overscroll-x-contain pb-[8px] outline-none [scrollbar-color:#cfd5e2_transparent] [scrollbar-width:thin] focus-visible:ring-2 focus-visible:ring-[#a9c7ff] focus-visible:ring-offset-2 [&::-webkit-scrollbar]:h-[6px] [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-[#cfd5e2] [&::-webkit-scrollbar-track]:bg-transparent"
                      >
                        <div className="flex w-max min-w-full justify-center gap-[12px] px-[4px]">
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
                      </div>
                      {memberScrollEdges.overflow && (
                        <p
                          id="team-member-scroll-hint"
                          className="mt-[5px] flex items-center justify-center gap-[5px] text-[11px] text-[#858b9c]"
                        >
                          <ChevronLeft className="size-[12px]" aria-hidden="true" />
                          横向滑动查看更多成员
                          <ChevronRight className="size-[12px]" aria-hidden="true" />
                        </p>
                      )}
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
                  <span className="rounded-full bg-[#f2f3f7] px-[8px] py-[3px] text-[11px] text-[#646b7c]">
                    内部执行记录已归档
                  </span>
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

      <TeamLogDialog
        open={teamLogOpen}
        loading={loadingTeamLog}
        log={teamLog}
        onClose={() => setTeamLogOpen(false)}
        onDownload={downloadTeamLog}
      />

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

function TeamLogDialog({
  open,
  loading,
  log,
  onClose,
  onDownload,
}: {
  open: boolean;
  loading: boolean;
  log: TeamLogPayload | null;
  onClose: () => void;
  onDownload: () => void;
}) {
  const sessions = log?.sessions || [];
  const summary = log?.summary || {};
  const teamName = String(log?.team?.name || '');
  const [expandedSessionIds, setExpandedSessionIds] = useState<Set<string>>(() => new Set());

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[14px] p-0 sm:max-w-[1180px]"
      >
        <div className="flex shrink-0 items-center justify-between gap-[16px] border-b border-[#edf0f5] px-[24px] py-[18px] pr-[54px]">
          <div className="flex min-w-0 items-center gap-[10px]">
            <span className="flex size-[32px] shrink-0 items-center justify-center rounded-[10px] bg-[#eef4ff] text-[#1a71ff]">
              <FileJson className="size-[16px]" />
            </span>
            <div className="min-w-0">
              <DialogTitle className="truncate text-[15px] font-semibold text-[#18181a]">
                团队完整日志
              </DialogTitle>
              <p className="mt-[2px] truncate text-[11px] text-[#858b9c]">
                {teamName || '团队'} · 在线查看任务调度、成员会话、模型事件和工具调用
              </p>
            </div>
          </div>
          <Button
            type="button"
            variant="outline"
            disabled={!log || loading}
            onClick={onDownload}
            className="h-[32px] shrink-0 gap-[6px] rounded-[9px] border-[#e3e7f1] px-[12px] text-[12px] font-normal text-[#464c5e]"
          >
            <Download className="size-[13px]" />
            下载 JSON
          </Button>
        </div>

        {loading ? (
          <div className="flex min-h-[360px] flex-1 items-center justify-center gap-[8px] text-[13px] text-[#858b9c]">
            <LoaderCircle className="size-[18px] animate-spin text-[#1a71ff]" />
            正在加载完整日志…
          </div>
        ) : log ? (
          <div className="min-h-0 flex-1 overflow-y-auto bg-[#fafbfc] px-[24px] py-[20px]">
            <div className="grid grid-cols-2 gap-[10px] sm:grid-cols-4">
              {[
                ['任务数', summary.task_count ?? log.tasks?.length ?? 0],
                ['唤醒事件', summary.wake_event_count ?? log.wake_events?.length ?? 0],
                ['黑板条目', summary.blackboard_entry_count ?? log.blackboard_entries?.length ?? 0],
                ['成员会话', summary.session_count ?? sessions.length],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-[12px] border border-[#e7eaf0] bg-white px-[14px] py-[12px]">
                  <p className="text-[11px] text-[#858b9c]">{label}</p>
                  <p className="mt-[3px] text-[20px] font-semibold tracking-[-0.02em] text-[#18181a]">{value}</p>
                </div>
              ))}
            </div>

            <section className="mt-[16px] rounded-[14px] border border-[#e3e7f1] bg-white p-[14px]">
              <div className="mb-[10px] flex items-center justify-between gap-[8px]">
                <div>
                  <h3 className="text-[13px] font-semibold text-[#18181a]">成员会话</h3>
                  <p className="mt-[2px] text-[11px] text-[#858b9c]">逐个查看成员消息、模型事件和工具调用</p>
                </div>
                <span className="rounded-full bg-[#eef4ff] px-[9px] py-[3px] text-[11px] text-[#1a71ff]">
                  {sessions.length}
                </span>
              </div>

              {sessions.length > 0 ? (
                <div className="grid gap-[8px]">
                  {sessions.map((sessionLog, index) => {
                    const session = sessionLog.session || {};
                    const messages = sessionLog.messages || [];
                    const events = sessionLog.events || [];
                    const invocations = sessionLog.tool_invocations || [];
                    const sessionId = String(session.id || session.session_id || index);
                    return (
                      <details
                        key={sessionId}
                        className="group rounded-[10px] border border-[#e7eaf0] bg-[#fcfcfd] open:bg-white"
                        onToggle={(event) => {
                          const expanded = event.currentTarget.open;
                          setExpandedSessionIds((current) => {
                            const next = new Set(current);
                            if (expanded) next.add(sessionId);
                            else next.delete(sessionId);
                            return next;
                          });
                        }}
                      >
                        <summary className="cursor-pointer list-none px-[13px] py-[11px] marker:hidden">
                          <div className="flex items-center justify-between gap-[12px]">
                            <div className="min-w-0">
                              <p className="truncate text-[12px] font-medium text-[#303442]">
                                {String(session.title || sessionId)}
                              </p>
                              <p className="mt-[2px] truncate text-[11px] text-[#858b9c]">
                                {String(session.agent_name || session.agent_id || '未指定员工')}
                                {session.status ? ` · ${String(session.status)}` : ''}
                              </p>
                            </div>
                            <div className="flex shrink-0 flex-wrap justify-end gap-[5px] text-[10px] text-[#697086]">
                              <span className="rounded-full bg-[#f1f3f7] px-[7px] py-[3px]">消息 {messages.length}</span>
                              <span className="rounded-full bg-[#f1f3f7] px-[7px] py-[3px]">事件 {events.length}</span>
                              <span className="rounded-full bg-[#f1f3f7] px-[7px] py-[3px]">工具调用 {invocations.length}</span>
                            </div>
                          </div>
                        </summary>

                        {expandedSessionIds.has(sessionId) && (
                        <div className="border-t border-[#edf0f5] px-[13px] py-[12px]">
                          <div className="grid gap-[8px]">
                            {messages.map((message, messageIndex) => {
                              const role = String(message.role || 'assistant');
                              const content = String(message.content || '');
                              return (
                                <div
                                  key={message.id || `${sessionId}-message-${messageIndex}`}
                                  className={cn('flex', role === 'user' ? 'justify-end' : 'justify-start')}
                                >
                                  <div
                                    className={cn(
                                      'max-w-[88%] rounded-[10px] px-[12px] py-[9px] text-[12px] leading-[1.65]',
                                      role === 'user'
                                        ? 'bg-[#eef4ff] text-[#24456f]'
                                        : 'border border-[#e7eaf0] bg-white text-[#303442]',
                                    )}
                                  >
                                    <div className="mb-[4px] text-[10px] font-medium text-[#858b9c]">
                                      {role === 'user' ? '用户' : role === 'assistant' ? '数字员工' : role}
                                    </div>
                                    {role === 'assistant'
                                      ? <MarkdownMessage content={content} />
                                      : <p className="whitespace-pre-wrap">{content}</p>}
                                  </div>
                                </div>
                              );
                            })}
                            {messages.length === 0 && (
                              <p className="py-[12px] text-center text-[11px] text-[#a7adbb]">暂无消息</p>
                            )}
                          </div>

                          {(events.length > 0 || invocations.length > 0) && (
                            <details className="mt-[10px] rounded-[9px] border border-[#e7eaf0] bg-[#fafbfc] px-[11px] py-[8px]">
                              <summary className="cursor-pointer text-[11px] font-medium text-[#60677a]">原始事件与工具调用</summary>
                              <pre className="mt-[8px] max-h-[420px] overflow-auto rounded-[8px] bg-[#18181a] p-[11px] text-[10px] leading-[1.55] text-[#d8e2f0]">
                                {JSON.stringify({
                                  traces: sessionLog.traces || [],
                                  events,
                                  tool_invocations: invocations,
                                }, null, 2)}
                              </pre>
                            </details>
                          )}
                        </div>
                        )}
                      </details>
                    );
                  })}
                </div>
              ) : (
                <p className="py-[24px] text-center text-[12px] text-[#a7adbb]">暂无成员会话</p>
              )}
            </section>

            <details className="mt-[12px] rounded-[12px] border border-[#e3e7f1] bg-white px-[14px] py-[11px]">
              <summary className="cursor-pointer text-[12px] font-medium text-[#464c5e]">查看调度、黑板与完整原始 JSON</summary>
              <pre className="mt-[10px] max-h-[560px] overflow-auto rounded-[8px] bg-[#18181a] p-[12px] text-[10px] leading-[1.55] text-[#d8e2f0]">
                {JSON.stringify(log, null, 2)}
              </pre>
            </details>

            <p className="mt-[10px] text-right text-[10px] text-[#a7adbb]">
              导出时间：{log.exported_at ? formatClientDateTime(log.exported_at) : '-'}
            </p>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
