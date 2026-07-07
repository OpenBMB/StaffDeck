import { useEffect, useMemo, useState } from 'react';
import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button as UiButton, Tabs, TabsList, TabsTrigger, notify } from '@/components/ui';
import { EnterpriseRoute } from '../enums/routes';
import IconChat from '../assets/icons/chat.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconAccount from '../assets/icons/sys-accounts.svg?react';
import IconProfileFile from '../assets/icons/profile-file.svg?react';
import IconProfileAlarm from '../assets/icons/profile-alarm.svg?react';
import IconProfileHistory from '../assets/icons/profile-history.svg?react';
import IconProfileCalendar from '../assets/icons/profile-calendar.svg?react';
import IconCapFolder from '../assets/icons/cap-folder.svg?react';
import IconCapMagicWand from '../assets/icons/cap-magicwand.svg?react';
import IconCapClipboard from '../assets/icons/cap-clipboard.svg?react';
import IconCapBriefcase from '../assets/icons/cap-briefcase.svg?react';
import IconCardArrow from '../assets/icons/card-arrow.svg?react';
import IconGrowthArrow from '../assets/icons/growth-arrow.svg?react';
import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '../components/AppHeader';
import EmployeeAvatar from '../components/EmployeeAvatar';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import StaffdeckIcon from '../components/StaffdeckIcon';
import capabilityLogs from '../assets/staffdeck/capabilityLogs.png';
import capabilityTasks from '../assets/staffdeck/capabilityTasks.png';
import capabilityTools from '../assets/staffdeck/capabilityTools.png';
import {
  canAccessEmployeeAgent,
  canManageEmployeeAgent,
  employeeDisplayName,
  employeeProfile,
  preferredEmployeeAgent,
  staffdeckDisplayText,
} from '../employee';
import type {
  AgentProfileRead,
  EnterpriseChatSessionRead,
  EnterpriseSessionDetailRead,
  FeedbackSummaryRead,
  GeneralSkillRead,
  KnowledgeBaseRead,
  ModelConfigRead,
  ScheduledTaskRead,
  SkillRead,
  ToolRead,
} from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const HEATMAP_ROWS = 7;
const HEATMAP_COLUMNS = 33;
const HEATMAP_BUCKETS = HEATMAP_ROWS * HEATMAP_COLUMNS;
// Rolling window: from the current month one year ago (left) to the current month (right).
const HEATMAP_MONTH_SLOTS = 13;
// Rows are Sun→Sat (第一行周日); labels only on 周一 / 周三 / 周五.
const HEATMAP_WEEKDAY_LABELS = ['', '周一', '', '周三', '', '周五', ''];
const HEATMAP_CELL_LEVELS = [
  'bg-[#f6f6f6] in-data-[theme=dark]:bg-[#363944]',
  'bg-[#cfd5e2] in-data-[theme=dark]:bg-[#5a6274]',
  'bg-[#9aa3ba] in-data-[theme=dark]:bg-[#7b8498]',
  'bg-[#6a7488] in-data-[theme=dark]:bg-[#a4adbf]',
  'bg-[#464c5e] in-data-[theme=dark]:bg-[#f0f2f6]',
];

type ReplyStats = {
  total: number;
  today: number;
  byDay: Record<string, number>;
};

type GrowthEvent = {
  id: string;
  kind: string;
  title: string;
  description: string;
  timestamp: string;
  icon: ReactNode;
  tone: string;
};

type GrowthTimestampSource = {
  created_at?: string;
  updated_at?: string;
  metadata?: Record<string, unknown>;
};

