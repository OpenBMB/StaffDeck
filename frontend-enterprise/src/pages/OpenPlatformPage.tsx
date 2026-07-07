import {
  FileSearchOutlined,
  ProfileOutlined,
  SolutionOutlined,
  ToolOutlined,
  UsergroupAddOutlined,
} from '../icons';
import { notify } from '@/components/ui';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, TENANT_ID } from '../api/client';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';
import EmployeeAvatar from '../components/EmployeeAvatar';
import IconAgents from '../assets/icons/nav-agents.svg?react';
import IconFolder from '../assets/icons/cap-folder.svg?react';
import IconMagicWand from '../assets/icons/cap-magicwand.svg?react';
import IconClipboard from '../assets/icons/cap-clipboard.svg?react';
import IconBriefcase from '../assets/icons/cap-briefcase.svg?react';
import plazaKnowledgeIcon from '../assets/icons/plaza-knowledge.svg';
import plazaSkillIcon from '../assets/icons/plaza-skill.svg';
import plazaSopIcon from '../assets/icons/plaza-sop.svg';
import plazaToolIcon from '../assets/icons/plaza-tool.svg';
import { canManageEmployeeAgent, employeeDisplayName, employeeProfile } from '../employee';
import type { AgentProfileRead, GeneralSkillRead, KnowledgeBaseRead, SkillRead, ToolRead } from '../types';

import AppHeader from '@/components/AppHeader';
import {
  PlatformColumn,
  PlatformEmployeeCard,
  PlatformEmployeeDrawer,
  PlatformKindDetailView,
  PlatformResourceCard,
  PlatformResourceDrawer,
  type PlatformResourceAccent,
  type PlatformStat,
} from '@/components/openPlatform';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type PlatformKind = 'agents' | 'knowledge' | 'general-skills' | 'skills' | 'tools';

type PlatformConfig = {
  kind: PlatformKind;
  title: string;
  subtitle: string;
  detail: string;
  useLabel: string;
  metricLabel: string;
  signals: string[];
  icon: ReactNode;
};

type PlatformItem = {
  id: string;
  deleteKey?: string;
  title: string;
  description: string;
  meta: string;
  tags: string[];
  agent?: AgentProfileRead;
};

const PLATFORM_CONFIGS: PlatformConfig[] = [
  {
    kind: 'agents',
    title: '数字员工广场',
    subtitle: '已发布到广场，可在对话端直接使用。',
    detail: '选择一个数字员工查看能力、岗位和服务范围。',
    useLabel: '使用此员工',
    metricLabel: '数字员工',
    signals: ['聊天可用', '支持对话', '查看能力'],
    icon: <UsergroupAddOutlined />,
  },
  {
    kind: 'knowledge',
    title: '知识库广场',
    subtitle: '发布到广场的知识库，可复制到你的数字员工。',
    detail: '从广场复制到当前数字员工的知识库。',
    useLabel: '复制到知识库',
    metricLabel: '知识库',
    signals: ['知识图谱', '引用来源', '可复制'],
    icon: <FileSearchOutlined />,
  },
  {
    kind: 'general-skills',
    title: '技能广场',
    subtitle: '浏览器、MCP、查询工具等可复用能力。',
    detail: '从广场复制到当前数字员工的技能。',
    useLabel: '复制到技能',
    metricLabel: '技能',
    signals: ['运行测试', 'MCP/浏览器', '能力复用'],
    icon: <SolutionOutlined />,
  },
  {
    kind: 'skills',
    title: 'SOP 广场',
    subtitle: '可复制和复用的业务流程与执行规范。',
    detail: '从广场复制到当前数字员工的 SOP。',
    useLabel: '复制到 SOP',
    metricLabel: '业务 SOP',
    signals: ['流程推进', '执行规范', '可复制'],
    icon: <ProfileOutlined />,
  },
  {
    kind: 'tools',
    title: '工具广场',
    subtitle: '可开放给员工调用和测试的工具能力。',
    detail: '前往工具页按现有流程配置和测试工具。',
    useLabel: '前往工具页',
    metricLabel: '工具能力',
    signals: ['调用权限', '测试可用', '工具配置'],
    icon: <ToolOutlined />,
  },
];

