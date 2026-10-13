import {
  FileSearchOutlined,
  ProfileOutlined,
  RobotOutlined,
  SolutionOutlined,
  ToolOutlined,
  UsergroupAddOutlined,
} from '../icons';
import { notify } from '@/components/ui';
import {
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { RevealGroup } from '@/components/RevealGroup';
import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, TENANT_ID } from '../api/client';
import { downloadGeneralSkillPackage } from '../lib/skill-package';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';
import EmployeeAvatar from '../components/EmployeeAvatar';
import IconAgents from '../assets/icons/nav-agents.svg?react';
import IconFolder from '../assets/icons/cap-folder.svg?react';
import IconMagicWand from '../assets/icons/cap-magicwand.svg?react';
import IconClipboard from '../assets/icons/cap-clipboard.svg?react';
import IconBriefcase from '../assets/icons/cap-briefcase.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconAdd from '../assets/icons/add.svg?react';
import plazaKnowledgeIcon from '../assets/icons/plaza-knowledge.svg';
import plazaAgentIcon from '../assets/icons/plaza-agent.svg';
import plazaSkillIcon from '../assets/icons/plaza-skill.svg';
import plazaSopIcon from '../assets/icons/plaza-sop.svg';
import plazaToolIcon from '../assets/icons/plaza-tool.svg';
import {
  agentResourceCount,
  canManageEmployeeAgent,
  employeeDisplayNameWithCreator,
  employeeProfile,
  resourceDisplayNameWithCreator,
} from '../employee';
import type { AgentProfileRead, GeneralSkillRead, KnowledgeBaseRead, SkillRead, ToolRead } from '../types';

import AppHeader from '@/components/AppHeader';
import { Button as UIButton } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  PlatformEmployeeCard,
  PlatformEmployeeDrawer,
  PlatformGridSkeleton,
  PlatformResourceCard,
  PlatformResourceDrawer,
  type PlatformDetailKind,
  type PlatformResourceAccent,
  type PlatformStat,
} from '@/components/openPlatform';
import { isTeamScope, readEmployeeScope } from '@/lib/agent-scope-storage';
import {
  GENERAL_SKILL_CATEGORY_ALL,
  GENERAL_SKILL_CATEGORIES,
} from '@/lib/general-skill-categories';
import { SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';
import { agentCatalogMeta, listAgentCatalog, type AgentCatalogEntry } from '@/lib/agentCatalog';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

// 广场模块标识只保留一处定义（PlatformKindDetailView 里那份），这里只是本页的别名，
// 避免两处字面量联合类型各自漂移。
type PlatformKind = PlatformDetailKind;

/** 每个广场模块各自的数据加载状态：进页面只拉当前 tab 用到的接口。 */
type KindLoadState = 'idle' | 'loading' | 'ready' | 'error';

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
  /** 技能包累计下载次数（仅技能广场条目带）。 */
  downloadCount?: number;
  /** 标签分类（仅技能广场条目带；null = 未分类）。 */
  category?: string | null;
  agent?: AgentProfileRead;
  /** Agent 广场条目（来自本地清单，不落库）。 */
  agentEntry?: AgentCatalogEntry;
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
    kind: 'agent-apps',
    title: 'Agent 广场',
    subtitle: '平台统一定制的 Agent，直接使用，不需要配置。',
    detail: '这类 Agent 由平台统一维护，成员只能使用、不能编辑。',
    useLabel: '立即使用',
    metricLabel: 'Agent',
    signals: ['开箱即用', '只读不可编辑', '模型可选'],
    icon: <RobotOutlined />,
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
/*
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
  */
];

const PLATFORM_BY_KIND = new Map(PLATFORM_CONFIGS.map((item) => [item.kind, item]));

// SD1 line glyph shown in each column header, matching the sidebar mapping.
// 广场 tab 头部字号图标用的是 SVG 组件签名，而 `icons.tsx` 里的 ant 风格图标是
// `(props: IconProps) => JSX`（rotate 收窄成 number，与 SVGProps 不兼容），这里薄包一层。
const AgentAppsIcon: ComponentType<SVGProps<SVGSVGElement>> = ({ className }) => (
  <RobotOutlined className={typeof className === 'string' ? className : undefined} />
);

const PLATFORM_ICON: Record<PlatformKind, ComponentType<SVGProps<SVGSVGElement>>> = {
  'agent-apps': AgentAppsIcon,
  agents: IconAgents,
  knowledge: IconFolder,
  'general-skills': IconMagicWand,
  skills: IconClipboard,
  tools: IconBriefcase,
};