export default function DashboardPage({
  currentUser,
  isAdmin = false,
  forceOverall = false,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  forceOverall?: boolean;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [sessions, setSessions] = useState<EnterpriseChatSessionRead[]>([]);
  const [feedbackSummary, setFeedbackSummary] = useState<FeedbackSummaryRead | null>(null);
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTaskRead[]>([]);
  const [replyStats, setReplyStats] = useState<ReplyStats>({ total: 0, today: 0, byDay: {} });
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [avatarEditorOpen, setAvatarEditorOpen] = useState(false);
  const [profileEditorOpen, setProfileEditorOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      setAgentId((event as CustomEvent<{ agentId?: string }>).detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    Promise.all([
      api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
      api.get<SkillRead[]>(`/api/enterprise/skills?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`),
      api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<EnterpriseChatSessionRead[]>(`/api/enterprise/sessions?tenant_id=${TENANT_ID}`),
      api.get<FeedbackSummaryRead>(`/api/enterprise/feedback/summary?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
      api.get<ScheduledTaskRead[]>(`/api/enterprise/scheduled-tasks?tenant_id=${TENANT_ID}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`),
    ])
      .then(([agentRows, skillRows, generalSkillRows, kbRows, modelRows, toolRows, sessionRows, feedbackRows, taskRows]) => {
        const visibleAgents = agentRows.filter((item) => canAccessEmployeeAgent(item, currentUser, {
          activeOnly: true,
          includeOverall: isAdmin,
        }));
        setAgents(visibleAgents);
        setSkills(skillRows);
        setGeneralSkills(generalSkillRows);
        setKnowledgeBases(kbRows);
        setModels(modelRows);
        setTools(toolRows);
        setSessions(sessionRows);
        setFeedbackSummary(feedbackRows);
        setScheduledTasks(taskRows.filter((item) => item.status !== 'archived'));
        if (forceOverall) {
          const overallAgent = visibleAgents.find((item) => item.is_overall);
          if (overallAgent && overallAgent.id !== agentId) {
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, overallAgent.id);
            window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: overallAgent.id } }));
            setAgentId(overallAgent.id);
          }
          return;
        }
        if (!agentId || !visibleAgents.some((item) => item.id === agentId)) {
          const manageableAgents = visibleAgents.filter((item) => canManageEmployeeAgent(item, currentUser));
          const next = isAdmin
            ? visibleAgents.find((item) => item.is_overall)?.id || preferredEmployeeAgent(visibleAgents)?.id || ''
            : preferredEmployeeAgent(manageableAgents)?.id
              || preferredEmployeeAgent(visibleAgents)?.id
              || '';
          if (next) {
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, next);
            window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: next } }));
            setAgentId(next);
          }
        }
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载数字员工档案失败'))
      .finally(() => setLoaded(true));
  }, [agentId, currentUser, forceOverall, isAdmin]);

  const selectedAgent = (forceOverall ? agents.find((item) => item.is_overall) : agents.find((item) => item.id === agentId))
    || (isAdmin ? agents.find((item) => item.is_overall) || null : agents.find((item) => !item.is_overall) || null);
  const employeeSessions = selectedAgent?.is_overall
    ? sessions
    : sessions.filter((item) => item.agent_id === selectedAgent?.id);

  useEffect(() => {
    let cancelled = false;
    async function loadReplyStats() {
      if (!selectedAgent || selectedAgent.is_overall || employeeSessions.length === 0) {
        setReplyStats({ total: 0, today: 0, byDay: {} });
        return;
      }
      try {
        const details = await Promise.all(
          employeeSessions.map((item) => api.get<EnterpriseSessionDetailRead>(
            `/api/enterprise/sessions/${item.id}?tenant_id=${TENANT_ID}`,
          )),
        );
        if (cancelled) return;
        const byDay: Record<string, number> = {};
        let total = 0;
        details.forEach((detail) => {
          detail.messages
            .filter((item) => item.role === 'assistant')
            .forEach((item) => {
              const key = dateKey(new Date(item.created_at));
              byDay[key] = (byDay[key] || 0) + 1;
              total += 1;
            });
        });
        setReplyStats({ total, today: byDay[dateKey(new Date())] || 0, byDay });
      } catch {
        if (!cancelled) setReplyStats({ total: 0, today: 0, byDay: {} });
      }
    }
    void loadReplyStats();
    return () => {
      cancelled = true;
    };
  }, [selectedAgent?.id, selectedAgent?.is_overall, sessions]);
  const defaultModel = models.find((item) => item.is_default);
  const totalCalls = skills.reduce((sum, item) => sum + (item.total_call_count || item.call_count || 0), 0);
  const positiveFeedback = skills.reduce((sum, item) => sum + (item.total_positive_feedback_count || 0), 0);
  const negativeFeedback = skills.reduce((sum, item) => sum + (item.total_negative_feedback_count || 0), 0);

  // Avoid flashing the 开放广场 / empty state before the agents API resolves,
  // which would otherwise briefly render before the employee profile appears.
  if (!loaded && agents.length === 0) {
    return <div className="page dashboard-page" />;
  }

  if (!selectedAgent && !isAdmin) {
    return (
      <div className="page dashboard-page">
        <div className="empty-workspace-card p-[24px]">
          <h3 className="m-0 text-[20px] font-semibold text-foreground">还没有数字员工</h3>
          <p className="mt-[8px] text-[14px] text-muted-foreground">
            点击左下角「新建数字员工」开始创建，或前往员工广场选择已发布的员工。
          </p>
          <div className="mt-[16px] flex gap-[8px]">
            <UiButton onClick={() => navigate('/enterprise/agents')}>查看我的数字员工</UiButton>
            <UiButton variant="outline" onClick={() => navigate('/enterprise/feedback')}>查看对话日志</UiButton>
          </div>
        </div>
      </div>
    );
  }

  if (!selectedAgent || selectedAgent.is_overall) {
    return (
      <div className="page dashboard-page">
        <div className="page-title">
          <h3>开放广场</h3>
        </div>
        <section className="employee-hero org-hero">
          <div>
            <span className="section-kicker">开放广场</span>
            <h2 className="ui-typography">开放广场</h2>
            <p className="ui-typography">
              汇集所有可共享的 SOP、知识库、技能和工具，新建数字员工时可以从这里复制配置作为起点。
            </p>
          </div>
          <div className="employee-hero-metrics">
            <MetricTile label="员工" value={agents.filter((item) => !item.is_overall).length} />
            <MetricTile label="对话" value={sessions.length} />
            <MetricTile label="反馈" value={feedbackSummary?.total_feedback || 0} />
          </div>
        </section>
        <div className="org-dashboard-grid">
          <DashboardStat title="SOP" value={skills.length} icon={<StaffdeckIcon name="filter" />} />
          <DashboardStat title="技能" value={generalSkills.length} icon={<StaffdeckIcon name="spark" />} />
          <DashboardStat title="知识库" value={knowledgeBases.length} icon={<StaffdeckIcon name="file" />} />
          <DashboardStat title="可用工具" value={tools.filter((item) => item.enabled).length} icon={<StaffdeckIcon name="tool" />} />
          <DashboardStat title="SOP 调用" value={totalCalls} icon={<StaffdeckIcon name="chat" />} />
          <DashboardStat title="好评" value={positiveFeedback || feedbackSummary?.up_count || 0} icon={<StaffdeckIcon name="chat" />} />
          <DashboardStat title="差评" value={negativeFeedback || feedbackSummary?.down_count || 0} icon={<StaffdeckIcon name="chat" />} />
          <div className="org-dashboard-card">
            <div className="ui-card-body p-[24px]">
              <span className="org-dashboard-icon"><StaffdeckIcon name="model" /></span>
              <span className="text-[13px] text-muted-foreground">默认模型</span>
              <span className="text-[15px] text-foreground">{defaultModel ? `${defaultModel.name} / ${defaultModel.model}` : '未配置'}</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const employee = employeeProfile(selectedAgent);
  const canEditSelectedAgent = canManageEmployeeAgent(selectedAgent, currentUser);
  const activeSkills = skills.filter((item) => item.status === 'published' && item.branch_status !== 'inactive');
  const activeGeneralSkills = generalSkills.filter((item) => item.status === 'published');
  const activeKnowledge = knowledgeBases.filter((item) => item.status === 'active');
  const activeTools = tools.filter((item) => item.enabled);
  const employeeScheduledTasks = scheduledTasks.filter((item) => item.agent_id === selectedAgent.id && item.status !== 'archived');
  const activeScheduledTasks = employeeScheduledTasks.filter((item) => item.status === 'active');
  const totalFeedback = positiveFeedback + negativeFeedback;
  const positiveRate = totalFeedback ? Math.round((positiveFeedback / totalFeedback) * 100) : 0;
  const negativeRate = totalFeedback ? Math.round((negativeFeedback / totalFeedback) * 100) : 0;
  const todayRounds = replyStats.today;
  const systemPromptSummary = typeof selectedAgent.metadata?.system_prompt_summary === 'string'
    ? selectedAgent.metadata.system_prompt_summary
    : '';
  const systemSummary = compactSummary(
    staffdeckDisplayText(selectedAgent.persona_prompt || systemPromptSummary || selectedAgent.description || `${employee.roleName}，负责接收任务、调用知识库、执行 SOP 并沉淀对话质量反馈。`),
    132,
  );
  const goToLogs = () => navigate(`/enterprise/feedback?agent_id=${encodeURIComponent(selectedAgent.id)}`);

  const capabilityCards = [
    {
      route: '/enterprise/knowledge',
      title: '知识库',
      tone: 'knowledge',
      count: activeKnowledge.length,
      body: activeKnowledge.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无知识库',
      icon: <IconCapFolder className="employee-cap-glyph" />,
      dark: false,
    },
    {
      route: '/enterprise/general-skills',
      title: '技能',
      tone: 'skill',
      count: activeGeneralSkills.length,
      body: activeGeneralSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用技能',
      icon: <IconCapMagicWand className="employee-cap-glyph" />,
      dark: false,
    },
    {
      route: '/enterprise/skills',
      title: 'SOP',
      tone: 'sop',
      count: activeSkills.length,
      body: activeSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用 SOP',
      icon: <IconCapClipboard className="employee-cap-glyph" />,
      dark: false,
    },
    {
      route: '/enterprise/tools',
      title: '工具',
      tone: 'tools',
      count: activeTools.length,
      body: activeTools.slice(0, 3).map((item) => staffdeckDisplayText(item.display_name || item.name)).join(' / ') || '暂无启用工具',
      icon: <IconCapBriefcase className="employee-cap-glyph" />,
      dark: true,
      illustration: capabilityTools,
    },
    {
      route: '/enterprise/scheduled-tasks',
      title: '定时任务',
      tone: 'tasks',
      count: activeScheduledTasks.length,
      body: activeScheduledTasks.slice(0, 2).map((item) => staffdeckDisplayText(item.title)).join(' / ') || '暂无启用定时任务',
      icon: <IconProfileAlarm className="employee-cap-glyph" />,
      dark: true,
      illustration: capabilityTasks,
    },
    {
      route: `/enterprise/feedback?agent_id=${encodeURIComponent(selectedAgent.id)}`,
      title: '对话日志',
      tone: 'logs',
      count: replyStats.total,
      body: staffdeckDisplayText(employeeSessions[0]?.summary || employeeSessions[0]?.last_agent_question || '暂无对话任务'),
      icon: <IconProfileCalendar className="employee-cap-glyph" />,
      dark: true,
      illustration: capabilityLogs,
    },
  ];

  const growthItems = growthTimeline(activeSkills, activeGeneralSkills, activeTools);

  const heroActionButtonClass = 'inline-flex items-center justify-center gap-[4px] py-[8px] px-[12px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white text-[12px] font-normal text-[#858b9c] shadow-[0px_6px_6px_rgba(0,0,0,0.05)] hover:bg-[#f6f6f6] hover:text-[#858b9c]';
  const heroAvatar = (
    <EmployeeAvatar
      agent={selectedAgent}
      width={136}
      height={160}
      radius={0}
      fit="contain"
      objectPosition="center bottom"
      style={{ background: 'transparent', border: 'none', boxShadow: 'none', overflow: 'visible' }}
    />
  );

  return (
    <div className="min-h-full w-full min-w-0 max-w-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex flex-wrap items-center gap-x-9 gap-y-6 pt-1 pl-1">
            <div className="flex shrink-0 flex-col items-center">
              {canEditSelectedAgent ? (
                <button
                  type="button"
                  onClick={() => setAvatarEditorOpen(true)}
                  aria-label="更换头像"
                  className="group relative block cursor-pointer border-0 bg-transparent p-0"
                >
                  {heroAvatar}
                  <span className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center justify-center gap-1 bg-black/45 py-1 text-[11px] text-white opacity-0 transition-opacity group-hover:opacity-100">
                    <IconAccount className="size-3" />
                    更换头像
                  </span>
                </button>
              ) : (
                heroAvatar
              )}
              <div className="flex items-center gap-4">
                <UiButton
                  variant="outline"
                  className={heroActionButtonClass}
                  onClick={() => { window.location.href = '/workspace/chat'; }}
                >
                  <IconChat className="size-[14px]" />
                  去对话
                </UiButton>
                {canEditSelectedAgent && (
                  <UiButton
                    variant="outline"
                    className={heroActionButtonClass}
                    onClick={() => setProfileEditorOpen(true)}
                  >
                    <IconEdit className="size-[14px]" />
                    编辑资料
                  </UiButton>
                )}
              </div>
            </div>

            <div className="flex min-w-[280px] flex-1 flex-col gap-2">
              <div className="flex items-end gap-2">
                <h2 className="m-0 text-[22px] leading-none font-semibold text-[#18181a]">
                  {employeeDisplayName(selectedAgent)}
                </h2>
                <span className="text-[13px] leading-none text-[#757f9c]">{employee.roleName || employeeDisplayName(selectedAgent)}</span>
              </div>

              <div className="flex flex-wrap items-center gap-4">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-[#f6f6f6] px-2.5 py-0.5">
                  <span
                    className="size-1.5 rounded-full ring-[1.5px] ring-white"
                    style={{ background: selectedAgent.status === 'active' ? '#22c55e' : '#c4c9d4' }}
                  />
                  <span className="text-[12px] text-[#757f9c]">
                    {selectedAgent.status === 'active' ? '在线' : '下线'}
                  </span>
                </span>
                <span className="text-[12px] text-[#757f9c]">入职时间：{employee.onboardedAt}</span>
                <div className="flex flex-wrap items-center gap-3">
                  {employee.workStyles.slice(0, 3).map((item) => (
                    <Badge
                      key={item}
                      variant="outline"
                      className="h-auto rounded-[10px] border-[0.5px] border-[#e3e7f1] px-4 py-1 text-[12px] font-normal text-[#757f9c]"
                    >
                      {item}
                    </Badge>
                  ))}
                </div>
              </div>

              <p className="m-0 line-clamp-2 max-w-[720px] text-[14px] leading-[22px] text-[#757f9c]">
                {systemSummary}
              </p>

              <div className="flex w-full max-w-[514px] gap-3">
                <HeroMetric value={activeKnowledge.length} label="资料" />
                <HeroMetric value={activeGeneralSkills.length} label="技能" />
                <HeroMetric value={activeSkills.length} label="SOP" />
                <HeroMetric value={activeScheduledTasks.length} label="定时任务" />
              </div>
            </div>
          </div>
        )}
      />
      <EmployeeProfileTabs activeKey="work" />

      <section className="relative flex w-full min-w-0 max-w-full mt-[-2px] flex-col gap-[24px] overflow-hidden rounded-[18px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] bg-white p-[14px] *:min-w-0 min-[521px]:p-[18px] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:bg-[#202126] in-data-[theme=dark]:text-[#f0f2f6]">
        <div className="flex w-full items-stretch">
          <ClickableMetric label="今日对话" value={todayRounds} onClick={goToLogs} />
          <ClickableMetric label="累计对话" value={replyStats.total} onClick={goToLogs} />
          <ClickableMetric label="好评率" value={positiveRate} suffix="%" onClick={goToLogs} />
          <ClickableMetric label="差评率" value={negativeRate} suffix="%" onClick={goToLogs} />
        </div>
        <ConversationHeatmap byDay={replyStats.byDay} />
        <div className="flex w-full min-w-0 max-w-full flex-col gap-[10px] my-[20px]">
          <div className="inline-flex items-center gap-[6px] self-start text-[14px] capitalize leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
            <IconGrowthArrow className="size-[14px] shrink-0" />
            成长记录
          </div>
          {growthItems.length ? (
            <div className="relative w-full min-w-0 max-w-full overflow-x-auto">
              <div className="grid grid-flow-col auto-cols-[minmax(160px,1fr)] gap-[20px]">
                {growthItems.map((item) => (
                  <div className="relative flex flex-col items-center gap-[8px]" key={item.id}>
                    <span className="pointer-events-none absolute left-[-10px] right-[-10px] top-[28px] z-0 h-px bg-[#e3e7f1] in-data-[theme=dark]:bg-[#363a45]" />
                    <p className="m-0 text-center text-[12px] font-medium leading-[16px] text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">
                      {formatMonthDay(item.timestamp)}
                    </p>
                    <span className="relative z-10 size-[8px] shrink-0 rounded-full bg-[#18181a] in-data-[theme=dark]:bg-[#f0f2f6]" />
                    <div className="relative flex w-[136px] flex-col gap-[4px] rounded-[14px] bg-[#f6f6f6] px-[16px] py-[10px] in-data-[theme=dark]:bg-[#2b2d33]">
                      <span className="absolute top-[-8px] left-1/2 size-0 -translate-x-1/2 border-x-6 border-b-8 border-x-transparent border-b-[#f6f6f6] in-data-[theme=dark]:border-b-[#2b2d33]" />
                      <span className="truncate text-[10px] leading-none text-[#757f9c]">{item.kind}</span>
                      <span className="truncate text-[12px] leading-none text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                        {staffdeckDisplayText(item.title)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="employee-memory-empty">暂无成长轨迹</div>
          )}
        </div>

        <div className="w-full min-w-0 max-w-full overflow-x-auto">
          <div className="grid grid-flow-col auto-cols-[minmax(160px,1fr)] gap-[clamp(18px,2.22vw,32px)]">
          {capabilityCards.map((item) => (
            <button
              type="button"
              key={item.title}
              className={`employee-cap-card${item.dark ? ' is-dark' : ''}`}
              onClick={() => navigate(item.route)}
            >
              <IconCardArrow className="employee-cap-arrow" />
              <span className="employee-cap-group">
                <span className="employee-cap-head">
                  {item.icon}
                  <span className="employee-cap-name">{item.title}</span>
                </span>
                <span className="employee-cap-metric">
                  <strong>{item.count}</strong>
                  <span className="employee-cap-bar"><i /></span>
                </span>
              </span>
              <span className="employee-cap-desc">{item.body}</span>
              {item.illustration && <img className="employee-cap-art" src={item.illustration} alt="" />}
            </button>
          ))}
          </div>
        </div>
      </section>
      <EmployeeAvatarEditor
        agent={selectedAgent}
        open={avatarEditorOpen}
        onClose={() => setAvatarEditorOpen(false)}
        onSaved={(saved) => setAgents((current) => current.map((item) => (item.id === saved.id ? saved : item)))}
      />
      <EmployeeProfileEditor
        agent={selectedAgent}
        open={profileEditorOpen}
        currentUser={currentUser}
        onClose={() => setProfileEditorOpen(false)}
        onSaved={(saved) => setAgents((current) => current.map((item) => (item.id === saved.id ? saved : item)))}
      />
    </div>
  );
}

function DashboardStat({ title, value, icon }: { title: string; value: number; icon: ReactNode }) {
  return (
    <div className="org-dashboard-card">
      <div className="ui-card-body p-[24px]">
        <span className="org-dashboard-icon">{icon}</span>
        <span className="text-[13px] text-muted-foreground">{title}</span>
        <strong>{value}</strong>
      </div>
    </div>
  );
}

function MetricTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="employee-metric-tile">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

type ProfileTabKey = 'work' | 'scheduled' | 'memories' | 'logs';

const PROFILE_TABS: {
  key: ProfileTabKey;
  label: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
  route: EnterpriseRoute;
}[] = [
  { key: 'work', label: '工作记录', Icon: IconProfileFile, route: EnterpriseRoute.Dashboard },
  { key: 'scheduled', label: '定时任务', Icon: IconProfileAlarm, route: EnterpriseRoute.ScheduledTasks },
  { key: 'memories', label: '记忆', Icon: IconProfileHistory, route: EnterpriseRoute.Memories },
  { key: 'logs', label: '对话日志', Icon: IconProfileCalendar, route: EnterpriseRoute.Feedback },
];

function EmployeeProfileTabs({ activeKey = 'work' }: { activeKey?: ProfileTabKey }) {
  const navigate = useNavigate();
  return (
    <Tabs
      value={activeKey}
      onValueChange={(value) => {
        const tab = PROFILE_TABS.find((item) => item.key === value);
        if (tab && value !== activeKey) navigate(tab.route);
      }}
      className="flex w-full flex-col items-center"
    >
      <TabsList
        aria-label="个人档案分区"
        className="h-[35px]! w-[504px] max-w-full gap-2 rounded-none bg-transparent p-0"
      >
        {PROFILE_TABS.map(({ key, label, Icon }) => (
          <TabsTrigger
            key={key}
            value={key}
            className="h-[35px] flex-1 gap-[7px] rounded-t-lg rounded-b-none border-0 text-[14px] font-bold text-[#8b94aa] hover:text-[#202226] data-[state=active]:bg-white data-[state=active]:text-[#202226] data-[state=active]:shadow-[0_-12px_28px_rgba(21,26,38,0.04)] in-data-[theme=dark]:text-[#8f98aa] in-data-[theme=dark]:hover:text-[#f0f2f6] in-data-[theme=dark]:data-[state=active]:bg-[#202126] in-data-[theme=dark]:data-[state=active]:text-[#c5ccd8] in-data-[theme=dark]:data-[state=active]:shadow-none"
          >
            <Icon />
            {label}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}

function HeroMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-1 items-end gap-1 rounded-[10px] bg-[#f6f6f6] px-5 py-2">
      <strong className="text-[14px] leading-none font-medium text-[#18181a]">{value}</strong>
      <span className="text-[12px] leading-none text-[#464c5e]">{label}</span>
    </div>
  );
}

function ClickableMetric({ label, value, suffix = '', onClick }: { label: string; value: number; suffix?: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-w-px flex-[1_0_0] cursor-pointer flex-col justify-center gap-1 border-[0.5px] border-[#e3e7f1] bg-transparent px-5 py-2.5 text-left transition-colors first:rounded-l-[14px] last:rounded-r-[14px] hover:bg-[#f7f8fa] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:hover:bg-white/5"
    >
      <strong className="text-[18px] font-medium leading-none text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">{value}{suffix}</strong>
      <span className="text-[12px] leading-none text-[#464c5e] in-data-[theme=dark]:text-[#aeb6c6]">{label}</span>
    </button>
  );
}

function ConversationHeatmap({ byDay }: { byDay: Record<string, number> }) {
  const days = useMemo(() => heatmapDays(byDay), [byDay]);
  const rows = useMemo(
    () =>
      Array.from({ length: HEATMAP_ROWS }, (_, row) =>
        Array.from({ length: HEATMAP_COLUMNS }, (_, column) => days[column * HEATMAP_ROWS + row]),
      ),
    [days],
  );
  return (
    <div className="w-full overflow-x-auto overflow-y-hidden">
      <div className="mx-auto flex w-max flex-col gap-[6px]">
        <div className="ml-[52px] grid w-[714px] grid-cols-[repeat(33,10px)] gap-x-[12px] text-[10px] capitalize leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
          {monthLabels().map((item) => (
            <span
              key={`${item.label}-${item.offset}`}
              className="whitespace-nowrap"
              style={{ gridColumn: `${item.offset + 1} / span ${item.span}` }}
            >
              {item.label}
            </span>
          ))}
        </div>
        {rows.map((cells, row) => (
          <div className="flex items-center gap-[32px]" key={`row-${row}`}>
            <span className="w-[20px] shrink-0 text-[10px] capitalize leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
              {HEATMAP_WEEKDAY_LABELS[row]}
            </span>
            <div className="flex gap-[12px]">
              {cells.map((day) => (
                <span
                  key={day.key}
                  className={`group relative size-[10px] shrink-0 rounded-[2.5px] border-[0.625px] border-solid border-[#e3e7f1] in-data-[theme=dark]:border-[#363a45] ${HEATMAP_CELL_LEVELS[Math.min(4, day.count)]}`}
                >
                  {day.count > 0 && (
                    <span className={`pointer-events-none absolute left-1/2 z-20 hidden -translate-x-1/2 whitespace-nowrap rounded-[6px] bg-[#303645] px-[8px] py-[5px] text-[11px] font-medium leading-none text-white shadow-[0_6px_16px_rgba(21,26,38,0.18)] group-hover:block in-data-[theme=dark]:bg-[#f0f2f6] in-data-[theme=dark]:text-[#202126] ${row < 2 ? 'top-full mt-[7px]' : 'bottom-full mb-[7px]'}`}>
                      {day.label} · {day.count} 轮对话
                      <span className={`absolute left-1/2 size-0 -translate-x-1/2 border-x-4 border-x-transparent ${row < 2 ? 'bottom-full border-b-4 border-b-[#303645] in-data-[theme=dark]:border-b-[#f0f2f6]' : 'top-full border-t-4 border-t-[#303645] in-data-[theme=dark]:border-t-[#f0f2f6]'}`} />
                    </span>
                  )}
                </span>
              ))}
            </div>
          </div>
        ))}
        <div className="mt-[4px] flex items-center justify-center gap-[6px] text-[12px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
          <span>少</span>
          {[1, 2, 3, 4].map((level) => (
            <span
              key={`legend-${level}`}
              className={`size-[12px] shrink-0 rounded-[3px] border-[0.625px] border-solid border-[#e3e7f1] in-data-[theme=dark]:border-[#363a45] ${HEATMAP_CELL_LEVELS[level]}`}
            />
          ))}
          <span>多</span>
        </div>
      </div>
    </div>
  );
}