const PLATFORM_BY_KIND = new Map(PLATFORM_CONFIGS.map((item) => [item.kind, item]));

// SD1 line glyph shown in each column header, matching the sidebar mapping.
const PLATFORM_ICON: Record<PlatformKind, ComponentType<SVGProps<SVGSVGElement>>> = {
  agents: IconAgents,
  knowledge: IconFolder,
  'general-skills': IconMagicWand,
  skills: IconClipboard,
  tools: IconBriefcase,
};

// Colorful 3D module icon shown on each广场 resource card (agents use avatars instead).
const PLATFORM_RESOURCE_ICON: Partial<Record<PlatformKind, string>> = {
  knowledge: plazaKnowledgeIcon,
  'general-skills': plazaSkillIcon,
  skills: plazaSopIcon,
  tools: plazaToolIcon,
};

// Per-module accent color for the resource card meta line and tag pills (SD1 232:4634).
const PLATFORM_ACCENT: Partial<Record<PlatformKind, PlatformResourceAccent>> = {
  knowledge: 'green',
  'general-skills': 'indigo',
  skills: 'blue',
  tools: 'orange',
};

// Unit rendered after the header count, e.g. "12 员工" / "12 内容".
function platformCountLabel(kind: PlatformKind): string {
  return kind === 'agents' ? '员工' : '内容';
}

// Bottom metric segments for a 数字员工广场 card.
function employeeStats(agent: AgentProfileRead): PlatformStat[] {
  return [
    { value: resourceCount(agent, 'knowledge_base'), label: '资料' },
    { value: resourceCount(agent, 'general_skill'), label: '技能' },
    { value: resourceCount(agent, 'skill'), label: 'SOP' },
  ];
}

function resourceDrawerBadge(kind: PlatformKind, item: PlatformItem): string {
  if (kind === 'skills') {
    const parts = item.meta.split(' / ');
    return parts[parts.length - 1] || item.tags[0] || '';
  }
  return item.tags[0] || '';
}

