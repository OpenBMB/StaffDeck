import { Button, Card, Space, Tag, Typography, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from '../api/client';
import { isEmployeeOwnedBy, isGalleryEmployee, type EnterpriseAuthUser } from '../auth';
import EmployeeAvatar from '../components/EmployeeAvatar';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import StaffdeckIcon from '../components/StaffdeckIcon';
import capabilityLogs from '../assets/staffdeck/sd1-card-logs.png';
import capabilityTasks from '../assets/staffdeck/sd1-card-scheduled.png';
import capabilityTools from '../assets/staffdeck/sd1-card-tools.png';
import {
  employeeDisplayName,
  employeeProfile,
  isDefaultEmployeeAgent,
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
const HEATMAP_ROWS = 4;
const HEATMAP_COLUMNS = 33;
const HEATMAP_BUCKETS = HEATMAP_ROWS * HEATMAP_COLUMNS;
const SD1_HEATMAP_MONTHS = [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0];

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
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  forceOverall?: boolean;
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
        const visibleAgents = agentRows.filter((item) => (
          isAdmin || (!item.is_overall && (
            isDefaultEmployeeAgent(item)
            || isEmployeeOwnedBy(item, currentUser)
            || isGalleryEmployee(item)
          ))
        ));
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
          const ownedAgents = visibleAgents.filter((item) => !item.is_overall && isEmployeeOwnedBy(item, currentUser));
          const next = isAdmin
            ? visibleAgents.find((item) => item.is_overall)?.id || preferredEmployeeAgent(visibleAgents)?.id || ''
            : preferredEmployeeAgent(ownedAgents)?.id
              || preferredEmployeeAgent(visibleAgents)?.id
              || '';
          if (next) {
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, next);
            window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: next } }));
            setAgentId(next);
          }
        }
      })
      .catch((error) => message.error(error instanceof Error ? error.message : '加载数字员工档案失败'));
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

  if (!selectedAgent && !isAdmin) {
    return (
      <div className="page dashboard-page">
        <Card className="empty-workspace-card">
          <Typography.Title level={3}>还没有数字员工</Typography.Title>
          <Typography.Paragraph type="secondary">
            点击左下角「新建数字员工」开始创建，或前往员工广场选择已发布的员工。
          </Typography.Paragraph>
          <Space>
            <Button type="primary" onClick={() => navigate('/enterprise/agents')}>查看我的数字员工</Button>
            <Button onClick={() => navigate('/enterprise/feedback')}>查看对话日志</Button>
          </Space>
        </Card>
      </div>
    );
  }

  if (!selectedAgent || selectedAgent.is_overall) {
    return (
      <div className="page dashboard-page">
        <div className="page-title">
          <Typography.Title level={3}>开放广场</Typography.Title>
        </div>
        <section className="employee-hero org-hero">
          <div>
            <span className="section-kicker">开放广场</span>
            <Typography.Title level={2}>开放广场</Typography.Title>
            <Typography.Paragraph>
              汇集所有可共享的 SOP、知识库、技能和工具，新建数字员工时可以从这里复制配置作为起点。
            </Typography.Paragraph>
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
          <Card className="org-dashboard-card" title="默认模型">
            <Typography.Text>{defaultModel ? `${defaultModel.name} / ${defaultModel.model}` : '未配置'}</Typography.Text>
          </Card>
        </div>
      </div>
    );
  }

  const employee = employeeProfile(selectedAgent);
  const canEditSelectedAgent = !selectedAgent.is_overall && (isAdmin || isEmployeeOwnedBy(selectedAgent, currentUser));
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
      icon: <StaffdeckIcon name="file" />,
      dark: false,
    },
    {
      route: '/enterprise/general-skills',
      title: '技能',
      tone: 'skill',
      count: activeGeneralSkills.length,
      body: activeGeneralSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用技能',
      icon: <StaffdeckIcon name="spark" />,
      dark: false,
    },
    {
      route: '/enterprise/skills',
      title: 'SOP',
      tone: 'sop',
      count: activeSkills.length,
      body: activeSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用 SOP',
      icon: <StaffdeckIcon name="filter" />,
      dark: false,
    },
    {
      route: '/enterprise/tools',
      title: '工具',
      tone: 'tools',
      count: activeTools.length,
      body: activeTools.slice(0, 3).map((item) => staffdeckDisplayText(item.display_name || item.name)).join(' / ') || '暂无启用工具',
      icon: <StaffdeckIcon name="tool" />,
      dark: true,
      illustration: capabilityTools,
    },
    {
      route: '/enterprise/scheduled-tasks',
      title: '定时任务',
      tone: 'tasks',
      count: activeScheduledTasks.length,
      body: activeScheduledTasks.slice(0, 2).map((item) => staffdeckDisplayText(item.title)).join(' / ') || '暂无启用定时任务',
      icon: <StaffdeckIcon name="clock" />,
      dark: true,
      illustration: capabilityTasks,
    },
    {
      route: `/enterprise/feedback?agent_id=${encodeURIComponent(selectedAgent.id)}`,
      title: '对话日志',
      tone: 'logs',
      count: replyStats.total,
      body: staffdeckDisplayText(employeeSessions[0]?.summary || employeeSessions[0]?.last_agent_question || '暂无对话任务'),
      icon: <StaffdeckIcon name="chat" />,
      dark: true,
      illustration: capabilityLogs,
    },
  ];

  const growthItems = growthTimeline(activeSkills, activeGeneralSkills, activeTools);

  return (
    <div className="page dashboard-page employee-dashboard-page employee-home-page">
      <section className="employee-home-hero">
        <div className="employee-id-card">
          <EmployeeAvatar agent={selectedAgent} size={116} />
          <div className="employee-avatar-actions">
            <Button size="small" icon={<StaffdeckIcon name="chat" />} onClick={() => { window.location.href = '/chat/'; }}>
              去对话
            </Button>
            {canEditSelectedAgent && (
              <Button
                size="small"
                icon={<StaffdeckIcon name="edit" />}
                onClick={() => setProfileEditorOpen(true)}
              >
                编辑资料
              </Button>
            )}
          </div>
          {canEditSelectedAgent && (
            <Button
              size="small"
              className="employee-avatar-change"
              icon={<StaffdeckIcon name="user" />}
              onClick={() => setAvatarEditorOpen(true)}
            >
              更换头像
            </Button>
          )}
        </div>
        <div className="employee-home-main">
          <div className="employee-home-title-row">
            <Typography.Title level={2}>{employee.roleName || employeeDisplayName(selectedAgent)}</Typography.Title>
            <span>{employeeDisplayName(selectedAgent)}</span>
          </div>
          <Space wrap className="employee-home-meta">
            <span className={`employee-online-status ${selectedAgent.status === 'active' ? 'is-online' : 'is-offline'}`}>
              <span className="employee-online-dot" />
              <Typography.Text>{selectedAgent.status === 'active' ? '在线' : '下线'}</Typography.Text>
            </span>
            <Typography.Text type="secondary">入职时间：{employee.onboardedAt}</Typography.Text>
            {employee.workStyles.slice(0, 3).map((item) => <Tag key={item}>{item}</Tag>)}
          </Space>
          <Typography.Paragraph className="employee-system-summary">{systemSummary}</Typography.Paragraph>
        </div>
        <div className="employee-home-side">
          <MetricTile label="资料" value={activeKnowledge.length} />
          <MetricTile label="技能" value={activeGeneralSkills.length} />
          <MetricTile label="SOP" value={activeSkills.length} />
          <MetricTile label="定期任务" value={activeScheduledTasks.length} />
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

      <nav className="employee-profile-tabs" aria-label="个人档案分区">
        <button type="button" className="active"><StaffdeckIcon name="file" /> 工作记录</button>
        <button type="button" onClick={() => navigate('/enterprise/scheduled-tasks')}><StaffdeckIcon name="clock" /> 定时任务</button>
        <button type="button" onClick={() => navigate('/enterprise/memories')}><StaffdeckIcon name="history" /> 记忆</button>
        <button type="button" onClick={() => navigate('/enterprise/feedback')}><StaffdeckIcon name="calendar" /> 对话日志</button>
      </nav>

      <section className="employee-work-card">
        <div className="employee-work-metrics">
          <ClickableMetric label="今日对话" value={todayRounds} onClick={goToLogs} />
          <ClickableMetric label="累计对话" value={replyStats.total} onClick={goToLogs} />
          <ClickableMetric label="好评率" value={positiveRate} suffix="%" onClick={goToLogs} />
          <ClickableMetric label="差评率" value={negativeRate} suffix="%" onClick={goToLogs} />
        </div>
        <ConversationHeatmap byDay={replyStats.byDay} />

        <div className="employee-growth-title"><StaffdeckIcon name="arrow" /> 成长记录</div>
        {growthItems.length ? (
          <div className="employee-memory-timeline">
            {growthItems.map((item) => (
              <div className="employee-memory-event" key={item.id}>
                <div className="employee-memory-date">
                  <strong>{formatMonthDay(item.timestamp)}</strong>
                  <span>{relativeTime(item.timestamp)}</span>
                </div>
                <span className={`employee-memory-dot is-${item.tone}`}>{item.icon}</span>
                <div className="employee-memory-copy">
                  <small>{item.kind}</small>
                  <strong>{staffdeckDisplayText(item.title)}</strong>
                  <span>{staffdeckDisplayText(item.description)}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="employee-memory-empty">暂无成长轨迹</div>
        )}

        <div className="employee-capability-grid">
          {capabilityCards.map((item) => (
            <Card
              key={item.title}
              className={`employee-capability-card tone-${item.tone}${item.dark ? ' is-dark' : ''}${item.illustration ? ' has-illustration' : ''}`}
              hoverable
              onClick={() => navigate(item.route)}
            >
              <div className="employee-capability-head">
                <span>{item.icon}</span>
                <em>{item.count}</em>
              </div>
              <strong className="employee-capability-title">{item.title}</strong>
              <Typography.Paragraph ellipsis={{ rows: 2 }}>{item.body}</Typography.Paragraph>
              {item.illustration && <img className="employee-capability-illustration" src={item.illustration} alt="" />}
              <span className="employee-capability-action">查看详情 <StaffdeckIcon name="arrow" /></span>
            </Card>
          ))}
        </div>
      </section>
    </div>
  );
}

function DashboardStat({ title, value, icon }: { title: string; value: number; icon: ReactNode }) {
  return (
    <Card className="org-dashboard-card">
      <span className="org-dashboard-icon">{icon}</span>
      <Typography.Text type="secondary">{title}</Typography.Text>
      <strong>{value}</strong>
    </Card>
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

function ClickableMetric({ label, value, suffix = '', onClick }: { label: string; value: number; suffix?: string; onClick: () => void }) {
  return (
    <button type="button" className="employee-work-metric" onClick={onClick}>
      <strong>{value}{suffix}</strong>
      <span>{label}</span>
    </button>
  );
}

function ConversationHeatmap({ byDay }: { byDay: Record<string, number> }) {
  const days = useMemo(() => heatmapDays(byDay), [byDay]);
  return (
    <div className="employee-heatmap">
      <div className="employee-heatmap-months">
        {monthLabels().map((item) => (
          <span
            key={`${item.label}-${item.offset}`}
            style={{ gridColumn: `${item.offset + 1} / span ${item.span}` }}
          >
            {item.label}
          </span>
        ))}
      </div>
      <div className="employee-heatmap-body">
        <div className="employee-heatmap-weekdays">
          <span>周一</span>
          <span>周二</span>
          <span>周三</span>
          <span>周四</span>
        </div>
        <div className="employee-heatmap-grid">
          {days.map((day) => (
            <span
              key={day.key}
              className={`employee-heatmap-cell level-${Math.min(4, day.count)}`}
              title={`${day.label}: ${day.count} 轮对话`}
            />
          ))}
        </div>
      </div>
      <div className="employee-heatmap-legend">
        <span>少</span>
        {[0, 1, 2, 3, 4].map((level) => <i className={`level-${level}`} key={level} />)}
        <span>多</span>
      </div>
    </div>
  );
}

function heatmapDays(byDay: Record<string, number>) {
  const year = new Date().getFullYear();
  return Array.from({ length: HEATMAP_BUCKETS }, (_, index) => {
    const column = Math.floor(index / HEATMAP_ROWS);
    const row = index % HEATMAP_ROWS;
    const monthSlot = Math.min(SD1_HEATMAP_MONTHS.length - 1, Math.floor((column * SD1_HEATMAP_MONTHS.length) / HEATMAP_COLUMNS));
    const monthIndex = SD1_HEATMAP_MONTHS[monthSlot];
    const monthStartColumn = Math.floor((monthSlot * HEATMAP_COLUMNS) / SD1_HEATMAP_MONTHS.length);
    const monthEndColumn = Math.floor(((monthSlot + 1) * HEATMAP_COLUMNS) / SD1_HEATMAP_MONTHS.length);
    const columnsInMonth = Math.max(1, monthEndColumn - monthStartColumn);
    const cellsInMonth = columnsInMonth * HEATMAP_ROWS;
    const cellInMonth = (column - monthStartColumn) * HEATMAP_ROWS + row;
    const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
    const startDay = Math.min(daysInMonth, Math.floor((cellInMonth * daysInMonth) / cellsInMonth) + 1);
    const endDay = Math.max(startDay, Math.min(daysInMonth, Math.floor(((cellInMonth + 1) * daysInMonth) / cellsInMonth)));
    const bucketStart = new Date(year, monthIndex, startDay);
    const bucketEnd = new Date(year, monthIndex, endDay);
    let count = 0;
    for (let dayOfMonth = startDay; dayOfMonth <= endDay; dayOfMonth += 1) {
      const day = new Date(year, monthIndex, dayOfMonth);
      count += byDay[dateKey(day)] || 0;
    }
    const startKey = dateKey(bucketStart);
    const endKey = dateKey(bucketEnd);
    return {
      key: `${startKey}-${endKey}`,
      label: startKey === endKey ? startKey : `${startKey} 至 ${endKey}`,
      date: bucketStart,
      count,
    };
  });
}

function monthLabels() {
  return SD1_HEATMAP_MONTHS.map((monthIndex, index) => {
    const offset = Math.floor((index * HEATMAP_COLUMNS) / SD1_HEATMAP_MONTHS.length);
    const nextOffset = Math.floor(((index + 1) * HEATMAP_COLUMNS) / SD1_HEATMAP_MONTHS.length);
    return {
      label: `${monthIndex + 1}月`,
      offset,
      span: Math.max(2, nextOffset - offset),
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

  tools.slice(0, 3).forEach((item) => {
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
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime())
    .slice(-4);
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
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function relativeTime(value?: string): string {
  if (!value) return '暂无';
  const diff = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(diff) || diff < 0) return '刚刚';
  const minutes = Math.floor(diff / 60000);
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
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