// Ascending rolling months ending at the current month, e.g. [去年7月 … 今年7月].
function heatmapMonthSequence() {
  const now = new Date();
  return Array.from({ length: HEATMAP_MONTH_SLOTS }, (_, index) => {
    const offsetFromNow = HEATMAP_MONTH_SLOTS - 1 - index;
    const date = new Date(now.getFullYear(), now.getMonth() - offsetFromNow, 1);
    return { year: date.getFullYear(), month: date.getMonth() };
  });
}

// Canonical partition of the columns into month slots, shared by the data grid
// and the month labels so they always line up.
function heatmapMonthColumnStart(slot: number) {
  return Math.floor((slot * HEATMAP_COLUMNS) / HEATMAP_MONTH_SLOTS);
}

function heatmapSlotForColumn(column: number) {
  for (let slot = HEATMAP_MONTH_SLOTS - 1; slot >= 0; slot -= 1) {
    if (column >= heatmapMonthColumnStart(slot)) return slot;
  }
  return 0;
}

function heatmapDays(byDay: Record<string, number>) {
  const months = heatmapMonthSequence();
  return Array.from({ length: HEATMAP_BUCKETS }, (_, index) => {
    const column = Math.floor(index / HEATMAP_ROWS);
    const row = index % HEATMAP_ROWS;
    const monthSlot = heatmapSlotForColumn(column);
    const { year, month } = months[monthSlot];
    const monthStartColumn = heatmapMonthColumnStart(monthSlot);
    const monthEndColumn = heatmapMonthColumnStart(monthSlot + 1);
    const columnsInMonth = Math.max(1, monthEndColumn - monthStartColumn);
    const cellsInMonth = columnsInMonth * HEATMAP_ROWS;
    const cellInMonth = (column - monthStartColumn) * HEATMAP_ROWS + row;
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const startDay = Math.min(daysInMonth, Math.floor((cellInMonth * daysInMonth) / cellsInMonth) + 1);
    const endDay = Math.max(startDay, Math.min(daysInMonth, Math.floor(((cellInMonth + 1) * daysInMonth) / cellsInMonth)));
    const bucketStart = new Date(year, month, startDay);
    const bucketEnd = new Date(year, month, endDay);
    let count = 0;
    for (let dayOfMonth = startDay; dayOfMonth <= endDay; dayOfMonth += 1) {
      count += byDay[dateKey(new Date(year, month, dayOfMonth))] || 0;
    }
    const startKey = dateKey(bucketStart);
    const endKey = dateKey(bucketEnd);
    return {
      key: `${index}-${startKey}`,
      label: startKey === endKey ? startKey : `${startKey} 至 ${endKey}`,
      date: bucketStart,
      count,
    };
  });
}

