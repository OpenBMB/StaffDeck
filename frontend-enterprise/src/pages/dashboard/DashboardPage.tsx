import { useEffect, useState } from 'react';
import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button as UiButton, Tabs, TabsList, TabsTrigger, notify } from '@/components/ui';
import { EnterpriseRoute } from '../../enums/routes';
import IconChat from '../../assets/icons/chat.svg?react';
import IconEdit from '../../assets/icons/edit.svg?react';
import IconAccount from '../../assets/icons/sys-accounts.svg?react';
import IconProfileFile from '../../assets/icons/profile-file.svg?react';
import IconProfileAlarm from '../../assets/icons/profile-alarm.svg?react';
import IconProfileHistory from '../../assets/icons/profile-history.svg?react';
import IconProfileCalendar from '../../assets/icons/profile-calendar.svg?react';
import { api, TENANT_ID } from '../../api/client';
import { loadEmployeeDirectory } from '../../api/employee-directory';
import type { EnterpriseAuthUser } from '../../auth';
import AppHeader from '../../components/AppHeader';
import EmployeeAvatar from '../../components/EmployeeAvatar';
import EmployeeAvatarEditor from '../../components/EmployeeAvatarEditor';
import EmployeeProfileEditor from '../../components/EmployeeProfileEditor';
import StaffdeckIcon from '../../components/StaffdeckIcon';
import ScheduledTasksTab from './ScheduledTasksTab';
import MemoriesTab from './MemoriesTab';
import ConversationLogsTab from './ConversationLogsTab';
import WorkRecordTab from './WorkRecordTab';
import EvolutionPanel from './EvolutionPanel';
import { employeeDashboardMetrics } from './employeeDashboardMetrics';
import {
  agentResourceCount,
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeCreatorName,
  employeeDisplayName,
  employeeProfile,
  preferredEmployeeAgent,
  staffdeckDisplayText,
} from '../../employee';
import type {
  AgentProfileRead,
  AgentWorkRecordEventRead,
  AgentWorkRecordRead,
  EnterpriseChatSessionRead,
  FeedbackSummaryRead,
  GeneralSkillRead,
  KnowledgeBaseRead,
  ModelConfigRead,
  ScheduledTaskRead,
  SkillRead,
  ToolRead,
} from '../../types';
import { isTeamScope, readEmployeeScope } from '../../lib/agent-scope-storage';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const PANEL_LABELS: Record<string, string> = {
  skills: 'SOP', generalSkills: '技能', knowledge: '知识库', models: '模型',
  tools: '工具', sessions: '对话日志', feedback: '反馈', tasks: '定时任务', work: '工作记录',
};