// Colorful 3D module icon shown on each广场 resource card (agents use avatars instead).
const PLATFORM_RESOURCE_ICON: Partial<Record<PlatformKind, string>> = {
  'agent-apps': plazaAgentIcon,
  knowledge: plazaKnowledgeIcon,
  'general-skills': plazaSkillIcon,
  skills: plazaSopIcon,
  tools: plazaToolIcon,
};

// Per-module accent color for the resource card meta line and tag pills (SD1 232:4634).
const PLATFORM_ACCENT: Partial<Record<PlatformKind, PlatformResourceAccent>> = {
  'agent-apps': 'violet',
  knowledge: 'green',
  'general-skills': 'indigo',
  skills: 'blue',
  tools: 'orange',
};

// Unit rendered after the header count, e.g. "12 员工" / "12 内容".
function platformCountLabel(kind: PlatformKind): string {
  if (kind === 'agents') return '员工';
  if (kind === 'agent-apps') return 'Agent';
  return '内容';
}

// 广场资源挂在 is_overall 的宿主数字员工下，模块接口都要带它的 agent_id 取广场作用域。
function overallSuffixFor(rows: AgentProfileRead[]): string {
  const overall = rows.find((item) => item.is_overall);
  return overall ? `&agent_id=${encodeURIComponent(overall.id)}` : '';
}

// Bottom metric segments for a 数字员工广场 card.
function employeeStats(agent: AgentProfileRead): PlatformStat[] {
  const usageCount = Math.max(0, Number(agent.usage_count || 0));
  return [
    { value: agentResourceCount(agent, 'knowledge_base'), label: '资料' },
    { value: agentResourceCount(agent, 'general_skill'), label: '技能' },
    { value: agentResourceCount(agent, 'skill'), label: 'SOP' },
    { value: usageCount > 9999 ? '9999+' : usageCount, label: '被使用' },
  ];
}

function resourceDrawerBadge(kind: PlatformKind, item: PlatformItem): string {
  if (kind === 'skills') {
    const parts = item.meta.split(' / ');
    return parts[parts.length - 1] || item.tags[0] || '';
  }
  return item.tags[0] || '';
}