function monthLabels() {
  const months = heatmapMonthSequence();
  return months.map((item, index) => {
    const offset = heatmapMonthColumnStart(index);
    const nextOffset = heatmapMonthColumnStart(index + 1);
    return {
      label: `${item.month + 1}月`,
      offset,
      span: Math.max(1, nextOffset - offset),
    };
  });
}

function dateKey(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function compactSummary(value: string, maxLength: number): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > maxLength ? `${compact.slice(0, maxLength)}...` : compact;
}

function growthTimeline(
  sops: SkillRead[],
  generalSkills: GeneralSkillRead[],
  tools: ToolRead[],
): GrowthEvent[] {
  const events: GrowthEvent[] = [];

  sops.forEach((item) => {
    const evolved = Boolean(item.branch_head_version && item.branch_head_version !== item.branch_base_version);
    events.push({
      id: `sop-${item.id}`,
      kind: evolved ? 'SOP 进化' : '新增 SOP',
      title: item.name,
      description: evolved
        ? `本地版本从 ${item.branch_base_version || item.version} 进化到 ${item.branch_head_version || item.version}`
        : `新增 ${item.version} 版业务流程`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="filter" />,
      tone: 'mint',
    });
  });

  generalSkills.forEach((item) => {
    const upgraded = isMeaningfullyUpdated(item.created_at, item.updated_at);
    events.push({
      id: `general-${item.id}`,
      kind: upgraded ? '技能升级' : '新增技能',
      title: item.name,
      description: upgraded ? '技能说明、权限或运行配置有更新' : `新增 ${item.slug} 通用能力`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="spark" />,
      tone: 'teal',
    });
  });

  tools.forEach((item) => {
    events.push({
      id: `tool-${item.id}`,
      kind: '新增工具',
      title: item.display_name || item.name,
      description: `${item.bucket || '工具'} · ${item.tool_type.toUpperCase()} 调用能力`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="tool" />,
      tone: 'green',
    });
  });

  return events
    .filter((item) => Boolean(item.timestamp))
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
}