export default function OpenPlatformPage({
  currentUser,
  isAdmin = false,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onLogout?: () => void;
}) {
  const navigate = useNavigate();
  const { kind } = useParams<{ kind?: PlatformKind }>();
  const selectedKind = kind && PLATFORM_BY_KIND.has(kind) ? kind : undefined;
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [deletingItemKey, setDeletingItemKey] = useState('');
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [detailItem, setDetailItem] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const nextAgentId = (event as CustomEvent<{ agentId?: string }>).detail?.agentId
        || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)
        || '';
      setAgentId(nextAgentId);
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  const loadPlatformData = useCallback(async () => {
    setLoading(true);
    try {
      const agentRows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      const overall = agentRows.find((item) => item.is_overall);
      const overallSuffix = overall ? `&agent_id=${encodeURIComponent(overall.id)}` : '';
      const [kbRows, generalRows, skillRows, toolRows] = await Promise.all([
        api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${overallSuffix}`),
        api.get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${overallSuffix}`),
        overall
          ? api.get<SkillRead[]>(`/api/enterprise/agents/${overall.id}/skills?tenant_id=${TENANT_ID}`)
          : Promise.resolve([]),
        api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}${overallSuffix}`),
      ]);
      setAgents(agentRows);
      setKnowledgeBases(kbRows);
      setGeneralSkills(generalRows);
      setSkills(skillRows);
      setTools(toolRows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载开放广场失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPlatformData();
  }, [loadPlatformData]);

  const visibleAgents = useMemo(
    () => agents.filter((item) => !item.is_overall && item.status === 'active' && isGalleryEmployee(item)),
    [agents],
  );
  const overallAgent = agents.find((item) => item.is_overall) || null;
  const canManagePlatform = isAdmin;
  const currentAgent = agents.find((item) => item.id === agentId);
  const targetEmployee = currentAgent && canManageEmployeeAgent(currentAgent, currentUser)
    ? currentAgent
    : agents.find((item) => canManageEmployeeAgent(item, currentUser) && !item.is_overall);

  const platformItems = useMemo<Record<PlatformKind, PlatformItem[]>>(() => ({
    agents: visibleAgents.map((item) => {
      const profile = employeeProfile(item);
      return {
        id: item.id,
        deleteKey: item.id,
        title: employeeDisplayName(item),
        description: item.description || '广场开放的数字员工。',
        meta: profile.roleName,
        tags: [
          item.status === 'active' ? '在线' : '下线',
          `SOP ${resourceCount(item, 'skill')}`,
          `技能 ${resourceCount(item, 'general_skill')}`,
        ],
        agent: item,
      };
    }),
    knowledge: knowledgeBases
      .filter((item) => item.status === 'active' && !isEmptyDefaultKnowledgeBase(item))
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: item.name,
        description: item.description || '广场沉淀的知识库。',
        meta: `${item.document_count} 文档 / ${item.bucket_count} 桶 / ${item.chunk_count} 片段`,
        tags: [item.version || 'v1.0.0', item.branch_sync_state || '广场版'],
      })),
    'general-skills': generalSkills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.slug,
        title: item.name,
        description: item.description || '可复制到当前数字员工的技能。',
        meta: item.slug,
        tags: [item.homepage ? '外部能力' : '内置能力', '已启用'],
      })),
    skills: skills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: item.name,
        description: item.description || '可复制和复用的业务 SOP。',
        meta: `${item.skill_id} / ${item.version}`,
        tags: [item.business_domain || '业务流程', `${item.total_call_count || item.call_count || 0} 次调用`],
      })),
    tools: tools
      .filter((item) => item.enabled)
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: item.display_name || item.name,
        description: item.description || '可配置到员工工具的工具。',
        meta: `${item.bucket || '工具'} / ${item.tool_type.toUpperCase()}`,
        tags: [item.method, item.enabled ? '已启用' : '已停用'],
      })),
  }), [generalSkills, knowledgeBases, skills, tools, visibleAgents]);

  const platformStats = PLATFORM_CONFIGS.map((config) => ({
    ...config,
    count: platformItems[config.kind].length,
  }));

  function ensureTargetEmployee(): boolean {
    if (!targetEmployee) {
      notify.warning('请先选择一个员工，再从广场复制资源。');
      return false;
    }
    if (targetEmployee.id !== agentId) {
      window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, targetEmployee.id);
      window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: targetEmployee.id } }));
      setAgentId(targetEmployee.id);
    }
    return true;
  }

  function usePlatformItem(platformKind: PlatformKind, itemId?: string) {
    if (platformKind === 'agents') {
      const agent = visibleAgents.find((item) => item.id === itemId) || visibleAgents[0];
      if (!agent) {
        notify.warning('广场暂无可用数字员工');
        return;
      }
      window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
      window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: agent.id } }));
      navigate('/enterprise/dashboard');
      return;
    }
    if (!ensureTargetEmployee()) return;
    const resourceParam = itemId ? `&resourceId=${encodeURIComponent(itemId)}` : '';
    if (platformKind === 'knowledge') navigate(`/enterprise/knowledge?add=plaza${resourceParam}`);
    if (platformKind === 'general-skills') navigate(`/enterprise/general-skills?add=plaza${resourceParam}`);
    if (platformKind === 'skills') navigate(`/enterprise/skills?add=plaza${resourceParam}`);
    if (platformKind === 'tools') navigate('/enterprise/tools?add=plaza');
  }

  function platformItemDeleteKey(platformKind: PlatformKind, item: PlatformItem): string {
    return `${platformKind}:${item.deleteKey || item.id}`;
  }

  function platformDeleteUrl(platformKind: PlatformKind, item: PlatformItem): string {
    const resourceKey = encodeURIComponent(item.deleteKey || item.id);
    const overallSuffix = overallAgent ? `&agent_id=${encodeURIComponent(overallAgent.id)}` : '';
    if (platformKind === 'agents') return `/api/enterprise/agents/${resourceKey}?tenant_id=${TENANT_ID}`;
    if (platformKind === 'knowledge') return `/api/enterprise/knowledge-bases/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
    if (platformKind === 'general-skills') return `/api/enterprise/general-skills/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
    if (platformKind === 'skills') return `/api/enterprise/skills/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
    return `/api/enterprise/tools/${resourceKey}?tenant_id=${TENANT_ID}${overallSuffix}`;
  }

  async function runDelete() {
    if (!confirmTarget) return;
    const { kind: platformKind, item } = confirmTarget;
    const key = platformItemDeleteKey(platformKind, item);
    setDeletingItemKey(key);
    try {
      await api.delete(platformDeleteUrl(platformKind, item));
      notify.success('已删除广场内容');
      setDetailItem((current) => (
        current && current.kind === platformKind && current.item.id === item.id ? null : current
      ));
      setConfirmTarget(null);
      await loadPlatformData();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeletingItemKey('');
    }
  }

  function navigateDetailItem(offset: -1 | 1) {
    if (!detailItem) return;
    const items = platformItems[detailItem.kind];
    const currentIndex = items.findIndex((entry) => entry.id === detailItem.item.id);
    const nextItem = items[currentIndex + offset];
    if (!nextItem) return;
    setDetailItem({ kind: detailItem.kind, item: nextItem });
  }

  function renderItemDrawer() {
    if (!detailItem) return null;
    const config = PLATFORM_BY_KIND.get(detailItem.kind) || PLATFORM_CONFIGS[0];
    const { item } = detailItem;
    const deleteKey = platformItemDeleteKey(detailItem.kind, item);
    const drawerItems = platformItems[detailItem.kind];
    const drawerIndex = drawerItems.findIndex((entry) => entry.id === item.id);

    if (detailItem.kind === 'agents' && item.agent) {
      const profile = employeeProfile(item.agent);
      const detailText = item.agent.persona_prompt
        || item.agent.description
        || config.detail;
      return (
        <PlatformEmployeeDrawer
          open
          agent={item.agent}
          platformTitle={config.title}
          name={item.title}
          role={item.meta}
          description={item.description}
          detailText={detailText}
          workStyles={profile.workStyles}
          stats={employeeStats(item.agent)}
          online={item.agent.status === 'active'}
          canManage={canManagePlatform}
          deleting={deletingItemKey === deleteKey}
          hasPrev={drawerIndex > 0}
          hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
          onClose={() => setDetailItem(null)}
          onPrev={() => navigateDetailItem(-1)}
          onNext={() => navigateDetailItem(1)}
          onDelete={() => setConfirmTarget({ kind: detailItem.kind, item })}
          onUse={() => {
            setDetailItem(null);
            usePlatformItem(detailItem.kind, item.id);
          }}
        />
      );
    }

    return (
      <PlatformResourceDrawer
        open
        platformTitle={config.title}
        icon={PLATFORM_RESOURCE_ICON[detailItem.kind]
          ? <img src={PLATFORM_RESOURCE_ICON[detailItem.kind]} alt="" className="size-[36px] object-contain" />
          : <span className="grid size-[36px] place-items-center text-[#757f9c]">{config.icon}</span>}
        accent={PLATFORM_ACCENT[detailItem.kind]}
        title={item.title}
        description={item.description}
        badge={resourceDrawerBadge(detailItem.kind, item)}
        categoryMeta={item.meta}
        detailText={config.detail}
        useLabel={config.useLabel}
        canManage={canManagePlatform}
        deleting={deletingItemKey === deleteKey}
        hasPrev={drawerIndex > 0}
        hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
        onClose={() => setDetailItem(null)}
        onPrev={() => navigateDetailItem(-1)}
        onNext={() => navigateDetailItem(1)}
        onDelete={() => setConfirmTarget({ kind: detailItem.kind, item })}
        onUse={() => {
          setDetailItem(null);
          usePlatformItem(detailItem.kind, item.id);
        }}
      />
    );
  }

  function renderConfirm() {
    const config = confirmTarget ? PLATFORM_BY_KIND.get(confirmTarget.kind) || PLATFORM_CONFIGS[0] : null;
    return (
      <ConfirmDialog
        open={Boolean(confirmTarget)}
        onOpenChange={(next) => { if (!next) setConfirmTarget(null); }}
        title={confirmTarget && config ? `删除${config.metricLabel}「${confirmTarget.item.title}」？` : ''}
        description={confirmTarget?.kind === 'agents'
          ? '删除后该数字员工会从广场和员工列表移除，相关资源绑定也会一并清理。'
          : '删除后该广场内容会从开放平台移除，已复制到员工侧的引用可能不再可同步。'}
        loading={Boolean(confirmTarget) && deletingItemKey === (confirmTarget ? platformItemDeleteKey(confirmTarget.kind, confirmTarget.item) : '')}
        onConfirm={() => void runDelete()}
      />
    );
  }

  if (selectedKind) {
    const config = PLATFORM_BY_KIND.get(selectedKind) || PLATFORM_CONFIGS[0];
    const PlatformIcon = PLATFORM_ICON[selectedKind];
    return (
      <>
        <PlatformKindDetailView
          kind={selectedKind}
          title={config.title}
          subtitle={config.subtitle}
          countLabel={platformCountLabel(selectedKind)}
          signals={config.signals}
          icon={PlatformIcon}
          items={platformItems[selectedKind]}
          loading={loading}
          employeeStats={employeeStats}
          onBack={() => navigate('/enterprise/platform')}
          onRefresh={() => void loadPlatformData()}
          onOpenItem={(item) => setDetailItem({ kind: selectedKind, item })}
          onLogout={onLogout}
          userName={currentUser?.username}
        />
        {renderItemDrawer()}
        {renderConfirm()}
      </>
    );
  }

  return (
    <div className="flex min-h-full flex-col box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px] xl:h-full xl:min-h-0 xl:overflow-hidden">
      <AppHeader
        className="mb-[24px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="开放广场平台"
      />
      <div className="mx-auto grid w-full grid-cols-1 gap-[12px] sm:grid-cols-2 xl:min-h-0 xl:flex-1 xl:grid-cols-5 xl:grid-rows-1">
        {platformStats.map((platform) => {
          const items = platformItems[platform.kind];
          const previews = items;
          const PlatformIcon = PLATFORM_ICON[platform.kind];
          return (
            <PlatformColumn
              key={platform.kind}
              icon={<PlatformIcon className="size-[14px]" />}
              title={platform.title}
              count={platform.count}
              countLabel={platformCountLabel(platform.kind)}
              filters={platform.signals}
              loading={loading}
              isEmpty={previews.length === 0}
              onViewAll={() => navigate(`/enterprise/platform/${platform.kind}`)}
            >
              {previews.map((item) => (
                platform.kind === 'agents' && item.agent ? (
                  <PlatformEmployeeCard
                    key={item.id}
                    avatar={(
                      <EmployeeAvatar
                        agent={item.agent}
                        width={50}
                        height={59}
                        fit="contain"
                        objectPosition="center bottom"
                        className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
                      />
                    )}
                    name={item.title}
                    role={item.meta}
                    online={item.agent.status === 'active'}
                    description={item.description}
                    stats={employeeStats(item.agent)}
                    onOpen={() => setDetailItem({ kind: platform.kind, item })}
                  />
                ) : (
                  <PlatformResourceCard
                    key={item.id}
                    icon={PLATFORM_RESOURCE_ICON[platform.kind]
                      ? <img src={PLATFORM_RESOURCE_ICON[platform.kind]} alt="" className="size-[32px] shrink-0 object-contain" />
                      : undefined}
                    accent={PLATFORM_ACCENT[platform.kind]}
                    title={item.title}
                    meta={item.meta}
                    description={item.description}
                    tags={item.tags.slice(0, 2)}
                    onClick={() => setDetailItem({ kind: platform.kind, item })}
                  />
                )
              ))}
            </PlatformColumn>
          );
        })}
      </div>
      {renderItemDrawer()}
      {renderConfirm()}
    </div>
  );
}

function resourceCount(agent: AgentProfileRead, resourceType: string): number {
  return (agent.resources || []).filter((item) => item.resource_type === resourceType && item.status !== 'inactive').length;
}

function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead): boolean {
  return item.name === '默认知识库' && item.document_count === 0 && item.bucket_count === 0 && item.chunk_count === 0;
}