function DetailSkeleton({ kind }: { kind: PlatformKind }) {
  return <PlatformGridSkeleton kind={kind} />;
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
  const initialKind = kind && PLATFORM_BY_KIND.has(kind) ? kind : 'agents';
  const [activeKind, setActiveKind] = useState<PlatformKind>(initialKind);
  // 广场首页已独立为侧边栏「首页」（/enterprise/home，HomePage.tsx），广场页只承载各资源列表。
  // 支持 ?q= 深链（技能分享页「去技能广场查看」带上技能名）：进页面即预填搜索词，
  // 广场自动过滤出目标技能；用户可随时清空改查。
  const [searchText, setSearchText] = useState(() => new URLSearchParams(window.location.search).get('q') || '');
  // 技能广场专用：标签分类下拉过滤（其他 tab 不显示，但状态保留、切回时可用）。
  const [skillCategoryFilter, setSkillCategoryFilter] = useState(GENERAL_SKILL_CATEGORY_ALL);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseRead[]>([]);
  const [generalSkills, setGeneralSkills] = useState<GeneralSkillRead[]>([]);
  const [skills, setSkills] = useState<SkillRead[]>([]);
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [kindStates, setKindStates] = useState<Partial<Record<PlatformKind, KindLoadState>>>({});
  // 首页角标：各模块可见资源数量来自聚合接口，一次请求即可填满 5 个 tab，不必等懒加载列表。
  const [galleryCounts, setGalleryCounts] = useState<Record<PlatformKind, number> | null>(null);
  const [deletingItemKey, setDeletingItemKey] = useState('');
  // 免费模型开关进行中的员工 id（'' = 空闲）
  const [freeModelTogglingKey, setFreeModelTogglingKey] = useState('');
  const [agentId, setAgentId] = useState(readEmployeeScope);
  const [detailItem, setDetailItem] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);
  // 下载中的技能 slug（'' = 空闲）；drawer 的 downloading 收窄成 Boolean
  const [downloadingSkill, setDownloadingSkill] = useState('');
  // 大包下载进度浮层，与技能广场/档案页同一套交互
  const [transfer, setTransfer] = useState<{ label: string; percent: number | null; receivedBytes?: number } | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<{ kind: PlatformKind; item: PlatformItem } | null>(null);

  // 已加载过的模块不重复拉；同一模块并发只发一次请求（ref 与 state 同步写，保证守卫是同步准确的）。
  const agentsCacheRef = useRef<AgentProfileRead[] | null>(null);
  const kindStatesRef = useRef<Partial<Record<PlatformKind, KindLoadState>>>({});
  const inFlightKindsRef = useRef<Set<PlatformKind>>(new Set());
  const setKindState = useCallback((target: PlatformKind, state: KindLoadState) => {
    kindStatesRef.current = { ...kindStatesRef.current, [target]: state };
    setKindStates(kindStatesRef.current);
  }, []);

  useEffect(() => {
    if (kind && PLATFORM_BY_KIND.has(kind)) {
      setActiveKind(kind);
    }
  }, [kind]);

  // 切 tab 时清空上一 tab 的搜索词；首轮挂载跳过——q= 深链在 useState 初始化时
  // 预填了分享页带来的搜索词，这里立刻清空就退化了（点击去技能广场检索不生效的根因）。
  const isFirstKindEffect = useRef(true);
  useEffect(() => {
    if (isFirstKindEffect.current) {
      isFirstKindEffect.current = false;
      return;
    }
    setSearchText('');
    setSkillCategoryFilter(GENERAL_SKILL_CATEGORY_ALL);
  }, [activeKind]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setAgentId(next && !isTeamScope(next) ? next : readEmployeeScope());
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  // 角标数量：独立轻接口，一次请求填满 5 个 tab。随 fetchAgents（enterprise/agents）同步
  // 触发；挂载时也提前拼一次（兜底深链直进页面的时序）。
  const galleryCountsFetchedRef = useRef(false);
  const fetchGalleryCounts = useCallback(() => {
    if (galleryCountsFetchedRef.current) return Promise.resolve();
    galleryCountsFetchedRef.current = true;
    // 后端 snake_case（general_skills），前端模块 key 是 'general-skills'，这里归一化。
    return api
      .get<Partial<Record<'agents' | 'knowledge' | 'general_skills' | 'skills' | 'tools', number>>>(
        `/api/enterprise/gallery/counts?tenant_id=${TENANT_ID}`,
      )
      .then((rows) => {
        // 字段缺失（旧后端）时保留 undefined，让 tab 回落到原有懒加载本地计数，而不是假 0。
        setGalleryCounts({
          agents: rows.agents,
          knowledge: rows.knowledge,
          'general-skills': rows.general_skills,
          skills: rows.skills,
          tools: rows.tools,
        } as Record<PlatformKind, number>);
      })
      .catch((error: unknown) => {
        // 允许重试：旧后端还没重启（404）时不至于永远没数字；下次 fetchAgents 时再拼。
        galleryCountsFetchedRef.current = false;
        if (import.meta.env.DEV) {
          console.warn('[gallery] counts request failed; badges fall back to local counts', error);
        }
      });
  }, []);

  const fetchAgents = useCallback(async (): Promise<AgentProfileRead[]> => {
    if (agentsCacheRef.current) return agentsCacheRef.current;
    const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
    agentsCacheRef.current = rows;
    setAgents(rows);
    // 角标数量随 agents 列表一起取：与模块列表同周期，依次闹一次（成功后不再重复拉）。
    void fetchGalleryCounts();
    return rows;
  }, [fetchGalleryCounts]);

  /**
   * 加载某个广场模块的数据。
   *
   * 数字员工列表是其他模块的前置依赖（拿 overall 宿主 id 拼 agent_id，以及"复制到员工"的目标），
   * 所以统一先确保它到位，再按 target 只拉对应模块那一个接口 —— 不再一进页面就把 5 个接口全打一遍。
   */
  const loadKindData = useCallback(async (target: PlatformKind, options?: { force?: boolean }) => {
    if (!options?.force) {
      const current = kindStatesRef.current[target];
      if (current === 'ready' || current === 'loading') return;
    }
    if (inFlightKindsRef.current.has(target)) return;
    inFlightKindsRef.current.add(target);
    if (options?.force && target === 'agents') agentsCacheRef.current = null;
    setKindState(target, 'loading');
    try {
      // Agent 广场的数据在本地清单里，不需要任何网络请求，直接落终态。
      if (target === 'agent-apps') {
        setKindState(target, 'ready');
        return;
      }
      const agentRows = await fetchAgents();
      if (target === 'knowledge') {
        setKnowledgeBases(await api.get<KnowledgeBaseRead[]>(`/api/enterprise/knowledge-bases?tenant_id=${TENANT_ID}${overallSuffixFor(agentRows)}`));
      } else if (target === 'general-skills') {
        // include_files=0：广场只要元信息，文件包内容单租户就有 MB 级体量
        setGeneralSkills(await api.get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${overallSuffixFor(agentRows)}&include_files=0`));
      } else if (target === 'skills') {
        const overall = agentRows.find((item) => item.is_overall);
        setSkills(overall
          ? await api.get<SkillRead[]>(`/api/enterprise/agents/${overall.id}/skills?tenant_id=${TENANT_ID}`)
          : []);
      } else if (target === 'tools') {
        setTools(await api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}${overallSuffixFor(agentRows)}`));
      }
      setKindState(target, 'ready');
    } catch (error) {
      // 失败也要落到终态，否则骨架屏会一直转
      setKindState(target, 'error');
      notify.error(error instanceof Error ? error.message : '加载开放广场失败');
    } finally {
      inFlightKindsRef.current.delete(target);
    }
  }, [fetchAgents, fetchGalleryCounts, setKindState]);

  useEffect(() => {
    void loadKindData(activeKind);
  }, [activeKind, loadKindData]);

  const activeKindState = kindStates[activeKind] || 'idle';
  const loading = activeKindState === 'idle' || activeKindState === 'loading';

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
    // Agent 广场的数据来自本地清单文件（src/data/agent-catalog.json），不请求后端：
    // 这批 Agent 是平台统一定制的，成员只能使用不能编辑，列表口径不随用户变化。
    // 唯一的例外是 adminOnly 条目（如 AI CodeReviewer），只进管理员视角。
    'agent-apps': listAgentCatalog(isAdmin).map((entry) => ({
      id: entry.entry,
      deleteKey: entry.entry,
      title: entry.name,
      description: entry.summary || entry.description || '平台定制的 Agent，可直接使用。',
      meta: agentCatalogMeta(entry),
      tags: [entry.category, ...entry.tags],
      agentEntry: entry,
    })),
    agents: visibleAgents.map((item) => {
      const profile = employeeProfile(item);
      return {
        id: item.id,
        deleteKey: item.id,
        title: employeeDisplayNameWithCreator(item),
        description: item.description || '广场开放的数字员工。',
        meta: profile.roleName,
        tags: [
          item.status === 'active' ? '在线' : '下线',
          `SOP ${agentResourceCount(item, 'skill')}`,
          `技能 ${agentResourceCount(item, 'general_skill')}`,
        ],
        agent: item,
      };
    }),
    knowledge: knowledgeBases
      .filter((item) => item.status === 'active' && !isEmptyDefaultKnowledgeBase(item))
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '广场沉淀的知识库。',
        meta: `${item.document_count} 文档 / ${item.bucket_count} 目录 / ${item.chunk_count} 引用`,
        tags: [item.version || 'v1.0.0', item.branch_sync_state || '广场版'],
      })),
    'general-skills': generalSkills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.slug,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '可复制到当前数字员工的技能。',
        meta: item.slug,
        downloadCount: item.download_count ?? 0,
        category: item.category,
        // 「N 次下载」放第 2 位：列表卡片只渲染前两个 tag（resourceDrawerBadge 读 tags[0]，
        // 顺序不能动 tags[0]），这样技能广场列表卡上直接能看到下载次数。
        tags: [item.homepage ? '外部能力' : '内置能力', `${item.download_count ?? 0} 次下载`, '已启用'],
      })),
    skills: skills
      .filter((item) => item.status === 'published')
      .map((item) => ({
        id: item.id,
        deleteKey: item.skill_id,
        title: resourceDisplayNameWithCreator(item.name, item),
        description: item.description || '可复制和复用的业务 SOP。',
        meta: `${item.skill_id} / ${item.version}`,
        tags: [item.business_domain || '业务流程', `${item.total_call_count || item.call_count || 0} 次调用`],
      })),
    tools: tools
      .filter((item) => item.enabled)
      .map((item) => ({
        id: item.id,
        deleteKey: item.id,
        title: resourceDisplayNameWithCreator(item.display_name || item.name, item),
        description: item.description || '可配置到员工工具的工具。',
        meta: `${item.bucket || '工具'} / ${item.tool_type.toUpperCase()}`,
        tags: [item.method, item.enabled ? '已启用' : '已停用'],
      })),
  }), [generalSkills, knowledgeBases, isAdmin, skills, tools, visibleAgents]);

  const filteredItems = useMemo(() => {
    const items = platformItems[activeKind];
    const scoped = activeKind === 'general-skills' && skillCategoryFilter !== GENERAL_SKILL_CATEGORY_ALL
      ? items.filter((item) => item.category === skillCategoryFilter)
      : items;
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return scoped;
    return scoped.filter((item) => [
      item.title,
      item.description,
      item.meta,
      item.category,
      item.tags.join(' '),
    ].some((value) => (value || '').toLowerCase().includes(keyword)));
  }, [activeKind, platformItems, searchText, skillCategoryFilter]);

  const tabItems = useMemo(
    () =>
      PLATFORM_CONFIGS.map((config) => ({
        value: config.kind,
        label: config.title,
        // 模块自身加载就绪时用本地列表长度（最准且随增删变化）；
        // 否则用聚合计数接口的数字填满未访问模块的角标；两者都不可用则不显示。
        count:
          // Agent 广场是本地清单，永远即时可得，不依赖懒加载状态与聚合计数
          config.kind === 'agent-apps'
            ? platformItems['agent-apps'].length
            : kindStates[config.kind] === 'ready'
              ? platformItems[config.kind].length
              : galleryCounts && galleryCounts[config.kind] !== undefined
                ? galleryCounts[config.kind]
                : undefined,
      })),
    [galleryCounts, kindStates, platformItems],
  );

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

  async function markPlatformAgentUsed(agent: AgentProfileRead) {
    const metadata = agent.metadata || {};
    if (metadata.used_by_current_user !== true && metadata.chat_used_by_current_user !== true) {
      await api.post<AgentProfileRead>(`/api/chat/agents/${agent.id}/use?tenant_id=${TENANT_ID}`, {});
    }
    setAgents((current) => current.map((item) => (
      item.id === agent.id
        ? {
          ...item,
          metadata: {
            ...(item.metadata || {}),
            used_by_current_user: true,
            chat_used_by_current_user: true,
          },
        }
        : item
    )));
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, agent.id);
    window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: agent.id } }));
    setAgentId(agent.id);
  }

  async function downloadPlazaSkillPackage(item: PlatformItem) {
    // general-skills 分支的 deleteKey 就是技能 slug（见 platformItems 构造）
    const slug = item.deleteKey || item.id;
    setDownloadingSkill(item.deleteKey || item.id);
    setTransfer({ label: `正在下载技能包 ${slug}.zip…`, percent: 0, receivedBytes: undefined });
    try {
      await downloadGeneralSkillPackage({
        slug,
        // 真实下载进度：XHR onprogress，无 Content-Length 时退化为已接收体积
        onProgress: ({ percent, receivedBytes, totalBytes }) => {
          setTransfer((current) => (current
            ? percent >= 0 || totalBytes > 0
              ? { ...current, percent: percent >= 0 ? percent : 0, receivedBytes }
              : { ...current, percent: null, receivedBytes }
            : current));
        },
      });
      notify.success(`已下载技能包：${slug}`);
      // 下载次数 +1 发生在后端，强刷技能列表让卡片上的「N 次下载」立即更新
      void loadKindData('general-skills', { force: true });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '技能包下载失败');
    } finally {
      setDownloadingSkill('');
      setTransfer(null);
    }
  }

  async function usePlatformItem(platformKind: PlatformKind, itemId?: string) {
    // Agent 广场：直接进该 Agent 的工作台，不做「复制到员工」这一步（Agent 本体不可编辑）。
    if (platformKind === 'agent-apps') {
      const entryId = itemId || platformItems['agent-apps'][0]?.id;
      if (!entryId) {
        notify.warning('Agent 广场暂无可用 Agent');
        return;
      }
      navigate(`/enterprise/agent-apps/${encodeURIComponent(entryId)}`);
      return;
    }
    if (platformKind === 'agents') {
      const agent = visibleAgents.find((item) => item.id === itemId) || visibleAgents[0];
      if (!agent) {
        notify.warning('广场暂无可用数字员工');
        return;
      }
      try {
        await markPlatformAgentUsed(agent);
        navigate('/enterprise/dashboard');
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '使用数字员工失败');
      }
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

  /** 管理员标记/取消免费员工；成功后强刷列表，并把抽屉持有的旧快照换成新对象。 */
  async function toggleAgentFreeModel(agent: AgentProfileRead, enabled: boolean) {
    setFreeModelTogglingKey(agent.id);
    try {
      await api.post<AgentProfileRead>(
        `/api/enterprise/agents/${encodeURIComponent(agent.id)}/free-model?tenant_id=${encodeURIComponent(TENANT_ID)}`,
        { enabled },
      );
      notify.success(enabled ? `已开启「${agent.name}」的免费模型` : `已关闭「${agent.name}」的免费模型`);
      // 只强刷列表的话，抽屉还拿着打开时的旧 agent 快照 → 开关会一直显示旧值，
      // 必须用新对象同步替换 detailItem（卡片列表随 setAgents 自动更新）。
      agentsCacheRef.current = null;
      const rows = await fetchAgents();
      setDetailItem((current) => {
        if (!current || current.kind !== 'agents') return current;
        const fresh = rows.find((row) => row.id === current.item.agent?.id);
        return fresh ? { kind: 'agents', item: { ...current.item, agent: fresh } } : current;
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '免费模型设置失败');
    } finally {
      setFreeModelTogglingKey('');
    }
  }

  async function runDelete() {    if (!confirmTarget) return;
    const { kind: platformKind, item } = confirmTarget;
    // Agent 广场对所有人只读（抽屉不暴露删除入口），这里再兜一层，防止将来误接。
    if (platformKind === 'agent-apps') {
      setConfirmTarget(null);
      return;
    }
    const key = platformItemDeleteKey(platformKind, item);
    setDeletingItemKey(key);
    try {
      if (platformKind === 'agents' && item.agent) {
        await api.post<AgentProfileRead>(
          `/api/enterprise/agents/${encodeURIComponent(item.agent.id)}/gallery:unpublish?tenant_id=${encodeURIComponent(TENANT_ID)}`,
          {},
        );
        window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
      } else {
        await api.delete(platformDeleteUrl(platformKind, item));
      }
      notify.success(platformKind === 'agents' ? '员工已从广场下线' : '已从广场移除');
      setDetailItem((current) => (
        current && current.kind === platformKind && current.item.id === item.id ? null : current
      ));
      setConfirmTarget(null);
      await loadKindData(platformKind, { force: true });
    } catch (error) {
      notify.error(error instanceof Error
        ? error.message
        : platformKind === 'agents'
          ? '员工广场下线失败'
          : '删除失败');
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

  function handleCreateGeneralSkill() {
    navigate('/enterprise/general-skills/new?scope=gallery');
  }

  function handleKindChange(next: PlatformKind) {
    setActiveKind(next);
    navigate(`/enterprise/platform/${next}`);
  }

  function renderItemDrawer() {
    if (!detailItem) return null;
    const config = PLATFORM_BY_KIND.get(detailItem.kind) || PLATFORM_CONFIGS[0];
    const { item } = detailItem;
    const deleteKey = platformItemDeleteKey(detailItem.kind, item);
    const drawerItems = platformItems[detailItem.kind];
    const drawerIndex = drawerItems.findIndex((entry) => entry.id === item.id);

    if (detailItem.kind === 'agents' && item.agent) {
      const agent = item.agent;
      const profile = employeeProfile(agent);
      const detailText = agent.persona_prompt
        || agent.description
        || config.detail;
      return (
        <PlatformEmployeeDrawer
          open
          agent={agent}
          platformTitle={config.title}
          name={item.title}
          role={item.meta}
          description={item.description}
          detailText={detailText}
          workStyles={profile.workStyles}
          stats={employeeStats(agent)}
          online={agent.status === 'active'}
          canManage={canManagePlatform}
          unpublishing={deletingItemKey === deleteKey}
          freeModelEnabled={agent.free_model_enabled === true}
          freeModelToggling={freeModelTogglingKey === agent.id}
          hasPrev={drawerIndex > 0}
          hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
          onClose={() => setDetailItem(null)}
          onPrev={() => navigateDetailItem(-1)}
          onNext={() => navigateDetailItem(1)}
          onUnpublish={() => setConfirmTarget({ kind: detailItem.kind, item })}
          onToggleFreeModel={(enabled) => void toggleAgentFreeModel(agent, enabled)}
          onUse={() => {
            setDetailItem(null);
            void usePlatformItem(detailItem.kind, item.id);
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
        description={item.agentEntry?.description || item.description}
        badge={resourceDrawerBadge(detailItem.kind, item)}
        categoryMeta={item.meta}
        categoryMetaLabel={detailItem.kind === 'agent-apps' ? '作者 / 更新时间' : undefined}
        detailText={item.agentEntry
          ? `能力：${item.agentEntry.capability}｜由平台统一维护，所有成员只能使用、不能编辑或删除。`
          : detailItem.kind === 'general-skills'
            ? `累计下载 ${item.downloadCount ?? 0} 次；${config.detail}`
            : config.detail}
        useLabel={config.useLabel}
        // Agent 广场对所有成员一律只读：不暴露删除/下线入口。
        canManage={detailItem.kind === 'agent-apps' ? false : canManagePlatform}
        deleting={deletingItemKey === deleteKey}
        hasPrev={drawerIndex > 0}
        hasNext={drawerIndex >= 0 && drawerIndex < drawerItems.length - 1}
        onClose={() => setDetailItem(null)}
        onPrev={() => navigateDetailItem(-1)}
        onNext={() => navigateDetailItem(1)}
        onDelete={detailItem.kind === 'agent-apps'
          ? undefined
          : () => setConfirmTarget({ kind: detailItem.kind, item })}
        onDownload={
          detailItem.kind === 'general-skills'
            ? () => void downloadPlazaSkillPackage(item)
            : undefined
        }
        downloading={Boolean(downloadingSkill) && detailItem.item.deleteKey === downloadingSkill}
        downloadProgress={downloadingSkill && detailItem.item.deleteKey === downloadingSkill
          ? transfer
            ? { percent: transfer.percent, receivedBytes: transfer.receivedBytes }
            : { percent: null }
          : null}
        onUse={() => {
          setDetailItem(null);
          void usePlatformItem(detailItem.kind, item.id);
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
        title={confirmTarget && config
          ? confirmTarget.kind === 'agents'
            ? `从广场下线员工「${confirmTarget.item.title}」？`
            : `删除${config.metricLabel}「${confirmTarget.item.title}」？`
          : ''}
        description={confirmTarget?.kind === 'agents'
          ? '下线后该员工不再出现在开放广场，也不能被新用户添加；员工本体及其资源、已有使用记录都会保留。'
          : '删除后该广场内容会从开放平台移除，已复制到员工侧的引用可能不再可同步。'}
        confirmText={confirmTarget?.kind === 'agents' ? '确认下线' : '删除'}
        loading={Boolean(confirmTarget) && deletingItemKey === (confirmTarget ? platformItemDeleteKey(confirmTarget.kind, confirmTarget.item) : '')}
        onConfirm={() => void runDelete()}
      />
    );
  }

  const config = PLATFORM_BY_KIND.get(activeKind) || PLATFORM_CONFIGS[0];
  const PlatformIcon = PLATFORM_ICON[activeKind];

  return (
    <div className="min-h-full box-border px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        className="mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="开放广场平台"
      />

      <div className="flex flex-col gap-[20px] rounded-[20px] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-center justify-between gap-[12px]">
          <div
            role="tablist"
            aria-label="广场类型"
            className="flex flex-wrap gap-[12px] max-[560px]:gap-[8px]"
          >
            {tabItems.map((item) => {
              const active = item.value === activeKind;
              return (
                <button
                  key={item.value}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => handleKindChange(item.value)}
                  className={cn(
                    'flex min-w-[120px] items-center justify-center gap-[8px] whitespace-nowrap rounded-[10px] border-[0.5px] px-[14px] py-[8px] text-[14px] leading-[normal] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-[#1a71ff] focus-visible:ring-offset-2',
                    active
                      ? 'border-[#1a71ff] bg-[#eef0ff] font-medium text-[#1a71ff]'
                      : 'border-[#e3e7f1] bg-white font-normal text-[#4f5669] hover:border-[#cbd3e6] hover:bg-[#f6f7fa] hover:text-[#18181a]',
                    'max-[560px]:min-w-[88px] max-[560px]:px-[10px] max-[560px]:py-[6px] max-[560px]:text-[12px]',
                  )}
                >
                  <span>{item.label}</span>
                  {item.count !== undefined && (
                    <span
                      className={cn(
                        'rounded-[90px] px-[7px] py-[1px] text-[11px] leading-none',
                        active ? 'bg-white text-[#1a71ff]' : 'bg-[#f2f4f8] text-[#757f9c]',
                      )}
                    >
                      {item.count}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          <div className="flex flex-wrap items-center gap-[8px]">
            {activeKind === 'general-skills' && (
                  <UIButton onClick={handleCreateGeneralSkill} className="h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]">
                    <IconAdd className="size-3.5" />
                    创建开放 Skill
                  </UIButton>
                )}
                <UIButton
                  variant="outline"
                  onClick={() => void loadKindData(activeKind, { force: true })}
                  disabled={loading}
                  className="h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
                >
                  <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
                  刷新
                </UIButton>
          </div>
        </div>

        <>
        <div className="flex flex-col gap-[10px] px-[12px]">
          <div className="flex items-center gap-[8px] text-[#18181a]">
            <PlatformIcon className="size-[16px] shrink-0" />
            <span className="text-[18px] font-medium leading-none">{config.title}</span>
          </div>

          {config.signals.length > 0 && (
            <div className="flex flex-wrap items-center gap-[8px]">
              {config.signals.map((signal) => (
                <span
                  key={signal}
                  className="rounded-[20px] border-[0.5px] border-[#e3e7f1] px-[10px] py-[3px] text-[11px] leading-[normal] text-[#757f9c]"
                >
                  {signal}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="flex flex-col gap-[16px]">
          <div className="flex flex-wrap items-center gap-[12px]">
            <label className="flex h-[34px] w-full max-w-[360px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a]">
              <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
              <input
                autoComplete="off"
                data-1p-ignore="true"
                data-lpignore="true"
                data-bwignore="true"
                value={searchText}
                placeholder={`搜索${platformCountLabel(activeKind)}`}
                onChange={(event) => setSearchText(event.target.value)}
                className="min-w-0 flex-1 border-0 bg-transparent text-[12px] text-[#18181a] outline-none placeholder:text-[#858b9c]"
              />
            </label>

            {activeKind === 'general-skills' && (
              <UISelect value={skillCategoryFilter} onValueChange={setSkillCategoryFilter}>
                <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-[130px]')} aria-label="标签分类筛选">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={GENERAL_SKILL_CATEGORY_ALL}>全部分类</SelectItem>
                  {GENERAL_SKILL_CATEGORIES.map((item) => (
                    <SelectItem key={item} value={item}>
                      {item}
                    </SelectItem>
                  ))}
                </SelectContent>
              </UISelect>
            )}
          </div>

          {loading ? (
            <DetailSkeleton kind={activeKind} />
          ) : filteredItems.length === 0 ? (
            <div className="grid min-h-[180px] w-full place-items-center content-center gap-[10px] rounded-[18px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] px-[20px] py-[40px] text-center font-bold text-[#8b94aa]">
              <IconSearch className="size-[20px] shrink-0" />
              <span>{platformItems[activeKind].length === 0 ? '暂无开放内容' : '没有匹配的广场内容'}</span>
            </div>
          ) : activeKind === 'agents' ? (
            <RevealGroup
              className="grid grid-cols-3 gap-[16px] max-[900px]:grid-cols-2 max-[560px]:grid-cols-1"
              itemCount={filteredItems.length}
              replayKey={activeKind}
            >
              {filteredItems.map((item) => item.agent && (
                <PlatformEmployeeCard
                  key={item.id}
                  avatar={(
                    <EmployeeAvatar
                      agent={item.agent}
                      width={66}
                      height={78}
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
                  freeBadge={item.agent.free_model_enabled === true}
                  onOpen={() => setDetailItem({ kind: activeKind, item })}
                  onUnpublish={canManagePlatform
                    ? () => setConfirmTarget({ kind: activeKind, item })
                    : undefined}
                  unpublishing={deletingItemKey === platformItemDeleteKey(activeKind, item)}
                />
              ))}
            </RevealGroup>
          ) : (
            <RevealGroup
              className="grid grid-cols-3 gap-[16px] max-[900px]:grid-cols-2 max-[560px]:grid-cols-1"
              itemCount={filteredItems.length}
              replayKey={activeKind}
            >
              {filteredItems.map((item) => (
                <PlatformResourceCard
                  key={item.id}
                  icon={PLATFORM_RESOURCE_ICON[activeKind]
                    ? <img src={PLATFORM_RESOURCE_ICON[activeKind]} alt="" className="size-[32px] shrink-0 object-contain" />
                    : undefined}
                  accent={PLATFORM_ACCENT[activeKind]}
                  title={item.title}
                  meta={item.meta}
                  description={item.description}
                  tags={item.tags.slice(0, 2)}
                  onClick={() => setDetailItem({ kind: activeKind, item })}
                />
              ))}
            </RevealGroup>
          )}
        </div>
        </>
      </div>

      {renderItemDrawer()}
      {renderConfirm()}

      {transfer && (
        <div aria-live="polite" className="fixed bottom-[24px] right-[24px] z-[60] w-[300px] rounded-[14px] border border-[#e4e9f2] bg-white/95 p-[14px] shadow-[0_8px_24px_rgba(0,0,0,0.12)]">
          <p className="mb-[8px] flex items-center justify-between gap-[8px] text-[12px] text-[#464C5E]">
            <span className="truncate">{transfer.label}</span>
            <span className="tabular-nums text-[#757F9C]">
              {transfer.percent !== null && transfer.percent > 0
                ? `${transfer.percent}%`
                : transfer.receivedBytes
                  ? `已接收 ${(transfer.receivedBytes / 1_048_576).toFixed(1)} MB`
                  : ''}
            </span>
          </p>
          <div
            role="progressbar"
            aria-label="技能包下载进度"
            aria-valuenow={transfer.percent ?? undefined}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-[6px] w-full overflow-hidden rounded-full bg-[#eef1f7]"
          >
            <div
              className={cn(
                'h-full rounded-full bg-[#18181a] transition-[width] duration-150',
                (transfer.percent === null || transfer.percent === 0) && 'w-1/3 animate-pulse',
              )}
              style={transfer.percent !== null && transfer.percent > 0 ? { width: `${transfer.percent}%` } : undefined}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function isEmptyDefaultKnowledgeBase(item: KnowledgeBaseRead): boolean {
  return item.name === '默认知识库' && item.document_count === 0 && item.bucket_count === 0 && item.chunk_count === 0;
}
