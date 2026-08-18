import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronDown, ChevronRight, MessageCircle } from 'lucide-react';

import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Textarea,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

import IconPlus from '../assets/icons/plus.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';

import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatar from '../components/EmployeeAvatar';
import { EnterpriseRoute } from '../enums/routes';
import { parseBackendDateTime } from '../lib/timezone';
import type { AgentProfileRead, TeamRead, TeamThreadRead } from '../types';

export function teamStatusLabel(status: string): string {
  if (status === 'active') return '正常';
  if (status === 'archived') return '已归档';
  return status;
}

export function taskStatusLabel(status: string): string {
  if (status === 'bidding') return '竞标中';
  if (status === 'pending') return '待认领';
  if (status === 'in_progress') return '进行中';
  if (status === 'review') return '待验收';
  if (status === 'done') return '已完成';
  if (status === 'rework') return '已退回';
  if (status === 'escalated') return '已升级';
  return status;
}

export function relativeTimeLabel(iso: string): string {
  const time = parseBackendDateTime(iso).getTime();
  if (Number.isNaN(time)) return '';
  const minutes = Math.floor((Date.now() - time) / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return parseBackendDateTime(iso).toLocaleDateString();
}

export function teamLeaderName(team: TeamRead): string {
  const leader = (team.members || []).find((member) => member.role === 'leader');
  return leader?.agent_name || '未设置';
}

export type TeamThreadTaskGroup = {
  taskId: string;
  title: string;
  status: string | null;
  latestAt: string;
  threads: TeamThreadRead[];
};

export type TeamThreadTree = {
  teamId: string;
  teamName: string;
  latestAt: string;
  tlThreads: TeamThreadRead[];
  tasks: TeamThreadTaskGroup[];
};

const THREAD_TASK_PREFIXES = ['团队任务验收:', '团队任务验收：', '团队任务:', '团队任务：', '团队竞标:', '团队竞标：'];

function stripThreadPrefix(title: string): string {
  for (const prefix of THREAD_TASK_PREFIXES) {
    if (title.startsWith(prefix)) return title.slice(prefix.length);
  }
  return title;
}

function latestOf(items: TeamThreadRead[]): string {
  return items.reduce((latest, item) => {
    const time = parseBackendDateTime(item.updated_at).getTime();
    return time > parseBackendDateTime(latest).getTime() ? item.updated_at : latest;
  }, items[0]?.updated_at ?? '');
}

/** 把平铺的团队线程组装成 团队 → 任务 → 线程 的树，供动态区树状展示。 */
export function buildThreadTree(threads: TeamThreadRead[]): TeamThreadTree[] {
  const byTeam = new Map<string, TeamThreadRead[]>();
  for (const thread of threads) {
    const list = byTeam.get(thread.team_id) || [];
    list.push(thread);
    byTeam.set(thread.team_id, list);
  }
  const tree: TeamThreadTree[] = [];
  for (const [teamId, teamThreads] of byTeam) {
    const tlThreads = teamThreads
      .filter((thread) => !thread.task_id)
      .sort((a, b) => parseBackendDateTime(b.updated_at).getTime() - parseBackendDateTime(a.updated_at).getTime());
    const byTask = new Map<string, TeamThreadRead[]>();
    for (const thread of teamThreads) {
      if (!thread.task_id) continue;
      const list = byTask.get(thread.task_id) || [];
      list.push(thread);
      byTask.set(thread.task_id, list);
    }
    const tasks: TeamThreadTaskGroup[] = [];
    for (const [taskId, taskThreads] of byTask) {
      taskThreads.sort(
        (a, b) => parseBackendDateTime(b.updated_at).getTime() - parseBackendDateTime(a.updated_at).getTime(),
      );
      const titled = taskThreads.find((thread) => thread.title.startsWith('团队任务')) || taskThreads[0];
      tasks.push({
        taskId,
        title: stripThreadPrefix(titled.title),
        status: taskThreads.find((thread) => thread.task_status)?.task_status ?? null,
        latestAt: latestOf(taskThreads),
        threads: taskThreads,
      });
    }
    tasks.sort((a, b) => parseBackendDateTime(b.latestAt).getTime() - parseBackendDateTime(a.latestAt).getTime());
    tree.push({
      teamId,
      teamName: teamThreads[0]?.team_name || teamId,
      latestAt: latestOf(teamThreads),
      tlThreads,
      tasks,
    });
  }
  tree.sort((a, b) => parseBackendDateTime(b.latestAt).getTime() - parseBackendDateTime(a.latestAt).getTime());
  return tree;
}

export default function TeamsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onLogout?: () => void;
}) {
  const [teams, setTeams] = useState<TeamRead[]>([]);
  const [threads, setThreads] = useState<TeamThreadRead[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<TeamRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [startingTeamId, setStartingTeamId] = useState('');
  const [expandedTeams, setExpandedTeams] = useState<Set<string> | null>(null);
  const navigate = useNavigate();

  const threadTree = buildThreadTree(threads);
  // 每个团队的实时任务概况（threads 按 updated_at 倒序，同任务首次出现即最新状态）
  const teamTaskCounts = new Map<string, { active: number; attention: number }>();
  {
    const seen = new Set<string>();
    for (const thread of threads) {
      if (!thread.task_id || !thread.task_status) continue;
      const key = `${thread.team_id}:${thread.task_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const counts = teamTaskCounts.get(thread.team_id) || { active: 0, attention: 0 };
      if (['pending', 'bidding', 'in_progress'].includes(thread.task_status)) counts.active += 1;
      else if (thread.task_status === 'review' || thread.task_status === 'escalated') counts.attention += 1;
      teamTaskCounts.set(thread.team_id, counts);
    }
  }
  // 默认只展开最新动态的团队，避免动态刷屏
  const expanded = expandedTeams ?? new Set(threadTree.slice(0, 1).map((node) => node.teamId));

  function toggleTeamExpand(teamId: string) {
    const next = new Set(expanded);
    if (next.has(teamId)) next.delete(teamId);
    else next.add(teamId);
    setExpandedTeams(next);
  }

  function renderThreadRow(thread: TeamThreadRead) {
    return (
      <button
        key={`${thread.kind}:${thread.session_id}:${thread.task_id || ''}`}
        type="button"
        onClick={() => openThread(thread)}
        className="flex w-full items-center gap-[8px] rounded-[10px] px-[10px] py-[8px] text-left transition-colors hover:bg-[#f6f7fa]"
      >
        <Badge
          variant="secondary"
          className={
            thread.kind === 'tl_chat'
              ? 'shrink-0 rounded-full bg-[#e8f0ff] text-[12px] font-normal text-[#1a71ff]'
              : 'shrink-0 rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]'
          }
        >
          {thread.kind === 'tl_chat' ? '项目领导对话' : '任务'}
        </Badge>
        <span className="min-w-0 flex-1 truncate text-[13px] text-[#18181a]" title={thread.title}>
          {thread.title}
        </span>
        <span className="shrink-0 text-[12px] text-[#a7adbb]">{relativeTimeLabel(thread.updated_at)}</span>
      </button>
    );
  }

  async function load() {
    setLoading(true);
    try {
      const rows = await api.get<TeamRead[]>(`/api/enterprise/teams?tenant_id=${TENANT_ID}`);
      setTeams(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载团队失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    void loadThreads();
    // 员工列表仅用于团队卡片的成员头像映射，失败不影响主流程
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  async function loadThreads() {
    try {
      const rows = await api.get<TeamThreadRead[]>(`/api/enterprise/team-threads?tenant_id=${TENANT_ID}`);
      setThreads(rows);
    } catch {
      setThreads([]);
    }
  }

  function openThread(thread: TeamThreadRead) {
    if (thread.kind === 'tl_chat') {
      navigate(`${EnterpriseRoute.Chat}/${thread.session_id}`);
      return;
    }
    const base = `${EnterpriseRoute.Teams}/${thread.team_id}`;
    navigate(thread.task_id ? `${base}?task=${thread.task_id}` : base);
  }

  async function startTeamChat(team: TeamRead) {
    if (startingTeamId) return;
    setStartingTeamId(team.id);
    try {
      const result = await api.post<{ session_id: string }>(
        `/api/enterprise/teams/${team.id}/tl/session`,
        { tenant_id: TENANT_ID },
      );
      if (!result.session_id) throw new Error('未返回团队群聊');
      navigate(`${EnterpriseRoute.Chat}/${result.session_id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '开始团队对话失败');
    } finally {
      setStartingTeamId('');
    }
  }

  async function createTeam() {
    const trimmed = name.trim();
    if (!trimmed) {
      notify.error('请输入团队名称');
      return;
    }
    setCreating(true);
    try {
      await api.post<TeamRead>('/api/enterprise/teams', {
        tenant_id: TENANT_ID,
        name: trimmed,
        description: description.trim() || undefined,
      });
      notify.success('团队已创建');
      setCreateOpen(false);
      setName('');
      setDescription('');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建团队失败');
    } finally {
      setCreating(false);
    }
  }

  async function confirmDelete() {
    const target = deleteTarget;
    if (!target) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/teams/${target.id}?tenant_id=${TENANT_ID}`);
      notify.success('团队已删除');
      setDeleteTarget(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除团队失败');
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title="我的团队"
        description="在管理端组建团队、设置项目领导并跟踪任务"
      />

      {(() => {
        const totalMembers = teams.reduce((sum, team) => sum + (team.members || []).length, 0);
        // threads 按 updated_at 倒序，task 首次出现即最新状态
        const latestTaskStatus = new Map<string, string>();
        for (const thread of threads) {
          if (thread.task_id && thread.task_status && !latestTaskStatus.has(thread.task_id)) {
            latestTaskStatus.set(thread.task_id, thread.task_status);
          }
        }
        const statuses = [...latestTaskStatus.values()];
        const activeTasks = statuses.filter((status) => ['pending', 'bidding', 'in_progress'].includes(status)).length;
        const attentionTasks = statuses.filter((status) => status === 'review' || status === 'escalated').length;
        const summaryCardClass =
          'flex h-[100px] flex-1 basis-[220px] items-center gap-[16px] rounded-[20px] bg-[#f6f6f6] px-[32px] py-[20px] text-left transition-shadow';
        const summaryStats = [
          { key: 'all', value: teams.length, label: '团队总数', sub: `${totalMembers} 名成员` },
          { key: 'active', value: activeTasks, label: '进行中任务', sub: '正在推进' },
          { key: 'attention', value: attentionTasks, label: '待处理', sub: '需要人工介入' },
        ];
        return (
          <div className="my-[36px] flex flex-wrap items-stretch gap-[20px]" aria-label="团队统计">
            {summaryStats.map((stat) => (
              <div key={stat.key} className={summaryCardClass}>
                <span className="shrink-0 text-[34px] font-semibold leading-none text-[#18181A]">{stat.value}</span>
                <span className="flex min-w-0 flex-col gap-[4px]">
                  <span className="whitespace-nowrap text-[14px] text-[#464C5E]">{stat.label}</span>
                  <span className="whitespace-nowrap text-[12px] text-[#757F9C]">{stat.sub}</span>
                </span>
              </div>
            ))}
            <button
              data-guide-target="teams-create"
              type="button"
              onClick={() => setCreateOpen(true)}
              className={`${summaryCardClass} hover:shadow-[0_16px_30px_0_rgba(0,0,0,0.10)]`}
            >
              <span className="grid size-[38px] shrink-0 place-items-center text-[#18181A]">
                <IconPlus className="size-[38px]" />
              </span>
              <span className="flex min-w-0 flex-col gap-[4px]">
                <span className="whitespace-nowrap text-[14px] text-[#464C5E]">创建新团队</span>
                <span className="whitespace-nowrap text-[12px] text-[#757F9C]">几步搭好你的团队</span>
              </span>
            </button>
          </div>
        );
      })()}

      <div className="mt-[16px] grid grid-cols-1 content-start gap-[20px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
        {teams.map((team) => {
          const members = team.members || [];
          const leader = members.find((member) => member.role === 'leader') || null;
          const ordered = leader ? [leader, ...members.filter((member) => member.id !== leader.id)] : members;
          const stacked = ordered.slice(0, 4);
          const extraCount = members.length - stacked.length;
          const counts = teamTaskCounts.get(team.id) || { active: 0, attention: 0 };
          return (
            <div
              key={team.id}
              role="button"
              tabIndex={0}
              onClick={() => navigate(`${EnterpriseRoute.Teams}/${team.id}`)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  navigate(`${EnterpriseRoute.Teams}/${team.id}`);
                }
              }}
              className="group cursor-pointer rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)] transition-all duration-200 hover:-translate-y-[2px] hover:shadow-[0_18px_36px_-12px_rgba(70,76,94,0.28)] active:translate-y-0 active:scale-[0.99]"
            >
              {/* 成员合影：项目领导居大带标记，悬浮时成员扇形散开 */}
              <div className="flex items-end justify-between">
                <div className="flex items-end">
                  {stacked.map((member, index) => {
                    const isLeader = leader?.id === member.id;
                    const memberAgent = agents.find((agent) => agent.id === member.agent_id) || null;
                    return (
                      <span
                        key={member.id}
                        className={index > 0 ? '-ml-[16px] transition-all duration-200 group-hover:-ml-[8px]' : ''}
                      >
                        <span className="relative block overflow-hidden rounded-full bg-[#f1f2f5] ring-[3px] ring-white">
                          <EmployeeAvatar agent={memberAgent} size={isLeader ? 64 : 44} />
                          {isLeader && (
                            <span className="absolute bottom-[2px] right-[2px] inline-flex h-[18px] items-center rounded-full bg-[#fff3d6] px-[5px] text-[10px] font-medium leading-none text-[#a16a00] ring-2 ring-white">
                              项目领导
                            </span>
                          )}
                        </span>
                      </span>
                    );
                  })}
                  {extraCount > 0 && (
                    <span className="-ml-[16px] grid size-[44px] place-items-center rounded-full bg-[#f2f3f7] text-[12px] text-[#464c5e] ring-[3px] ring-white transition-all duration-200 group-hover:-ml-[8px]">
                      {`+${extraCount}`}
                    </span>
                  )}
                </div>
                <Badge
                  variant="secondary"
                  className="shrink-0 rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]"
                >
                  {teamStatusLabel(team.status)}
                </Badge>
              </div>

              <div className="mt-[14px] flex flex-col gap-[10px]">
                <span className="min-w-0 truncate text-[16px] font-medium tracking-[-0.01em] text-[#18181a]" title={team.name}>
                  {team.name}
                </span>
                <p className="line-clamp-2 min-h-[34px] text-[12px] leading-[17px] text-[#757f9c]">
                  {team.description || '暂无描述'}
                </p>
                <div className="flex flex-wrap items-center gap-[6px]">
                  <span className="rounded-full bg-[#f2f3f7] px-[8px] py-[3px] text-[11px] leading-none text-[#464c5e]">
                    {`${members.length} 名成员`}
                  </span>
                  {counts.active > 0 && (
                    <span className="rounded-full bg-[#e8f0ff] px-[8px] py-[3px] text-[11px] leading-none text-[#1a71ff]">
                      {`${counts.active} 进行中`}
                    </span>
                  )}
                  {counts.attention > 0 && (
                    <span className="rounded-full bg-[#fff3d6] px-[8px] py-[3px] text-[11px] leading-none text-[#a16a00]">
                      {`${counts.attention} 待处理`}
                    </span>
                  )}
                </div>
                <div className="flex items-center justify-between gap-[10px] border-t border-[#f2f4f8] pt-[10px]">
                  <span className="min-w-0 truncate text-[12px] text-[#757f9c]">
                    {`项目领导：${leader?.agent_name || '未设置'}`}
                  </span>
                  <div className="flex shrink-0 items-center gap-[4px]">
                    <button
                      type="button"
                      aria-label={`开始与团队 ${team.name} 对话`}
                      disabled={Boolean(startingTeamId)}
                      onClick={(event) => {
                        event.stopPropagation();
                        void startTeamChat(team);
                      }}
                      className="inline-flex h-[30px] items-center gap-[5px] rounded-[9px] bg-[#18181a] px-[10px] text-[11px] text-white transition-colors hover:bg-[#303030] disabled:cursor-wait disabled:opacity-50"
                    >
                      <MessageCircle className="size-[13px]" />
                      {startingTeamId === team.id ? '进入中…' : '开始对话'}
                    </button>
                    <button
                      type="button"
                      aria-label={`删除团队 ${team.name}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        setDeleteTarget(team);
                      }}
                      className="inline-grid size-[28px] shrink-0 place-items-center rounded-[8px] text-[#c3c9d6] transition-colors hover:bg-[#fce7e7] hover:text-[#f5483b]"
                    >
                      <IconTrash className="size-[14px]" />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          );
        })}
        {!loading && teams.length === 0 && (
          <div className="col-span-full flex h-[200px] items-center justify-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] text-[14px] text-[#7f879a]">
            暂无团队，点击上方「创建新团队」开始
          </div>
        )}
      </div>

      <section aria-label="团队动态" className="mt-[24px] rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
        <h2 className="mb-[12px] text-[16px] font-medium text-[#18181a]">团队动态</h2>
        <div className="flex flex-col gap-[8px]">
          {threadTree.map((node) => {
            const isExpanded = expanded.has(node.teamId);
            const threadCount = node.tlThreads.length + node.tasks.reduce((sum, task) => sum + task.threads.length, 0);
            return (
              <div key={node.teamId} className="rounded-[12px] border border-[#eef1f6]">
                <div className="flex items-center gap-[8px] px-[12px] py-[10px]">
                  <button
                    type="button"
                    aria-label={isExpanded ? `收起团队 ${node.teamName}` : `展开团队 ${node.teamName}`}
                    onClick={() => toggleTeamExpand(node.teamId)}
                    className="inline-grid size-[24px] shrink-0 place-items-center rounded-[8px] text-[#858b9c] transition-colors hover:bg-[#eef1f6]"
                  >
                    {isExpanded ? <ChevronDown className="size-[14px]" /> : <ChevronRight className="size-[14px]" />}
                  </button>
                  <button
                    type="button"
                    onClick={() => navigate(`${EnterpriseRoute.Teams}/${node.teamId}`)}
                    className="min-w-0 flex-1 truncate text-left text-[14px] font-medium text-[#18181a] hover:text-[#1a71ff]"
                    title={node.teamName}
                  >
                    {node.teamName}
                  </button>
                  <Badge variant="secondary" className="shrink-0 rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]">
                    {node.tasks.length} 任务 · {threadCount} 线程
                  </Badge>
                  <span className="shrink-0 text-[12px] text-[#a7adbb]">{relativeTimeLabel(node.latestAt)}</span>
                </div>
                {isExpanded && (
                  <div className="flex flex-col gap-[4px] border-t border-[#f2f4f8] px-[12px] py-[8px]">
                    {node.tlThreads.map((thread) => renderThreadRow(thread))}
                    {node.tasks.map((task) => (
                      <div key={task.taskId} className="flex flex-col gap-[2px]">
                        <button
                          type="button"
                          onClick={() => navigate(`${EnterpriseRoute.Teams}/${node.teamId}?task=${task.taskId}`)}
                          className="flex w-full items-center gap-[8px] rounded-[10px] bg-[#fafbfd] px-[10px] py-[8px] text-left transition-colors hover:bg-[#f2f4f9]"
                        >
                          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#464c5e]" title={task.title}>
                            {task.title}
                          </span>
                          {task.status && (
                            <Badge variant="secondary" className="shrink-0 rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]">
                              {taskStatusLabel(task.status)}
                            </Badge>
                          )}
                          <span className="shrink-0 text-[12px] text-[#a7adbb]">{relativeTimeLabel(task.latestAt)}</span>
                        </button>
                        <div className="ml-[16px] flex flex-col gap-[2px] border-l border-[#eef1f6] pl-[8px]">
                          {task.threads.map((thread) => renderThreadRow(thread))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
          {threadTree.length === 0 && (
            <p className="py-[12px] text-center text-[12px] text-[#a7adbb]">暂无团队动态</p>
          )}
        </div>
      </section>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[480px]">
          <DialogTitle className="shrink-0 px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            新建团队
          </DialogTitle>
          <div className="flex min-h-0 flex-1 flex-col gap-[12px] overflow-y-auto px-[24px] pb-[16px]">
            <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
              团队名称
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="请输入团队名称"
                aria-label="团队名称"
              />
            </label>
            <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
              团队描述
              <Textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="请输入团队描述（可选）"
                aria-label="团队描述"
                rows={3}
              />
            </label>
          </div>
          <div className="flex items-center justify-end gap-[8px] px-[24px] pb-[16px]">
            <Button
              type="button"
              variant="outline"
              disabled={creating}
              onClick={() => setCreateOpen(false)}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[16px] text-[14px] font-normal text-[#464c5e]"
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={creating}
              onClick={() => void createTeam()}
              className="h-[32px] rounded-[10px] bg-[#18181a] px-[16px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              {creating ? '创建中…' : '创建'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除团队「${deleteTarget?.name || ''}」？`}
        description="删除后团队及其任务将一并移除，操作不可撤销。"
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}