function stableGrowthTimestamp(item: GrowthTimestampSource): string {
  const metadata = item.metadata || {};
  const candidates = [
    metadata.learned_at,
    metadata.assigned_at,
    metadata.installed_at,
    metadata.imported_at,
    metadata.created_at,
    item.created_at,
  ];
  return candidates.find((value): value is string => typeof value === 'string' && Boolean(value.trim())) || '';
}

function isMeaningfullyUpdated(createdAt?: string, updatedAt?: string): boolean {
  if (!createdAt || !updatedAt) return false;
  return Math.abs(new Date(updatedAt).getTime() - new Date(createdAt).getTime()) > 60 * 1000;
}

function formatMonthDay(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return `${date.getMonth() + 1}.${date.getDate()}`;
}

function formatTaskSchedule(task: ScheduledTaskRead): string {
  const schedule = task.schedule || {};
  if (task.schedule_type === 'weekly') {
    const weekdays = Array.isArray(schedule.weekdays)
      ? schedule.weekdays.map((item) => ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][Number(item)]).filter(Boolean).join('、')
      : '周一';
    return `每周 ${weekdays} ${schedule.time || '09:00'}`;
  }
  if (task.schedule_type === 'monthly') {
    return `每月 ${schedule.day_of_month || 1} 号 ${schedule.time || '09:00'}`;
  }
  if (task.schedule_type === 'once') {
    return '一次性';
  }
  return `每天 ${schedule.time || '09:00'}`;
}

function formatTaskTime(value: string): string {
  const date = parseBackendTime(value);
  if (Number.isNaN(date.getTime())) return '暂无';
  return date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}

function parseBackendTime(value: string): Date {
  const text = String(value || '').trim();
  if (!text) return new Date('');
  if (/[zZ]|[+-]\d{2}:\d{2}$/.test(text)) return new Date(text);
  return new Date(`${text}Z`);
}