export default function DashboardPage({
  currentUser,
  isAdmin = false,
  profileTab = 'work',
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  profileTab?: ProfileTabKey;
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
  const [activityEvents, setActivityEvents] = useState<AgentWorkRecordEventRead[]>([]);
  const [agentId, setAgentId] = useState(readEmployeeScope);
  const [avatarEditorOpen, setAvatarEditorOpen] = useState(false);
  const [profileEditorOpen, setProfileEditorOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [validatedScope, setValidatedScope] = useState<string | null>(null);
  const requestedScope = `${currentUser?.id || ''}:${agentId}`;
  const [panelStates, setPanelStates] = useState<Record<string, 'loading' | 'ready' | 'error'>>({});
  const [panelErrors, setPanelErrors] = useState<Record<string, string>>({});
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setAgentId(next && !isTeamScope(next) ? next : readEmployeeScope());
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let switchingAgent = false;
    setLoaded(false);
    setValidatedScope(null);
    setPanelStates(Object.fromEntries(Object.keys(PANEL_LABELS).map((key) => [key, 'loading'])));
    setPanelErrors({});
    setSkills([]); setGeneralSkills([]); setKnowledgeBases([]); setModels([]);
    setTools([]); setSessions([]); setFeedbackSummary(null); setScheduledTasks([]);
    const controller = new AbortController();
    // Persisted scope is only a preference: it may belong to a previous assembly.
    // Resolve it against the current roster before making any scoped request.
    loadEmployeeDirectory(controller.signal)
      .then(async (agentRows) => {
        if (cancelled) return;
        const visibleAgents = agentRows.filter((item) => canSelectCurrentEmployeeAgent(item, currentUser, {
          activeOnly: true,
        }));
        setAgents(visibleAgents);
        if (!agentId || !visibleAgents.some((item) => item.id === agentId)) {
          const manageableAgents = visibleAgents.filter((item) => canManageEmployeeAgent(item, currentUser));
          const next = isAdmin
            ? preferredEmployeeAgent(visibleAgents)?.id || ''
            : preferredEmployeeAgent(manageableAgents)?.id
              || preferredEmployeeAgent(visibleAgents)?.id
              || '';
          if (next) {
            switchingAgent = true;
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, next);
            window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: next } }));
            setAgentId(next);
            return;
          }
          window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
          setAgentId('');
          setSkills([]);
          setGeneralSkills([]);
          setKnowledgeBases([]);
          setModels([]);
          setTools([]);
          setSessions([]);
          setFeedbackSummary(null);
          setScheduledTasks([]);
          return;
        }
        const scope = `tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(agentId)}`;
        setValidatedScope(`${currentUser?.id || ''}:${agentId}`);
        setLoaded(true);
        async function loadPanel<T>(key: string, path: string, apply: (value: T) => void) {
          try {
            const value = await api.get<T>(path, { signal: controller.signal });
            if (cancelled) return;
            apply(value);
            setPanelStates((state) => ({ ...state, [key]: 'ready' }));
          } catch (error) {
            if (cancelled) return;
            const message = error instanceof Error ? error.message : '加载失败，请重试';
            setPanelStates((state) => ({ ...state, [key]: 'error' }));
            setPanelErrors((state) => ({ ...state, [key]: message }));
            notify.error(message);
          }
        }
        await Promise.all([
          loadPanel<SkillRead[]>('skills', `/api/enterprise/skills?${scope}`, setSkills),
          loadPanel<GeneralSkillRead[]>('generalSkills', `/api/enterprise/general-skills?${scope}`, setGeneralSkills),
          loadPanel<KnowledgeBaseRead[]>('knowledge', `/api/enterprise/knowledge-bases?${scope}`, setKnowledgeBases),
          loadPanel<ModelConfigRead[]>('models', `/api/enterprise/model-configs?tenant_id=${TENANT_ID}`, setModels),
          loadPanel<ToolRead[]>('tools', `/api/enterprise/tools?${scope}`, setTools),
          loadPanel<EnterpriseChatSessionRead[]>('sessions', `/api/enterprise/sessions?${scope}`, setSessions),
          loadPanel<FeedbackSummaryRead>('feedback', `/api/enterprise/feedback/summary?${scope}`, setFeedbackSummary),
          loadPanel<ScheduledTaskRead[]>('tasks', `/api/enterprise/scheduled-tasks?${scope}`, (rows) => setScheduledTasks(rows.filter((item) => item.status !== 'archived'))),
        ]);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载数字员工档案失败');
      })
      .finally(() => {
        if (!cancelled && !switchingAgent) setLoaded(true);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [agentId, currentUser, isAdmin, reload]);

  const selectedAgent = agents.find((item) => item.id === agentId)
    || agents.find((item) => !item.is_overall)
    || null;
  const employeeSessions = selectedAgent?.is_overall
    ? sessions
    : sessions.filter((item) => item.agent_id === selectedAgent?.id);

  useEffect(() => {
    let cancelled = false;
    async function loadWorkRecord() {
      if (validatedScope !== requestedScope || !selectedAgent || selectedAgent.id !== agentId || selectedAgent.is_overall) {
        setActivityEvents([]);
        return;
      }
      try {
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
        const workRecord = await api.get<AgentWorkRecordRead>(
          `/api/enterprise/agents/${encodeURIComponent(selectedAgent.id)}/work-record?tenant_id=${TENANT_ID}&timezone=${encodeURIComponent(timezone)}`,
        );
        if (cancelled) return;
        setActivityEvents(workRecord.events);
        setPanelStates((state) => ({ ...state, work: 'ready' }));
      } catch (error) {
        if (cancelled) return;
        setActivityEvents([]);
        setPanelStates((state) => ({ ...state, work: 'error' }));
        setPanelErrors((state) => ({ ...state, work: error instanceof Error ? error.message : '加载员工工作记录失败' }));
        notify.error(error instanceof Error ? error.message : '加载员工工作记录失败');
      }
    }
    void loadWorkRecord();
    return () => {
      cancelled = true;
    };
  }, [selectedAgent?.id, selectedAgent?.is_overall, validatedScope, requestedScope, agentId, reload]);
  const defaultModel = models.find((item) => item.is_default);
  const totalCalls = skills.reduce((sum, item) => sum + (item.total_call_count || item.call_count || 0), 0);
  const positiveFeedback = skills.reduce((sum, item) => sum + (item.total_positive_feedback_count || 0), 0);
  const negativeFeedback = skills.reduce((sum, item) => sum + (item.total_negative_feedback_count || 0), 0);
  const visibleKnowledgeBases = knowledgeBases.filter((item) => !isEmptyDefaultKnowledgeBase(item));

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
          <DashboardStat title="知识库" value={visibleKnowledgeBases.length} icon={<StaffdeckIcon name="file" />} />
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
  const employeeCreator = employeeCreatorName(selectedAgent);
  const canEditSelectedAgent = canManageEmployeeAgent(selectedAgent, currentUser);
  const activeSkills = skills.filter((item) => item.status === 'published' && item.branch_status !== 'inactive');
  const activeGeneralSkills = generalSkills.filter((item) => item.status === 'published');
  const activeKnowledge = visibleKnowledgeBases.filter((item) => item.status === 'active');
  const activeTools = tools.filter((item) => item.enabled);
  const selectedKnowledgeCount = visibleKnowledgeBases.length;
  const selectedGeneralSkillCount = agentResourceCount(selectedAgent, 'general_skill');
  const selectedSkillCount = agentResourceCount(selectedAgent, 'skill');
  const employeeScheduledTasks = scheduledTasks.filter((item) => item.agent_id === selectedAgent.id && item.status !== 'archived');
  const activeScheduledTasks = employeeScheduledTasks.filter((item) => item.status === 'active');
  const dashboardMetrics = employeeDashboardMetrics(employeeSessions, feedbackSummary);
  const systemPromptSummary = typeof selectedAgent.metadata?.system_prompt_summary === 'string'
    ? selectedAgent.metadata.system_prompt_summary
    : '';
  const systemSummary = compactSummary(
    staffdeckDisplayText(selectedAgent.persona_prompt || systemPromptSummary || selectedAgent.description || `${employee.roleName}，负责接收任务、调用知识库、执行 SOP 并沉淀对话质量反馈。`),
    132,
  );

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
                  onClick={() => { window.location.href = `${import.meta.env.BASE_URL}workspace/chat`; }}
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
                <span className="text-[12px] text-[#757f9c]">创建者：{employeeCreator}</span>
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
                <HeroMetric value={panelStates.knowledge === 'ready' ? selectedKnowledgeCount : '—'} label="资料" />
                <HeroMetric value={selectedGeneralSkillCount} label="技能" />
                <HeroMetric value={selectedSkillCount} label="SOP" />
                <HeroMetric value={panelStates.tasks === 'ready' ? activeScheduledTasks.length : '—'} label="定时任务" />
              </div>
            </div>
          </div>
        )}
      />
      <EmployeeProfileTabs activeKey={profileTab} />
      {Object.keys(panelErrors).length > 0 && (
        <div role="alert" className="my-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <p>部分数据加载失败，其他模块仍可使用</p>
          {Object.entries(panelErrors).map(([key, message]) => <p key={key}>{PANEL_LABELS[key]}：{message}</p>)}
          <UiButton variant="outline" onClick={() => setReload((value) => value + 1)}>重新加载</UiButton>
        </div>
      )}
      {profileTab === 'work' && (
        <>
          <WorkRecordTab
            panelStates={panelStates}
            selectedAgent={selectedAgent}
            activeKnowledge={activeKnowledge}
            activeGeneralSkills={activeGeneralSkills}
            activeSkills={activeSkills}
            activeTools={activeTools}
            activeScheduledTasks={activeScheduledTasks}
            employeeSessions={employeeSessions}
            conversationCount={dashboardMetrics.conversationCount}
            activityEvents={activityEvents}
            feedbackCount={dashboardMetrics.feedbackCount}
            positiveRate={dashboardMetrics.positiveRate}
            negativeRate={dashboardMetrics.negativeRate}
          />
          {canEditSelectedAgent && <EvolutionPanel agentId={selectedAgent.id} />}
        </>
      )}
      {profileTab === 'scheduled' && <ScheduledTasksTab />}
      {profileTab === 'memories' && <MemoriesTab currentUser={currentUser} agent={selectedAgent} />}
      {profileTab === 'logs' && <ConversationLogsTab />}
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

function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead): boolean {
  const hasRuntimeKnowledge = item.document_count > 0 || item.bucket_count > 0 || item.chunk_count > 0;
  if (!hasRuntimeKnowledge && item.metadata?.created_from_document_upload && !item.metadata?.source_document_id) {
    return true;
  }
  return (
    item.name === '默认知识库'
    && item.document_count === 0
    && item.bucket_count === 0
    && item.chunk_count === 0
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

function HeroMetric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex flex-1 items-end gap-1 rounded-[10px] bg-[#f6f6f6] px-5 py-2">
      <strong className="text-[14px] leading-none font-medium text-[#18181a]">{value}</strong>
      <span className="text-[12px] leading-none text-[#464c5e]">{label}</span>
    </div>
  );
}

function compactSummary(value: string, maxLength: number): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > maxLength ? `${compact.slice(0, maxLength)}...` : compact;
}
