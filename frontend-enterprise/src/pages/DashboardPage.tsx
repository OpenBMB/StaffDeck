import {
  ApiOutlined,
  BookOutlined,
  CalendarOutlined,
  CheckCircleOutlined,
  CommentOutlined,
  DashboardOutlined,
  FileTextOutlined,
  MessageOutlined,
  ProfileOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import { Avatar, Button, Card, Space, Tag, Typography, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { api, TENANT_ID } from '../api/client';
import { activeResourceCount, employeeDisplayName, employeeProfile, resourceCount } from '../employee';
import type {
  AgentProfileRead,
  EnterpriseChatSessionRead,
  EnterpriseSessionDetailRead,
  FeedbackSummaryRead,
  GeneralSkillRead,
  KnowledgeBaseRead,
  ModelConfigRead,
  SkillRead,
  ToolRead,
} from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
type ReplyStats = {
  total: number;
  today: number;
  byDay: Record<string, number>;
};

export default function DashboardPage() {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [sessions, setSessions] = useState<EnterpriseChatSessionRead[]>([]);
  const [feedbackSummary, setFeedbackSummary] = useState<FeedbackSummaryRead | null>(null);
  const [replyStats, setReplyStats] = useState<ReplyStats>({ total: 0, today: 0, byDay: {} });
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');

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
      api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}`),
      api.get<EnterpriseChatSessionRead[]>(`/api/enterprise/sessions?tenant_id=${TENANT_ID}`),
      api.get<FeedbackSummaryRead>(`/api/enterprise/feedback/summary?tenant_id=${TENANT_ID}`),
    ])
      .then(([agentRows, skillRows, generalSkillRows, kbRows, modelRows, toolRows, sessionRows, feedbackRows]) => {
        setAgents(agentRows);
        setSkills(skillRows);
        setGeneralSkills(generalSkillRows);
        setKnowledgeBases(kbRows);
        setModels(modelRows);
        setTools(toolRows);
        setSessions(sessionRows);
        setFeedbackSummary(feedbackRows);
        if (!agentId) {
          const next = agentRows.find((item) => item.is_overall)?.id || agentRows[0]?.id || '';
          if (next) setAgentId(next);
        }
      })
      .catch((error) => message.error(error instanceof Error ? error.message : '加载看板失败'));
  }, [agentId]);

  const selectedAgent = agents.find((item) => item.id === agentId) || agents.find((item) => item.is_overall) || null;
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

  if (!selectedAgent || selectedAgent.is_overall) {
    return (
      <div className="page dashboard-page">
        <div className="page-title">
          <Typography.Title level={3}>看板</Typography.Title>
        </div>
        <section className="employee-hero org-hero">
          <div>
            <span className="section-kicker">组织资源库</span>
            <Typography.Title level={2}>UltraRAG4 数字员工运营台</Typography.Title>
            <Typography.Paragraph>
              统一管理员工、业务资料、SOP、已掌握技能、工具和对话质检，让企业服务能力持续沉淀。
            </Typography.Paragraph>
          </div>
          <div className="employee-hero-metrics">
            <MetricTile label="员工" value={agents.filter((item) => !item.is_overall).length} />
            <MetricTile label="对话" value={sessions.length} />
            <MetricTile label="反馈" value={feedbackSummary?.total_feedback || 0} />
          </div>
        </section>
        <div className="org-dashboard-grid">
          <DashboardStat title="SOP" value={skills.length} icon={<ProfileOutlined />} />
          <DashboardStat title="已掌握技能" value={generalSkills.length} icon={<ApiOutlined />} />
          <DashboardStat title="业务资料" value={knowledgeBases.length} icon={<BookOutlined />} />
          <DashboardStat title="可用工具" value={tools.filter((item) => item.enabled).length} icon={<ToolOutlined />} />
          <DashboardStat title="SOP 调用" value={totalCalls} icon={<MessageOutlined />} />
          <DashboardStat title="好评" value={positiveFeedback || feedbackSummary?.up_count || 0} icon={<DashboardOutlined />} />
          <DashboardStat title="差评" value={negativeFeedback || feedbackSummary?.down_count || 0} icon={<DashboardOutlined />} />
          <Card className="org-dashboard-card" title="默认模型">
            <Typography.Text>{defaultModel ? `${defaultModel.name} / ${defaultModel.model}` : '未配置'}</Typography.Text>
          </Card>
        </div>
      </div>
    );
  }

  const employee = employeeProfile(selectedAgent);
  const activeSkills = skills.filter((item) => item.status === 'published' && item.branch_status !== 'inactive');
  const activeGeneralSkills = generalSkills.filter((item) => item.status === 'published');
  const activeKnowledge = knowledgeBases.filter((item) => item.status === 'active');
  const activeTools = tools.filter((item) => item.enabled);
  const totalFeedback = positiveFeedback + negativeFeedback;
  const positiveRate = totalFeedback ? Math.round((positiveFeedback / totalFeedback) * 100) : 0;
  const negativeRate = totalFeedback ? Math.round((negativeFeedback / totalFeedback) * 100) : 0;
  const todayRounds = replyStats.today;
  const systemPromptSummary = typeof selectedAgent.metadata?.system_prompt_summary === 'string'
    ? selectedAgent.metadata.system_prompt_summary
    : '';
  const systemSummary = compactSummary(
    selectedAgent.persona_prompt || systemPromptSummary || selectedAgent.description || `${employee.roleName}，负责接收任务、调用业务资料、执行 SOP 并沉淀对话质量反馈。`,
    132,
  );

  const scrollToSection = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  const capabilityCards = [
    {
      id: 'skills',
      title: '已掌握技能',
      count: activeGeneralSkills.length,
      body: activeGeneralSkills.slice(0, 3).map((item) => item.name).join(' / ') || '暂无启用技能',
      icon: <ApiOutlined />,
    },
    {
      id: 'sop',
      title: 'SOP管理',
      count: activeSkills.length,
      body: activeSkills.slice(0, 3).map((item) => item.name).join(' / ') || '暂无启用 SOP',
      icon: <ProfileOutlined />,
    },
    {
      id: 'knowledge',
      title: '业务资料',
      count: activeKnowledge.length,
      body: activeKnowledge.slice(0, 3).map((item) => item.name).join(' / ') || '暂无业务资料',
      icon: <FileTextOutlined />,
    },
    {
      id: 'tools',
      title: '工具箱',
      count: activeTools.length,
      body: activeTools.slice(0, 3).map((item) => item.display_name || item.name).join(' / ') || '暂无启用工具',
      icon: <ToolOutlined />,
    },
    {
      id: 'logs',
      title: '对话日志',
      count: replyStats.total,
      body: employeeSessions[0]?.summary || employeeSessions[0]?.last_agent_question || '暂无对话任务',
      icon: <CommentOutlined />,
    },
  ];

  const growthItems = growthTimeline(activeSkills, activeGeneralSkills, activeKnowledge, employeeSessions);

  return (
    <div className="page dashboard-page employee-dashboard-page employee-home-page">
      <section className="employee-home-hero">
        <div className="employee-id-card">
          <Avatar className={`employee-avatar tone-${employee.avatarTone}`} size={116}>{employee.avatarText}</Avatar>
          <span>ID: {selectedAgent.id.slice(-8)}</span>
        </div>
        <div className="employee-home-main">
          <div className="employee-home-title-row">
            <Typography.Title level={2}>{employeeDisplayName(selectedAgent)}</Typography.Title>
            <Tag>{employee.roleName}</Tag>
          </div>
          <Space wrap className="employee-home-meta">
            <span className="employee-online-dot" />
            <Typography.Text>{selectedAgent.status === 'active' ? '在线' : '下线'}</Typography.Text>
            <Typography.Text type="secondary">入职时间：{employee.onboardedAt}</Typography.Text>
          </Space>
          <Typography.Paragraph className="employee-system-summary">{systemSummary}</Typography.Paragraph>
          <Button type="text" onClick={() => scrollToSection('capabilities')}>编辑入职资料</Button>
        </div>
        <div className="employee-home-side">
          <MetricTile label="SOP" value={resourceCount(selectedAgent.resources, 'skill')} />
          <MetricTile label="技能" value={resourceCount(selectedAgent.resources, 'general_skill')} />
          <MetricTile label="资料" value={resourceCount(selectedAgent.resources, 'knowledge_base')} />
          <MetricTile label="资源" value={activeResourceCount(selectedAgent.resources)} />
        </div>
      </section>

      <section className="employee-work-card">
        <div className="employee-section-head">
          <div>
            <Typography.Title level={4}>工作记录</Typography.Title>
            <Typography.Text type="secondary">每天完成多少轮对话，以及近期质量表现。</Typography.Text>
          </div>
          <Space wrap>
            <Button type="text" icon={<CalendarOutlined />}>时间线视图</Button>
            <Button type="text" icon={<CommentOutlined />} onClick={() => scrollToSection('logs')}>对话任务</Button>
          </Space>
        </div>
        <div className="employee-work-metrics">
          <ClickableMetric label="今日对话" value={todayRounds} suffix="轮" onClick={() => scrollToSection('logs')} />
          <ClickableMetric label="累计对话" value={replyStats.total} onClick={() => scrollToSection('logs')} />
          <ClickableMetric label="收获好评率" value={positiveRate} suffix="%" onClick={() => scrollToSection('logs')} />
          <ClickableMetric label="差评率" value={negativeRate} suffix="%" onClick={() => scrollToSection('logs')} />
        </div>
        <ConversationHeatmap byDay={replyStats.byDay} />
      </section>

      <section className="employee-growth-card" id="growth">
        <div className="employee-section-head">
          <div>
            <Typography.Title level={4}>成长轨迹</Typography.Title>
            <Typography.Text type="secondary">从学习 SOP、掌握技能、补充资料和完成对话中沉淀能力。</Typography.Text>
          </div>
          <Button type="link" onClick={() => scrollToSection('logs')}>查看完整对话</Button>
        </div>
        <div className="employee-growth-line">
          {growthItems.map((item) => (
            <div className="employee-growth-node" key={`${item.kind}-${item.title}`}>
              <span className={`employee-growth-dot is-${item.tone}`}>{item.icon}</span>
              <small>{item.kind} · {item.time}</small>
              <strong>{item.title}</strong>
            </div>
          ))}
        </div>
      </section>

      <section className="employee-capability-wrap" id="capabilities">
        <div className="employee-section-head">
          <div>
            <Typography.Title level={4}>能力与工具</Typography.Title>
            <Typography.Text type="secondary">员工当前能用什么、会走哪些流程、能引用哪些业务资料。</Typography.Text>
          </div>
        </div>
        <div className="employee-capability-grid">
          {capabilityCards.map((item) => (
            <Card key={item.id} className="employee-capability-card">
              <div className="employee-capability-head">
                <span>{item.icon}</span>
                <strong>{item.title}</strong>
                <em>{item.count}</em>
              </div>
              <Typography.Paragraph ellipsis={{ rows: 2 }}>{item.body}</Typography.Paragraph>
              <Space>
                <Button type="link" onClick={() => scrollToSection(item.id)}>查看完整</Button>
                <Button type="link" onClick={() => scrollToSection(item.id)}>修改</Button>
              </Space>
            </Card>
          ))}
        </div>
      </section>

      <EmployeeSection id="skills" title="已掌握技能" icon={<ApiOutlined />}>
        <ResourceList items={activeGeneralSkills.map((item) => `${item.name} · ${item.slug}`)} empty="暂无启用技能" />
      </EmployeeSection>
      <EmployeeSection id="sop" title="SOP管理" icon={<ProfileOutlined />}>
        <ResourceList items={activeSkills.map((item) => `${item.name} · v${item.version}`)} empty="暂无启用 SOP" />
      </EmployeeSection>
      <EmployeeSection id="knowledge" title="业务资料" icon={<FileTextOutlined />}>
        <ResourceList items={activeKnowledge.map((item) => `${item.name} · ${item.bucket_count} 桶 / ${item.chunk_count} 片段`)} empty="暂无业务资料" />
      </EmployeeSection>
      <EmployeeSection id="tools" title="工具箱" icon={<ToolOutlined />}>
        <ResourceList items={activeTools.map((item) => `${item.display_name || item.name} · ${item.bucket}`)} empty="暂无启用工具" />
      </EmployeeSection>
      <EmployeeSection id="logs" title="对话日志" icon={<CommentOutlined />}>
        <ResourceList
          items={employeeSessions.slice(0, 8).map((item) => `${item.title || item.id} · ${item.summary || item.last_agent_question || item.status}`)}
          empty="暂无对话任务"
        />
      </EmployeeSection>
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

function EmployeeSection({ id, title, icon, children }: { id: string; title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <Card id={id} className="employee-detail-section" title={<Space>{icon}<span>{title}</span></Space>}>
      {children}
    </Card>
  );
}

function ResourceList({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <Typography.Text type="secondary">{empty}</Typography.Text>;
  return (
    <div className="employee-resource-list">
      {items.map((item) => <div key={item}>{item}</div>)}
    </div>
  );
}

function ConversationHeatmap({ byDay }: { byDay: Record<string, number> }) {
  const days = useMemo(() => heatmapDays(byDay), [byDay]);
  return (
    <div className="employee-heatmap">
      <div className="employee-heatmap-months">
        {monthLabels(days).map((item) => <span key={`${item.label}-${item.offset}`} style={{ gridColumnStart: item.offset + 1 }}>{item.label}</span>)}
      </div>
      <div className="employee-heatmap-body">
        <div className="employee-heatmap-weekdays">
          <span>周一</span>
          <span>周三</span>
          <span>周五</span>
        </div>
        <div className="employee-heatmap-grid">
          {days.map((day) => (
            <span
              key={day.key}
              className={`employee-heatmap-cell level-${Math.min(4, day.count)}`}
              title={`${day.key}: ${day.count} 轮对话`}
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
  const today = new Date();
  const start = new Date(today);
  start.setDate(today.getDate() - 7 * 25);
  const weekDay = (start.getDay() + 6) % 7;
  start.setDate(start.getDate() - weekDay);
  return Array.from({ length: 7 * 26 }, (_, index) => {
    const day = new Date(start);
    day.setDate(start.getDate() + index);
    const key = dateKey(day);
    return { key, date: day, count: byDay[key] || 0 };
  });
}

function monthLabels(days: ReturnType<typeof heatmapDays>) {
  const labels: Array<{ label: string; offset: number }> = [];
  let last = '';
  days.forEach((day, index) => {
    const label = `${day.date.getMonth() + 1}月`;
    if (label !== last && day.date.getDate() <= 7) {
      labels.push({ label, offset: Math.floor(index / 7) });
      last = label;
    }
  });
  return labels;
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
  knowledge: KnowledgeBaseRead[],
  sessions: EnterpriseChatSessionRead[],
) {
  const latestSession = sessions[0];
  return [
    {
      kind: '对话完成',
      title: latestSession ? compactSummary(latestSession.summary || latestSession.last_agent_question || latestSession.title || latestSession.id, 54) : '等待首个对话任务',
      time: latestSession ? relativeTime(latestSession.updated_at) : '暂无',
      icon: <CommentOutlined />,
      tone: 'green',
    },
    {
      kind: '学习 SOP',
      title: sops[0]?.name || '尚未学习 SOP',
      time: sops[0] ? relativeTime(sops[0].updated_at) : '待学习',
      icon: <ProfileOutlined />,
      tone: 'mint',
    },
    {
      kind: '掌握技能',
      title: generalSkills[0]?.name || '尚未启用技能',
      time: generalSkills[0] ? relativeTime(generalSkills[0].updated_at) : '待启用',
      icon: <CheckCircleOutlined />,
      tone: 'teal',
    },
    {
      kind: '业务资料',
      title: knowledge[0]?.name || '尚未绑定资料',
      time: knowledge[0] ? relativeTime(knowledge[0].updated_at) : '待绑定',
      icon: <BookOutlined />,
      tone: 'gold',
    },
  ];
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
