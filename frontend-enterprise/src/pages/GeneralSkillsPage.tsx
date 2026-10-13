import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  GithubOutlined,
  PlusOutlined,
  TeamOutlined,
  UploadOutlined,
} from '../icons';
import type { ChangeEvent, DragEvent, HTMLAttributes, ReactNode } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Ban, ChevronRight, CircleCheck, Copy, Download, Eye, EyeOff, FilePlus2, FolderPlus, Share2, Users } from 'lucide-react';
import { ContextMenu } from 'radix-ui';

import { api, streamPost, TENANT_ID } from '../api/client';
import { downloadGeneralSkillPackage } from '../lib/skill-package';
import { isEnterpriseAdmin, type EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import CapabilityScopeLoading from '@/components/CapabilityScopeLoading';
import {
  CapabilityScopeBadge,
  CapabilityScopeControl,
  normalizeCapabilityScope,
} from '@/components/CapabilityScopeControl';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { SkillShareDialog } from '@/components/SkillShareDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { ModelConfigDropdown } from '@/components/ModelConfigDropdown';
import { Paginator } from '@/components/Paginator';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Input,
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { isTeamScope, readEmployeeScope } from '@/lib/agent-scope-storage';
import { missingSkillReferencePaths } from '@/lib/general-skill-import-error';
import {
  MENU_CONTENT_CLASS,
  MENU_ITEM_CLASS,
  MENU_ITEM_DANGER_CLASS,
  MOBILE_CARD_CLASS,
  SELECT_TRIGGER_CLASS,
  formatDateTime,
} from '@/lib/enterprise-ui';
import { StatCard } from '@/components/StatCard';
import { ResourceImportDialog } from '@/components/ResourceImportDialog';
import CodeBlock, { renderCodeTokens } from '../components/CodeBlock';
import { renderMarkdownBlocks } from './chat/chatHelpers';
import IconAdd from '../assets/icons/add.svg?react';
import IconArrowRight from '../assets/icons/arrow-right.svg?react';
import IconFolder from '../assets/icons/cap-folder.svg?react';
import IconMagicWand from '../assets/icons/cap-magicwand.svg?react';
import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconPlay from '../assets/icons/play.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconProfileFile from '../assets/icons/profile-file.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconSkill from '../assets/icons/plaza-skill.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import {
  canManageEmployeeAgent,
  openGalleryAgentId,
  openGalleryImportSourceOptions,
  resourceCreatorName,
  visibleEmployeeAgents,
} from '../employee';
import { useClientPagination } from '../hooks/useClientPagination';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import type { BadgeTone } from './scheduled-tasks/shared';
import type {
  AgentProfileRead,
  CapabilityScope,
  GeneralSkillRead,
  GeneralSkillRunResponse,
  ModelConfigRead,
} from '../types';

const GENERAL_SKILL_PAGE_SIZE = 10;
//: 技能广场的标签分类：新建/编辑表单与下拉过滤共用这一份口径。
const GENERAL_SKILL_CATEGORIES = ['办公类', '财务类', '人力资源类', '编程类'] as const;
//: Radix Select 不接受空串 value，用一个哨兵值表示「未分类」。
const GENERAL_SKILL_CATEGORY_NONE = '__none__';
const GENERAL_SKILL_RUN_MODEL_STORAGE_KEY = 'general-skill-run-model';

const STATUS_BADGE: Record<GeneralSkillRead['status'], { tone: BadgeTone; text: string }> = {
  draft: { tone: 'blue', text: '草稿' },
  published: { tone: 'green', text: '已启用' },
  archived: { tone: 'gray', text: '已停用' },
};

const EMPTY_SKILL_MARKDOWN = `# 技能说明

在这里编写技能文档。名称、Slug 和描述由上方表单维护，系统不会从文档中自动抽取。`;

const SECTION_CARD_CLASS =
  'flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-[#FFF] p-[18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]';
const SECTION_CARD_TITLE_CLASS = 'text-[14px] font-medium text-[#18181a]';
const FIELD_LABEL_CLASS = 'text-[13px] font-medium text-[#18181a]';
const RETURN_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6]! hover:bg-white! hover:text-[#18181a]! aria-expanded:border-[#cbd3e6]! aria-expanded:bg-white! aria-expanded:text-[#18181a]!';
const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const DELETE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#d20b0b] hover:border-[#f3b6b6]! hover:bg-[#fce7e7]! hover:text-[#d20b0b]! aria-expanded:border-[#f3b6b6]! aria-expanded:bg-[#fce7e7]! aria-expanded:text-[#d20b0b]!';
const EDITOR_ACTION_OUTLINE_CLASS = RETURN_BUTTON_CLASS;
const EDITOR_ACTION_PRIMARY_CLASS = PRIMARY_BUTTON_CLASS;
const HIDDEN_FILE_INPUT_CLASS =
  'pointer-events-none fixed size-px opacity-0 [inset:auto_auto_0_0]';
const SKILL_EDITOR_DRAG_ACTIVE_CLASS =
  'ring-1 ring-[#18181a]/20 shadow-[0_-4px_16px_0_rgba(0,0,0,0.08)]';
const SKILL_DROP_HINT_CLASS =
  'pointer-events-none absolute inset-x-[18px] bottom-[18px] top-[46px] z-[6] flex items-center justify-center gap-3 rounded-[14px] border border-dashed border-[#18181a] bg-white/90 text-[15px] font-semibold text-[#18181a] shadow-sm backdrop-blur-sm';
const SKILL_FILE_EDITOR_CLASS =
  'grid min-h-[560px] flex-1 grid-cols-[minmax(180px,240px)_minmax(0,1fr)] overflow-hidden border-t border-[#e3e7f1] bg-[#fafafa]';
const SKILL_FILE_TREE_CLASS =
  'grid min-w-0 grid-rows-[auto_minmax(0,1fr)_auto] border-r border-[#e3e7f1] bg-white';
const SKILL_FILE_TREE_HEADER_CLASS =
  'flex min-h-[44px] items-center gap-2 border-b border-[#e3e7f1] bg-[#f6f6f6] px-[14px] text-[12px] font-medium text-[#757f9c]';
const SKILL_FILE_TREE_LIST_CLASS =
  'min-h-0 overflow-auto bg-white p-2';
const SKILL_FILE_TREE_ACTIONS_CLASS =
  'flex gap-2 border-t border-[#e3e7f1] bg-white p-[10px]';
const SKILL_FILE_PANE_CLASS =
  'grid min-w-0 grid-rows-[auto_minmax(0,1fr)]';
const SKILL_FILE_TAB_CLASS =
  'flex min-h-[44px] items-center gap-2 border-b border-[#e3e7f1] bg-[#f6f6f6] px-[14px] text-[12px] font-medium text-[#757f9c]';
const SKILL_FILE_TAB_ACTION_BUTTON_CLASS =
  'inline-flex h-[28px] shrink-0 items-center gap-[4px] rounded-[6px] px-[8px] text-[12px] font-medium text-[#757f9c] transition-colors hover:bg-[#edf1f7] hover:text-[#18181a] disabled:pointer-events-none disabled:opacity-40';
const SKILL_CODE_EDITOR_CLASS =
  'relative min-h-0 overflow-hidden bg-[#fafafa] font-mono text-[13px] leading-[1.7] tab-[2] shadow-[inset_0_1px_0_#e3e7f1]';
const SKILL_MARKDOWN_PREVIEW_CLASS =
  'min-h-0 overflow-auto bg-white p-[18px_20px] text-[14px] leading-[1.8] text-[#18181a]';
const SKILL_MARKDOWN_PREVIEW_BODY_CLASS =
  '[&>h1]:mb-4 [&>h1]:text-[22px] [&>h1]:font-semibold [&>h1]:leading-[1.35] [&>h2]:mb-3 [&>h2]:mt-6 [&>h2]:text-[18px] [&>h2]:font-semibold [&>h2]:leading-[1.4] [&>h3]:mb-2 [&>h3]:mt-5 [&>h3]:text-[16px] [&>h3]:font-semibold [&>p]:mb-3 [&>p]:whitespace-pre-wrap [&>ul]:mb-3 [&>ol]:mb-3 [&>ul]:pl-6 [&>ol]:pl-6 [&>li]:mb-1 [&>blockquote]:mb-3 [&>blockquote]:border-l-2 [&>blockquote]:border-[#e3e7f1] [&>blockquote]:pl-4 [&>blockquote]:text-[#757f9c] [&>code]:rounded-[4px] [&>code]:bg-[#f6f7fb] [&>code]:px-1 [&>code]:py-[1px] [&>code]:font-mono [&>pre]:mb-3 [&>pre]:overflow-auto [&>pre]:rounded-[10px] [&>pre]:bg-[#f6f7fb] [&>pre]:p-4 [&_table]:mb-3 [&_table]:w-full [&_table]:border-collapse [&_th]:border [&_th]:border-[#e3e7f1] [&_th]:bg-[#f6f7fb] [&_th]:px-3 [&_th]:py-2 [&_td]:border [&_td]:border-[#e3e7f1] [&_td]:px-3 [&_td]:py-2';
const SKILL_CODE_HIGHLIGHT_CLASS =
  'pointer-events-none absolute inset-0 z-[1] m-0 overflow-hidden whitespace-pre p-[18px_20px] text-[#18181a] tab-[2]';
const SKILL_CODE_HIGHLIGHT_CODE_CLASS =
  'block w-max min-w-full font-[inherit] will-change-transform';
const SKILL_CODE_INPUT_CLASS =
  'absolute inset-0 z-[2] m-0 size-full min-h-0 resize-none overflow-auto rounded-none border-0 bg-transparent! p-[18px_20px] font-[inherit] leading-[inherit] tracking-normal whitespace-pre text-transparent caret-[#18181a] outline-none tab-[2] [scrollbar-gutter:stable] selection:bg-[rgba(0,120,215,0.24)] [-webkit-text-fill-color:transparent]';
const SKILL_RESULT_LAYOUT_CLASS = 'grid gap-5';
const SKILL_SECTION_LABEL_CLASS =
  'mb-2 text-[12px] font-semibold text-[#757f9c]';
const SKILL_REPLY_PANEL_CLASS =
  'rounded-xl border border-[#eceef1] bg-white p-[16px_18px]';
const SKILL_REPLY_TEXT_CLASS =
  'mb-0! text-[15px] leading-[1.8] text-[#18181a]';
const SKILL_TRACE_LIST_CLASS =
  'grid gap-[10px] rounded-xl border border-[#eceef1] bg-[#fbfcfd] p-[12px_14px]';
const SKILL_TRACE_ITEM_CLASS =
  'grid min-w-0 grid-cols-[12px_minmax(0,1fr)] gap-[10px]';
const SKILL_TRACE_ITEM_BODY_CLASS = 'min-w-0 max-w-full';
const SKILL_TRACE_DOT_CLASS =
  'mt-[9px] size-[7px] shrink-0 rounded-full bg-[#18181a]';
const SKILL_TRACE_TITLE_CLASS =
  'text-[13px] font-semibold text-[#18181a]';
const SKILL_TRACE_MESSAGE_CLASS =
  'mt-[2px] break-words text-[12px] leading-[1.55] text-[#757f9c]';
const SKILL_TRACE_CODE_DETAILS_CLASS =
  'group/gs-trace box-border w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-[#eceef1] bg-white';
const SKILL_TRACE_CODE_SUMMARY_CLASS =
  'flex min-h-[38px] cursor-pointer list-none items-center gap-2 px-3 py-[9px] text-[12px] font-semibold text-[#18181a] select-none group-open/gs-trace:border-b group-open/gs-trace:border-[#eceef1] [&::-webkit-details-marker]:hidden';
const SKILL_CODE_BLOCK_CLASS =
  'm-0 max-h-[520px] max-w-full overflow-auto whitespace-pre border-0 p-[16px_18px] font-mono text-[12px] leading-[1.65]';
const SKILL_OUTPUT_STACK_CLASS = 'grid gap-[10px]';

function skillFileNodeClass(active: boolean) {
  return cn(
    'flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-lg border-0 bg-transparent px-[10px] py-2 text-left text-[12px] text-[#757f9c] transition-[background,color,box-shadow] duration-150',
    'hover:bg-[#f6f6f6] hover:text-[#18181a]',
    active && 'bg-[#f6f6f6] text-[#18181a]',
  );
}

function TraceDisclosureLabel() {
  return (
    <span className="ml-auto text-[12px] font-medium text-[#757f9c]">
      <span className="group-open/gs-trace:hidden">展开</span>
      <span className="hidden group-open/gs-trace:inline">收起</span>
    </span>
  );
}

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const GENERAL_SKILL_RUN_IDLE_TIMEOUT_MS = 600_000;
const FOLDER_INPUT_PROPS = {
  webkitdirectory: '',
  directory: '',
} as Record<string, string>;

type GeneralSkillFile = {
  path: string;
  content: string;
  size?: number;
  mime_type?: string;
};

type SkillFileTreeNode =
  | { kind: 'folder'; name: string; path: string; children: SkillFileTreeNode[] }
  | { kind: 'file'; name: string; path: string; file: GeneralSkillFile };

type DroppedSkillFile = {
  file: File;
  path: string;
};

type GeneralSkillImportMode = 'plaza' | 'employee';

type SkillFileSystemEntry = {
  name: string;
  fullPath: string;
  isFile: boolean;
  isDirectory: boolean;
};

type SkillFileEntry = SkillFileSystemEntry & {
  file: (success: (file: File) => void, failure?: (error: DOMException) => void) => void;
};

type SkillDirectoryEntry = SkillFileSystemEntry & {
  createReader: () => {
    readEntries: (
      success: (entries: SkillFileSystemEntry[]) => void,
      failure?: (error: DOMException) => void,
    ) => void;
  };
};

const PHASE_LABELS: Record<string, string> = {
  skill_loaded: '加载技能',
  planning: '生成执行方案',
  plan_created: '生成代码',
  attempt_started: '开始运行',
  running_code: '运行代码',
  stdout_chunk: '运行输出',
  stderr_chunk: '错误输出',
  code_finished: '读取运行结果',
  code_timeout: '运行超时',
  reflection_passed: '校验通过',
  reflection_retrying: '反思修复',
  reflection_stopped: '停止重试',
  repair_planning: '重新生成代码',
  repair_failed: '修复失败',
  plan_failed: '生成失败',
  replying: '生成回复',
  reply_created: '完成回复',
  reply_failed: '回复失败',
};

function formatJson(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function codeLanguage(value: string, fallback = 'text'): string {
  const trimmed = value.trim();
  if (!trimmed) return fallback;
  try {
    JSON.parse(trimmed);
    return 'json';
  } catch {
    return fallback;
  }
}

function isSkillPackageArchive(file: File): boolean {
  const name = file.name.toLowerCase();
  const type = file.type.toLowerCase();
  return name.endsWith('.zip') || type === 'application/zip' || type === 'application/x-zip-compressed';
}

function RunCodePanel({
  title,
  code,
  language,
  defaultOpen = false,
  className,
}: {
  title: string;
  code: string;
  language?: string;
  defaultOpen?: boolean;
  className?: string;
}) {
  return (
    <details className={cn(SKILL_TRACE_CODE_DETAILS_CLASS, 'mt-0', className)} open={defaultOpen}>
      <summary className={SKILL_TRACE_CODE_SUMMARY_CLASS}>
        {title}
        <TraceDisclosureLabel />
      </summary>
      <CodeBlock className={SKILL_CODE_BLOCK_CLASS} code={code} language={language || codeLanguage(code)} />
    </details>
  );
}

type GeneralSkillPageProps = {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

export function GeneralSkillNewPage(props: GeneralSkillPageProps = {}) {
  return <GeneralSkillEditorPage mode="new" {...props} />;
}

export function GeneralSkillEditPage(props: GeneralSkillPageProps = {}) {
  return <GeneralSkillEditorPage mode="edit" {...props} />;
}

export default function GeneralSkillsPage({ embedded = false, currentUser, onLogout }: { embedded?: boolean } & GeneralSkillPageProps) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState(() => {
    // 支持 ?q= 深链（如技能分享页「去技能广场查看」带上技能名）：进页面即预填搜索词，
    // 广场自动过滤出目标技能；用户可随时清空改查。
    return new URLSearchParams(window.location.search).get('q') || '';
  });
  const [statusFilter, setStatusFilter] = useState<'all' | GeneralSkillRead['status']>('all');
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [agentId, setAgentId] = useState(readEmployeeScope);
  const [isOverallAgent, setIsOverallAgent] = useState(true);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [clawhubModalOpen, setClawhubModalOpen] = useState(false);
  const [clawhubSource, setClawhubSource] = useState('');
  const [clawhubLoading, setClawhubLoading] = useState(false);
  const clawhubAbortRef = useRef<AbortController | null>(null);
  const [agentImportOpen, setAgentImportOpen] = useState(false);
  const [agentImportMode, setAgentImportMode] = useState<GeneralSkillImportMode>('plaza');
  const [agentImportLoading, setAgentImportLoading] = useState(false);
  const [agentImportProgress, setAgentImportProgress] = useState<{ done: number; total: number; current: string } | null>(null);
  const [agentImportAgents, setAgentImportAgents] = useState<AgentProfileRead[]>([]);
  const [agentImportSourceAgentId, setAgentImportSourceAgentId] = useState('');
  const [agentImportSourceSkills, setAgentImportSourceSkills] = useState<GeneralSkillRead[]>([]);
  const [agentImportSelectedSkillIds, setAgentImportSelectedSkillIds] = useState<string[]>([]);
  const [agentScopeLoaded, setAgentScopeLoaded] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<GeneralSkillRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [missingRefsPrompt, setMissingRefsPrompt] = useState<MissingReferencesPrompt | null>(null);
  // 大包下载进度浮层（percent=null 且 receivedBytes>0 = 响应无 Content-Length）
  const [transfer, setTransfer] = useState<{ label: string; percent: number | null; receivedBytes?: number } | null>(null);
  // 技能分享：当前分享的目标技能 + 已生成的 token（弹窗内展示/复制链接）
  const [shareDialogRow, setShareDialogRow] = useState<GeneralSkillRead | null>(null);
  const [shareToken, setShareToken] = useState('');
  const [shareCreating, setShareCreating] = useState(false);

  /** 命中「缺参考文件」的 400 时挂起二次确认；返回 true 表示已接管错误提示。 */
  function promptForMissingReferences(error: unknown, retry: () => void | Promise<void>): boolean {
    const paths = missingSkillReferencePaths(error);
    if (!paths.length) return false;
    setMissingRefsPrompt({ paths, retry });
    return true;
  }

  function confirmMissingReferences() {
    const prompt = missingRefsPrompt;
    setMissingRefsPrompt(null);
    if (prompt) void prompt.retry();
  }

  const pageTitle = isOverallAgent ? '技能广场' : '技能';
  const listLabel = isOverallAgent ? '技能广场列表' : '技能列表';
  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const canManageCurrentScope = isOverallAgent
    ? true
    : currentAgent
      ? canManageEmployeeAgent(currentAgent, currentUser)
      : isEnterpriseAdmin(currentUser);
  // 广场技能（metadata.scope === 'open_gallery'）只能从广场进入编辑器，跳转时带上
  // ?scope=gallery，否则编辑器会按员工作用域解析，返回时掉出广场。
  const editRouteFor = (row: GeneralSkillRead) =>
    `/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit${
      row.metadata?.scope === 'open_gallery' ? '?scope=gallery' : ''
    }`;

  const load = () => {
    const agentSuffix = agentId && !isOverallAgent ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    setLoading(true);
    return api
      // include_files=0：列表只用元信息启停/跳转，文件包单租户 MB 级，拉全量根本不需要
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentSuffix}&include_files=0`)
      .then(setRows)
      .catch((error) => notify.error(error.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (!agentScopeLoaded) return;
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, isOverallAgent, agentScopeLoaded]);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setAgents(items);
        setIsOverallAgent(Boolean(items.find((item) => item.id === agentId)?.is_overall ?? true));
        setAgentScopeLoaded(true);
      })
      .catch(() => {
        setIsOverallAgent(true);
        setAgentScopeLoaded(true);
      });
  }, [agentId]);

  useEffect(() => {
    if (searchParams.get('add') !== 'plaza') return;
    if (!agentScopeLoaded) return;
    const resourceId = searchParams.get('resourceId') || undefined;
    if (isOverallAgent) {
      notify.warning('请先选择一个数字员工，再从广场复制技能');
    } else {
      void requestAgentImport('plaza', resourceId);
    }
    const next = new URLSearchParams(searchParams);
    next.delete('add');
    next.delete('resourceId');
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentScopeLoaded, isOverallAgent, searchParams, setSearchParams]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setAgentId(next && !isTeamScope(next) ? next : readEmployeeScope());
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    return rows.filter((row) => {
      const matchesStatus = statusFilter === 'all' || row.status === statusFilter;
      const matchesCategory = categoryFilter === 'all' || row.category === categoryFilter;
      const haystack = [
        row.name,
        row.slug,
        row.description,
        row.category,
        row.homepage,
        resourceCreatorName(row),
      ].filter(Boolean).join(' ').toLowerCase();
      return matchesStatus && matchesCategory && (!keyword || haystack.includes(keyword));
    });
  }, [rows, searchText, statusFilter, categoryFilter]);

  const pagination = useClientPagination(filteredRows, GENERAL_SKILL_PAGE_SIZE, `${searchText}|${statusFilter}|${categoryFilter}`);

  const stats = useMemo(() => ({
    total: rows.length,
    published: rows.filter((row) => row.status === 'published').length,
    draft: rows.filter((row) => row.status === 'draft').length,
    archived: rows.filter((row) => row.status === 'archived').length,
  }), [rows]);

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    try {
      const agentSuffix = agentId && !isOverallAgent ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${row.slug}/${published ? 'publish' : 'archive'}?tenant_id=${TENANT_ID}${agentSuffix}`,
      );
      setRows((current) => current.map((item) => (item.id === next.id ? next : item)));
      notify.success(published ? '已启用技能' : '已停用技能');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : published ? '启用失败' : '停用失败');
    }
  }

  async function publishSkillToGallery(row: GeneralSkillRead) {
    if (!agentId) return;
    try {
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${encodeURIComponent(row.slug)}/publish-to-gallery?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(agentId)}`,
      );
      setRows((current) => current.map((item) => (item.id === next.id ? next : item)));
      notify.success('已发布到技能广场');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发布到广场失败');
    }
  }

  async function downloadSkillPackage(row: GeneralSkillRead) {
    // 大包下载可能持续几十秒：显示真实下载进度条，不再「傻等」
    setTransfer({ label: `正在下载技能包 ${row.slug}.zip…`, percent: 0 });
    try {
      await downloadGeneralSkillPackage({
        slug: row.slug,
        // 广场作用域（isOverallAgent）不带 agent_id —— 后端据此走「开放广场」可见性分支
        agentId: isOverallAgent ? null : agentId,
        onProgress: ({ percent, receivedBytes, totalBytes }) => {
          if (percent >= 0) {
            setTransfer((current) => (current ? { ...current, percent } : current));
          } else if (totalBytes === 0 && receivedBytes > 0) {
            // 无 Content-Length：只展示已接收体积
            setTransfer((current) => (current
              ? { ...current, percent: null, receivedBytes }
              : current));
          }
        },
      });
      notify.success(`已下载技能包：${row.slug}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '技能包下载失败');
    } finally {
      setTransfer(null);
    }
  }

  /**
   * 分享技能：请求后端拿 token，弹出复制链接。published 才可分享；
   * 只有创建者/管理员能看到「分享」入口（菜单里已过滤），这里再拦一次权限错误。
   */
  async function requestSkillShare(row: GeneralSkillRead) {
    setShareDialogRow(row);
    setShareToken('');
  }

  async function confirmDeleteSkill() {
    const row = deleteTarget;
    if (!row) return;
    const branchMode = !isOverallAgent;
    setDeleting(true);
    try {
      const agentSuffix = agentId && !isOverallAgent ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      await api.delete(`/api/enterprise/general-skills/${row.slug}?tenant_id=${TENANT_ID}${agentSuffix}`);
      setRows((current) => current.filter((item) => item.id !== row.id));
      notify.success(branchMode ? '已移除技能' : '已删除技能');
      setDeleteTarget(null);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : branchMode ? '移除失败' : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  function requestClawHubImport() {
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = null;
    setClawhubLoading(false);
    setClawhubSource('');
    setClawhubModalOpen(true);
  }

  function cancelClawHubImport() {
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = null;
    setClawhubLoading(false);
    setClawhubModalOpen(false);
  }

  async function requestAgentImport(mode: GeneralSkillImportMode, selectedResourceId?: string) {
    try {
      const agents = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      const firstSource = mode === 'plaza'
        ? openGalleryAgentId(agents)
        : visibleEmployeeAgents(agents, currentUser, { activeOnly: true, excludeAgentId: agentId })[0]?.id || '';
      setAgentImportMode(mode);
      setAgentImportAgents(agents);
      setAgentImportSourceAgentId(firstSource);
      setAgentImportSelectedSkillIds([]);
      setAgentImportOpen(true);
      if (firstSource) {
        const sourceRows = await loadAgentImportSourceSkills(firstSource);
        if (selectedResourceId && sourceRows.some((item) => item.id === selectedResourceId)) {
          setAgentImportSelectedSkillIds([selectedResourceId]);
        }
      } else {
        setAgentImportSourceSkills([]);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工列表失败');
    }
  }

  async function loadAgentImportSourceSkills(sourceAgentId: string): Promise<GeneralSkillRead[]> {
    setAgentImportSourceSkills([]);
    setAgentImportSelectedSkillIds([]);
    if (!sourceAgentId) return [];
    try {
      const sourceRows = await api.get<GeneralSkillRead[]>(
        `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(sourceAgentId)}&include_files=0`,
      );
      const existingIds = new Set(rows.map((item) => item.id));
      // 已绑到当前员工的技能也要展示（disabled + 「已添加」标记）：全部静默过滤掉会
      // 让「从广场复制」弹窗明明有技能力却一片空白，看起来像没有可用技能。
      const publishedRows = sourceRows.filter((item) => item.status === 'published');
      setAgentImportSourceSkills(publishedRows.map((item) => ({
        ...item,
        __is_bound: existingIds.has(item.id),
      })));
      return publishedRows;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载来源技能失败');
      return [];
    }
  }

  async function submitAgentImportSkills() {
    if (!agentId) {
      notify.warning('请先选择一个数字员工');
      return;
    }
    if (!agentImportSourceAgentId) {
      notify.warning(agentImportMode === 'plaza' ? '请选择开放广场' : '请选择复制来源');
      return;
    }
    if (!agentImportSelectedSkillIds.length) {
      notify.warning('请选择要复制的技能');
      return;
    }
    setAgentImportLoading(true);
    try {
      // 大包复制服务端耗时与单个技能包体积成正比，一批一 POST 无法展示真实进度。
      // 改为逐个提交：每完成 1 个推进一格，进度是真实的；失败项不中断整批。
      const done: string[] = [];
      const failed: string[] = [];
      for (const [index, resourceId] of agentImportSelectedSkillIds.entries()) {
        const sourceSkill = agentImportSourceSkills.find((item) => item.id === resourceId);
        setAgentImportProgress({ done: index, total: agentImportSelectedSkillIds.length, current: sourceSkill?.name || '' });
        try {
          await api.post(`/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/import`, {
            tenant_id: TENANT_ID,
            source_agent_id: agentImportSourceAgentId,
            resource_type: 'general_skill',
            resource_ids: [resourceId],
          });
          done.push(resourceId);
        } catch (error) {
          console.warn('复制技能失败', sourceSkill?.name || resourceId, error);
          failed.push(sourceSkill?.name || resourceId);
        }
      }
      setAgentImportProgress({ done: agentImportSelectedSkillIds.length, total: agentImportSelectedSkillIds.length, current: '' });
      if (failed.length) {
        notify.error(`已复制 ${agentImportSelectedSkillIds.length - failed.length} 个，失败 ${failed.length} 个：${failed.slice(0, 5).join('、')}${failed.length > 5 ? '…' : ''}`);
      } else {
        notify.success(`已复制 ${done.length} 个技能`);
      }
      setAgentImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制技能失败');
    } finally {
      setAgentImportLoading(false);
      setAgentImportProgress(null);
    }
  }

  async function importClawHubSource(allowMissingReferences = false) {
    if (!clawhubSource.trim()) {
      notify.warning('请输入开源平台地址、GitHub 仓库或 SKILL.md 链接');
      return;
    }
    const controller = new AbortController();
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = controller;
    setClawhubLoading(true);
    try {
      const row = await api.postWithSignal<GeneralSkillRead>('/api/enterprise/general-skills/import-skillhub', {
        tenant_id: TENANT_ID,
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        source: clawhubSource.trim(),
        status: 'published',
        allow_missing_references: allowMissingReferences,
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已新增 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setClawhubModalOpen(false);
      // 导入＝创建新技能，留在技能广场列表看结果（不再跳进新建技能的编辑页）
      void load();
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
      if (promptForMissingReferences(error, () => importClawHubSource(true))) return;
      notify.error(error instanceof Error ? error.message : '从开源平台导入失败');
    } finally {
      if (clawhubAbortRef.current === controller) {
        clawhubAbortRef.current = null;
        setClawhubLoading(false);
      }
    }
  }

  function renderActions(row: GeneralSkillRead) {
    const published = row.status === 'published';
    // 广场作用域：编辑/启停/删除等管理操作仅对管理员或创建者可见（全员可发布，但管理受限）；
    // 「下载」对所有能看到该技能的人开放，所以菜单本身不再整体隐藏。
    const canManage =
      !isOverallAgent ||
      isEnterpriseAdmin(currentUser) ||
      row.metadata?.owner_user_id === currentUser?.id;
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="技能操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem
            className={MENU_ITEM_CLASS}
            onSelect={() => void downloadSkillPackage(row)}
          >
            <Download />
            下载
          </DropdownMenuItem>
          {published && (
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              onSelect={() => void requestSkillShare(row)}
            >
              <Share2 />
              分享
            </DropdownMenuItem>
          )}
          {canManage && (
            <>
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                onSelect={() => navigate(editRouteFor(row))}
              >
                <IconEdit />
                {isOverallAgent ? '编辑' : '编辑本地版本'}
              </DropdownMenuItem>
              {published ? (
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void setSkillPublished(row, false)}>
                  <Ban />
                  停用
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void setSkillPublished(row, true)}>
                  <CircleCheck />
                  启用
                </DropdownMenuItem>
              )}
              {!isOverallAgent && row.metadata?.scope === 'agent_private' && (
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void publishSkillToGallery(row)}>
                  <UploadOutlined />
                  发布到广场
                </DropdownMenuItem>
              )}
              <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
              <DropdownMenuItem
                variant="destructive"
                className={MENU_ITEM_DANGER_CLASS}
                onSelect={() => setDeleteTarget(row)}
              >
                <IconTrash />
                {isOverallAgent ? '删除' : '移除'}
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  const columns: DataTableColumn<GeneralSkillRead>[] = [
    {
      key: 'name',
      title: '名称',
      width: 200,
      className: 'text-[#18181a]',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate font-medium leading-[18px] text-[#18181a]" title={row.name}>
            {row.name}
          </span>
          <span className="truncate text-[#858b9c]" title={row.slug}>
            {row.slug}
          </span>
        </div>
      ),
    },
    {
      key: 'description',
      title: '描述',
      className: 'whitespace-normal',
      render: (row) => <span className="line-clamp-2 wrap-break-word">{row.description || '暂无描述'}</span>,
    },
    {
      key: 'files',
      title: '文件',
      width: 90,
      render: (row) => `${row.skill_files?.length || 1} 个`,
    },
    {
      key: 'capability_scope',
      title: '能力范围',
      width: 105,
      render: (row) => <CapabilityScopeBadge value={row.capability_scope} />,
    },
    {
      key: 'category',
      title: '标签分类',
      width: 110,
      render: (row) => (
        row.category
          ? <StatusBadge tone="blue">{row.category}</StatusBadge>
          : <span className="text-[#c0c6d4]">未分类</span>
      ),
    },
    {
      key: 'creator',
      title: '创建者',
      width: 120,
      render: (row) => (
        <span className="block truncate text-[#858b9c]" title={resourceCreatorName(row)}>
          {resourceCreatorName(row) || '-'}
        </span>
      ),
    },
    {
      key: 'status',
      title: '状态',
      width: 100,
      render: (row) => {
        const preset = STATUS_BADGE[row.status] || { tone: 'gray' as BadgeTone, text: row.status };
        return <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>;
      },
    },
    {
      key: 'updated',
      title: '更新时间',
      width: 170,
      render: (row) => formatDateTime(row.updated_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => renderActions(row),
    },
  ];

  const renderMobileCard = (row: GeneralSkillRead) => {
    const preset = STATUS_BADGE[row.status] || { tone: 'gray' as BadgeTone, text: row.status };
    return (
      <article className={MOBILE_CARD_CLASS} key={row.id}>
        <div className="flex min-w-0 items-start justify-between gap-[10px]">
          <div className="min-w-0">
            <strong className="block truncate text-[14px] font-semibold text-[#18181a]">{row.name}</strong>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.slug}</span>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">创建者：{resourceCreatorName(row) || '-'}</span>
          </div>
          {renderActions(row)}
        </div>
        {row.description && (
          <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">{row.description}</p>
        )}
        <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
          <div className="flex items-center gap-[6px]">
            <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>
            <CapabilityScopeBadge value={row.capability_scope} />
            {row.category && <StatusBadge tone="blue">{row.category}</StatusBadge>}
          </div>
          <span>{row.skill_files?.length || 1} 个文件 · {formatDateTime(row.updated_at)}</span>
        </div>
      </article>
    );
  };

  const listEmptyText = isOverallAgent
    ? canManageCurrentScope ? '暂无技能，点击「新增」创建一个吧' : '暂无技能'
    : '当前员工暂无技能';

  if (!agentScopeLoaded) return <CapabilityScopeLoading />;

  return (
    <div className={embedded ? undefined : 'min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]'}>
      {!embedded && <AppHeader onLogout={onLogout} userName={currentUser?.username} title={pageTitle} />}
      {/* embedded（数字员工档案页 skills tab）也要有添加/刷新入口：员工作用域下的
          「从广场复制」就是“把已有技能绑到该员工身上”的唯一路径 */}
      <div className={embedded ? 'mb-[16px] flex items-center justify-end gap-[12px]' : 'mt-[20px] mb-[16px] flex items-center justify-end gap-[12px]'}>
            <UIButton
              variant="outline"
              onClick={() => void load()}
              disabled={loading}
              className="h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
              刷新
            </UIButton>
            {canManageCurrentScope && (
              <DropdownMenu>
                <DropdownMenuTrigger data-guide-target="skills-create" className="flex h-[34px] items-center gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white outline-none transition-colors hover:bg-[#303030]">
                  <IconAdd className="size-[14px]" />
                  {isOverallAgent ? '新增' : '添加技能'}
                  <IconChevronDown className="size-[12px]" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
                  {isOverallAgent && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => navigate('/enterprise/general-skills/new')}>
                      <IconAdd />
                      新建技能
                    </DropdownMenuItem>
                  )}
                  {!isOverallAgent && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void requestAgentImport('plaza')}>
                      <Copy />
                      从广场添加已有技能
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestClawHubImport()}>
                    <GithubOutlined />
                    从开源平台导入
                  </DropdownMenuItem>
                  {!isOverallAgent && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void requestAgentImport('employee')}>
                      <Users />
                      从数字员工复制
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-[#FFF] p-[18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="技能统计">
          <StatCard label="技能总数" value={stats.total} />
          <StatCard label="已启用" value={stats.published} tone="green" />
          <StatCard label="草稿" value={stats.draft} />
          <StatCard label="已停用" value={stats.archived} />
        </div>

        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconMagicWand className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">{listLabel}</span>
          </div>

          <div className="flex flex-wrap items-center gap-[16px]">
            <label className="flex h-[34px] w-[300px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
              <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
              <input
                autoComplete="off"
                data-1p-ignore="true"
                data-lpignore="true"
                data-bwignore="true"
                value={searchText}
                placeholder="搜索技能名称、Slug、描述或主页"
                onChange={(event) => setSearchText(event.target.value)}
                className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
              />
              {searchText && (
                <button
                  type="button"
                  aria-label="清除搜索"
                  onClick={() => setSearchText('')}
                  className="grid size-[16px] shrink-0 place-items-center text-[#c0c6d4] hover:text-[#858b9c]"
                >
                  <IconClear className="size-[14px]" />
                </button>
              )}
            </label>
            <UISelect value={statusFilter} onValueChange={(value) => setStatusFilter(value as 'all' | GeneralSkillRead['status'])}>
              <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-[130px]')} aria-label="状态筛选">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部状态</SelectItem>
                <SelectItem value="published">已启用</SelectItem>
                <SelectItem value="draft">草稿</SelectItem>
                <SelectItem value="archived">已停用</SelectItem>
              </SelectContent>
            </UISelect>
            <UISelect value={categoryFilter} onValueChange={setCategoryFilter}>
              <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-[130px]')} aria-label="标签分类筛选">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部分类</SelectItem>
                {GENERAL_SKILL_CATEGORIES.map((item) => (
                  <SelectItem key={item} value={item}>{item}</SelectItem>
                ))}
              </SelectContent>
            </UISelect>
          </div>

          <div className="grid gap-[10px] md:hidden">
            {filteredRows.length ? (
              pagination.pagedItems.map(renderMobileCard)
            ) : (
              <div className="py-[40px] text-center text-[13px] text-[#858b9c]">{listEmptyText}</div>
            )}
          </div>

          <div className="hidden md:block">
            <DataTable
              aria-label="技能列表"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText={listEmptyText}
            />
          </div>

          {filteredRows.length > 0 && (
            <Paginator
              aria-label="技能分页"
              className="mt-0 mb-[6px]"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </div>
      </div>

      <ClawHubDialog
        open={clawhubModalOpen}
        loading={clawhubLoading}
        source={clawhubSource}
        onSourceChange={setClawhubSource}
        onClose={cancelClawHubImport}
        onSubmit={() => void importClawHubSource()}
      />

      <ResourceImportDialog
        open={agentImportOpen}
        loading={agentImportLoading}
        progress={agentImportProgress}
        icon={<IconSkill className="size-[14px] shrink-0" />}
        title={agentImportMode === 'plaza' ? '从广场复制技能' : '从数字员工复制技能'}
        sourcePlaceholder={agentImportMode === 'plaza' ? '选择开放广场' : '选择复制来源'}
        sources={agentImportMode === 'plaza'
          ? openGalleryImportSourceOptions(agentImportAgents, '开放广场')
          : visibleEmployeeAgents(agentImportAgents, currentUser, { activeOnly: true, excludeAgentId: agentId })
            .map((item) => ({ value: item.id, label: item.name }))}
        sourceId={agentImportSourceAgentId}
        itemsLabel="选择技能"
        items={agentImportSourceSkills.map((item) => ({
          id: item.id,
          disabled: Boolean((item as { __is_bound?: boolean }).__is_bound),
          disabledHint: '已添加到当前员工',
          label: (
            <>
              {item.name}
              <span className="text-[#858b9c]"> · {item.slug}</span>
            </>
          ),
        }))}
        selectedIds={agentImportSelectedSkillIds}
        emptyText="没有可复制的技能"
        note={
          agentImportMode === 'plaza'
            ? '从开放广场复制可用技能；不可复制内容不会出现在列表。'
            : '从数字员工复制可用技能；不可见内容不会出现在列表。'
        }
        onSourceChange={(value) => {
          setAgentImportSourceAgentId(value);
          void loadAgentImportSourceSkills(value);
        }}
        onSelectedChange={setAgentImportSelectedSkillIds}
        onClose={() => setAgentImportOpen(false)}
        onSubmit={() => void submitAgentImportSkills()}
      />

      <MissingReferencesConfirm
        prompt={missingRefsPrompt}
        onCancel={() => setMissingRefsPrompt(null)}
        onConfirm={confirmMissingReferences}
      />

      <SkillShareDialog
        skill={shareDialogRow}
        open={Boolean(shareDialogRow)}
        onClose={() => setShareDialogRow(null)}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        loading={deleting}
        title={deleteTarget ? `${isOverallAgent ? '删除' : '移除'}技能「${deleteTarget.name}」？` : ''}
        description={
          isOverallAgent
            ? '删除后该技能不会再出现在技能广场中，此操作不可撤销。'
            : '这只会在当前数字员工中隐藏该技能；开放广场和其他数字员工仍然保留。'
        }
        confirmText={isOverallAgent ? '删除' : '移除'}
        onConfirm={() => void confirmDeleteSkill()}
      />

      {transfer && (
        <div aria-live="polite" className="fixed bottom-[24px] right-[24px] z-50 w-[300px] rounded-[14px] border border-[#e4e9f2] bg-white/95 p-[14px] shadow-[0_8px_24px_rgba(0,0,0,0.12)]">
          <p className="mb-[8px] flex items-center justify-between gap-[8px] text-[12px] text-[#464C5E]">
            <span className="truncate">{transfer.label}</span>
            <span className="tabular-nums text-[#757F9C]">
              {transfer.percent !== null
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
                // 无 Content-Length 时宽度未知：用流动条纹表达“进行中”
                transfer.percent === null && 'w-1/3 animate-pulse',
              )}
              style={transfer.percent !== null ? { width: `${transfer.percent}%` } : undefined}
            />
          </div>
        </div>
      )}
    </div>
  );
}

type MissingReferencesPrompt = {
  /** SKILL.md 引用了、但技能包里没有的文件路径。 */
  paths: string[];
  /** 用户确认「仍要导入」后重放的导入动作（带 allow_missing_references=true）。 */
  retry: () => void | Promise<void>;
};

const MISSING_REFERENCES_PREVIEW_LIMIT = 20;

/**
 * 「技能包缺少 SKILL.md 引用的文件」二次确认。
 *
 * 后端默认对缺引用直接 400，这里把真实的缺失清单摊开给用户看，确认后带
 * `allow_missing_references=true` 重放导入——既不静默放过残包，也不把用户卡死。
 */
function MissingReferencesConfirm({
  prompt,
  onCancel,
  onConfirm,
}: {
  prompt: MissingReferencesPrompt | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const paths = prompt?.paths || [];
  const preview = paths.slice(0, MISSING_REFERENCES_PREVIEW_LIMIT);
  return (
    <ConfirmDialog
      open={Boolean(prompt)}
      onOpenChange={(open) => !open && onCancel()}
      destructive={false}
      title={`技能包缺少 ${paths.length} 个 SKILL.md 引用的文件`}
      confirmText="仍要导入"
      description={
        <div className="flex flex-col gap-[8px]">
          <span>这些文件不在技能包里，导入后按需读取时可能读不到。确认后仍可导入。</span>
          <ul className="m-0 flex max-h-[168px] list-none flex-col gap-[2px] overflow-auto rounded-[8px] bg-[#f4f5f8] p-[8px] font-mono text-[12px] leading-[18px] text-[#464c5e]">
            {preview.map((path) => (
              <li key={path} className="truncate" title={path}>{path}</li>
            ))}
          </ul>
          {paths.length > preview.length && (
            <span>另有 {paths.length - preview.length} 个文件未列出。</span>
          )}
        </div>
      }
      onConfirm={onConfirm}
    />
  );
}

function ClawHubDialog({
  open,
  loading,
  source,
  onSourceChange,
  onClose,
  onSubmit,
}: {
  open: boolean;
  loading: boolean;
  source: string;
  onSourceChange: (value: string) => void;
  onClose: () => void;
  onSubmit: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[560px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconSkill className="size-[14px] shrink-0" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            从开源平台导入技能
          </DialogTitle>
        </div>

        <div className="flex flex-col gap-[12px] px-[12px]">
          <p className="text-[12px] leading-[1.6] text-[#858b9c]">
            支持开源平台地址、GitHub repo/tree/raw SKILL.md 或 owner/repo 形式。本地 zip 或 Markdown 文件请在编辑页使用「导入 &gt; 选择文件」。
          </p>
          <input
            autoComplete="off"
            data-1p-ignore="true"
            data-lpignore="true"
            data-bwignore="true"
            value={source}
            onChange={(event) => onSourceChange(event.target.value)}
            placeholder="例如 alchaincyf/nuwa-skill 或 https://github.com/owner/repo/tree/main/skill"
            className="h-[34px] w-full rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#17191f] outline-none transition-colors placeholder:text-[#c0c6d4] focus:border-[#18181a]"
          />
        </div>

        <div className="flex items-center justify-end gap-[8px] px-[12px]">
          <UIButton
            variant="outline"
            disabled={loading}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </UIButton>
          <UIButton
            disabled={loading}
            onClick={onSubmit}
            className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
          >
            新增
          </UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function traceDetail(item: Record<string, unknown>): string {
  return [
    item.rationale,
    item.expected_output,
    item.phase === 'code_finished' ? item.stdout_preview : undefined,
    item.phase === 'code_finished' || item.phase === 'code_timeout' ? item.stderr_preview : undefined,
    item.run_id,
  ]
    .filter((value) => typeof value === 'string' && value.trim())
    .map(String)
    .join('\n');
}

function traceItemCode(item: Record<string, unknown>): string {
  return typeof item.code === 'string' && item.code.trim() ? item.code : '';
}

function resultSucceeded(result: Partial<GeneralSkillRunResponse> | null): boolean {
  if (!result) return false;
  const success = result.structured_result?.success;
  return success !== false && !result.stderr;
}

function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true;
  return error instanceof Error && error.name === 'AbortError';
}

function languageFromFilePath(path?: string): string {
  const extension = (path || '').split('.').pop()?.toLowerCase();
  if (extension === 'py') return 'python';
  if (extension === 'json') return 'json';
  if (extension === 'md' || extension === 'markdown') return 'markdown';
  return 'text';
}

function normalizeSkillFilePath(path: string): string {
  return path.replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+/g, '/').trim();
}

function isValidSkillFilePath(path: string): boolean {
  const parts = path.split('/');
  return Boolean(path) && parts.every((part) => Boolean(part) && part !== '.' && part !== '..');
}

function skillFolderPaths(files: GeneralSkillFile[], explicitDirectories: string[]): string[] {
  const paths = new Set<string>();
  [...explicitDirectories, ...files.map((file) => file.path)]
    .forEach((rawPath) => {
      const parts = normalizeSkillFilePath(rawPath).split('/').filter(Boolean);
      const folderPartCount = explicitDirectories.includes(rawPath) ? parts.length : Math.max(0, parts.length - 1);
      for (let index = 1; index <= folderPartCount; index += 1) {
        paths.add(parts.slice(0, index).join('/'));
      }
    });
  return Array.from(paths).sort((left, right) => left.localeCompare(right, 'zh-CN'));
}

function buildSkillFileTree(files: GeneralSkillFile[], explicitDirectories: string[]): SkillFileTreeNode[] {
  const root: SkillFileTreeNode[] = [];
  const folders = new Map<string, Extract<SkillFileTreeNode, { kind: 'folder' }>>();

  const ensureFolder = (path: string) => {
    const normalized = normalizeSkillFilePath(path);
    const existing = folders.get(normalized);
    if (existing) return existing;
    const parts = normalized.split('/');
    const name = parts.pop() || normalized;
    const parentPath = parts.join('/');
    const node: Extract<SkillFileTreeNode, { kind: 'folder' }> = {
      kind: 'folder',
      name,
      path: normalized,
      children: [],
    };
    (parentPath ? ensureFolder(parentPath).children : root).push(node);
    folders.set(normalized, node);
    return node;
  };

  skillFolderPaths(files, explicitDirectories).forEach(ensureFolder);
  files.forEach((file) => {
    const normalized = normalizeSkillFilePath(file.path);
    const parts = normalized.split('/');
    const name = parts.pop() || normalized;
    const parentPath = parts.join('/');
    const node: SkillFileTreeNode = { kind: 'file', name, path: normalized, file };
    (parentPath ? ensureFolder(parentPath).children : root).push(node);
  });

  const sortNodes = (nodes: SkillFileTreeNode[]) => {
    nodes.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'folder' ? -1 : 1;
      return left.name.localeCompare(right.name, 'zh-CN');
    });
    nodes.forEach((node) => {
      if (node.kind === 'folder') sortNodes(node.children);
    });
  };
  sortNodes(root);
  return root;
}

function mimeTypeFromSkillFilePath(path: string): string {
  const extension = path.split('.').pop()?.toLowerCase();
  if (extension === 'md' || extension === 'markdown') return 'text/markdown';
  if (extension === 'json') return 'application/json';
  if (extension === 'py') return 'text/x-python';
  return 'text/plain';
}

function SkillFileTreeEntry({
  node,
  depth,
  expandedFolders,
  selectedFilePath,
  selectedFolderPath,
  onToggleFolder,
  onSelectFile,
  onCreateEntry,
  onRenameFile,
  onRenameFolder,
  onDeleteFile,
  onDeleteFolder,
}: {
  node: SkillFileTreeNode;
  depth: number;
  expandedFolders: Set<string>;
  selectedFilePath: string;
  selectedFolderPath: string | null;
  onToggleFolder: (path: string) => void;
  onSelectFile: (path: string) => void;
  onCreateEntry: (mode: 'file' | 'folder', parentPath: string) => void;
  onRenameFile: (file: GeneralSkillFile) => void;
  onRenameFolder: (path: string) => void;
  onDeleteFile: (file: GeneralSkillFile) => void;
  onDeleteFolder: (path: string) => void;
}) {
  const paddingLeft = 8 + depth * 14;
  if (node.kind === 'folder') {
    const expanded = expandedFolders.has(node.path);
    return (
      <div role="treeitem" aria-expanded={expanded}>
        <ContextMenu.Root>
          <ContextMenu.Trigger asChild>
            <button
              type="button"
              className={skillFileNodeClass(node.path === selectedFolderPath)}
              style={{ paddingLeft }}
              onClick={() => onToggleFolder(node.path)}
              title={node.path}
            >
              <ChevronRight className={cn('size-[13px] shrink-0 transition-transform', expanded && 'rotate-90')} />
              <IconFolder className="size-[14px] shrink-0" />
              <span className="min-w-0 truncate">{node.name}</span>
            </button>
          </ContextMenu.Trigger>
          <ContextMenu.Portal>
            <ContextMenu.Content className={MENU_CONTENT_CLASS}>
              <ContextMenu.Item className={cn(MENU_ITEM_CLASS, 'flex items-center whitespace-nowrap')} onSelect={() => onCreateEntry('file', node.path)}>
                <FilePlus2 />
                新建文件
              </ContextMenu.Item>
              <ContextMenu.Item className={cn(MENU_ITEM_CLASS, 'flex items-center whitespace-nowrap')} onSelect={() => onCreateEntry('folder', node.path)}>
                <FolderPlus />
                新建文件夹
              </ContextMenu.Item>
              <ContextMenu.Item className={cn(MENU_ITEM_CLASS, 'flex items-center whitespace-nowrap')} onSelect={() => onRenameFolder(node.path)}>
                <EditOutlined />
                重命名
              </ContextMenu.Item>
              <ContextMenu.Item className={cn(MENU_ITEM_DANGER_CLASS, 'flex items-center whitespace-nowrap')} onSelect={() => onDeleteFolder(node.path)}>
                <DeleteOutlined />
                删除
              </ContextMenu.Item>
            </ContextMenu.Content>
          </ContextMenu.Portal>
        </ContextMenu.Root>
        {expanded && (
          <div role="group">
            {node.children.map((child) => (
              <SkillFileTreeEntry
                key={`${child.kind}:${child.path}`}
                node={child}
                depth={depth + 1}
                expandedFolders={expandedFolders}
                selectedFilePath={selectedFilePath}
                selectedFolderPath={selectedFolderPath}
                onToggleFolder={onToggleFolder}
                onSelectFile={onSelectFile}
                onCreateEntry={onCreateEntry}
                onRenameFile={onRenameFile}
                onRenameFolder={onRenameFolder}
                onDeleteFile={onDeleteFile}
                onDeleteFolder={onDeleteFolder}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div role="treeitem">
      <ContextMenu.Root>
        <ContextMenu.Trigger asChild>
          <button
            type="button"
            className={skillFileNodeClass(node.path === selectedFilePath && !selectedFolderPath)}
            style={{ paddingLeft: paddingLeft + 27 }}
            onClick={() => onSelectFile(node.path)}
            onContextMenu={() => onSelectFile(node.path)}
            title={node.path}
          >
            <IconProfileFile className="size-[14px] shrink-0" />
            <span className="min-w-0 truncate">{node.name}</span>
          </button>
        </ContextMenu.Trigger>
        <ContextMenu.Portal>
          <ContextMenu.Content className={MENU_CONTENT_CLASS}>
            <ContextMenu.Item className={cn(MENU_ITEM_CLASS, 'flex items-center whitespace-nowrap')} onSelect={() => onRenameFile(node.file)}>
              <EditOutlined />
              重命名
            </ContextMenu.Item>
            <ContextMenu.Item className={cn(MENU_ITEM_DANGER_CLASS, 'flex items-center whitespace-nowrap')} onSelect={() => onDeleteFile(node.file)}>
              <DeleteOutlined />
              删除
            </ContextMenu.Item>
          </ContextMenu.Content>
        </ContextMenu.Portal>
      </ContextMenu.Root>
    </div>
  );
}

function packagePathFromRaw(value: string): string {
  const normalized = value.replace(/\\/g, '/').replace(/^\/+/, '');
  const parts = normalized.split('/').filter(Boolean);
  return parts.length > 1 ? parts.slice(1).join('/') : normalized;
}

function packagePath(file: File): string {
  return packagePathFromRaw((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name);
}

function readEntryFile(entry: SkillFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

function readDirectoryEntries(entry: SkillDirectoryEntry): Promise<SkillFileSystemEntry[]> {
  const reader = entry.createReader();
  const output: SkillFileSystemEntry[] = [];

  return new Promise((resolve, reject) => {
    const readNext = () => {
      reader.readEntries((entries) => {
        if (!entries.length) {
          resolve(output);
          return;
        }
        output.push(...entries);
        readNext();
      }, reject);
    };
    readNext();
  });
}

async function collectDroppedEntryFiles(entry: SkillFileSystemEntry): Promise<DroppedSkillFile[]> {
  if (entry.isFile) {
    const file = await readEntryFile(entry as SkillFileEntry);
    return [{ file, path: packagePathFromRaw(entry.fullPath || file.name) }];
  }
  if (!entry.isDirectory) return [];
  const entries = await readDirectoryEntries(entry as SkillDirectoryEntry);
  const nested = await Promise.all(entries.map(collectDroppedEntryFiles));
  return nested.flat();
}

function dataTransferEntry(item: DataTransferItem): SkillFileSystemEntry | null {
  const getter = (item as unknown as { webkitGetAsEntry?: () => unknown }).webkitGetAsEntry;
  const entry = getter?.call(item);
  if (!entry || typeof entry !== 'object') return null;
  return entry as SkillFileSystemEntry;
}

async function droppedSkillFiles(dataTransfer: DataTransfer): Promise<DroppedSkillFile[]> {
  const entries = Array.from(dataTransfer.items || [])
    .map(dataTransferEntry)
    .filter((entry): entry is SkillFileSystemEntry => Boolean(entry));
  if (entries.length) {
    const nested = await Promise.all(entries.map(collectDroppedEntryFiles));
    return nested.flat();
  }
  return Array.from(dataTransfer.files || []).map((file) => ({ file, path: packagePath(file) }));
}

function parseMetadata(markdownText: string): Record<string, string> {
  const lines = markdownText.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return {};
  const result: Record<string, string> = {};
  for (let index = 1; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (line === '---') break;
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim();
    const value = line.slice(colon + 1).trim().replace(/^['"]|['"]$/g, '');
    if (key && value) result[key] = value;
  }
  return result;
}

function applyMetadata(
  markdownText: string,
  setters: {
    setSkillName: (value: string) => void;
    setSkillSlug: (value: string) => void;
    setSkillDescription: (value: string) => void;
    setSkillHomepage: (value: string) => void;
  },
) {
  const metadata = parseMetadata(markdownText);
  if (metadata.name || metadata.title) setters.setSkillName(metadata.name || metadata.title);
  if (metadata.slug || metadata.id) setters.setSkillSlug(metadata.slug || metadata.id);
  if (metadata.description || metadata.summary) setters.setSkillDescription(metadata.description || metadata.summary);
  if (metadata.homepage || metadata.url) setters.setSkillHomepage(metadata.homepage || metadata.url);
}

function normalizedSkillFiles(files: GeneralSkillFile[] = []): string {
  return JSON.stringify(
    [...files]
      .map((file) => ({
        path: file.path,
        content: file.content,
        mime_type: file.mime_type || '',
      }))
      .sort((a, b) => a.path.localeCompare(b.path)),
  );
}

function SectionCard({
  className,
  bodyClassName,
  title,
  extra,
  loading,
  children,
  ...rest
}: {
  className?: string;
  bodyClassName?: string;
  title?: ReactNode;
  extra?: ReactNode;
  loading?: boolean;
  children?: ReactNode;
} & Omit<HTMLAttributes<HTMLDivElement>, 'title'>) {
  return (
    <section className={cn(SECTION_CARD_CLASS, 'overflow-hidden', className)} {...rest}>
      {(title || extra) && (
        <div className="flex min-h-[40px] items-center justify-between gap-[12px]">
          <div className={cn('min-w-0', SECTION_CARD_TITLE_CLASS)}>{title}</div>
          {extra ? <div className="shrink-0">{extra}</div> : null}
        </div>
      )}
      <div className={cn('min-h-0 flex-1', bodyClassName)}>
        {loading ? (
          <div className="py-[24px] text-center text-[13px] text-[#858b9c]">加载中…</div>
        ) : (
          children
        )}
      </div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-[6px]">
      <span className={FIELD_LABEL_CLASS}>{label}</span>
      {children}
    </div>
  );
}

function GeneralSkillEditorPage({ mode, currentUser, onLogout }: { mode: 'new' | 'edit' } & GeneralSkillPageProps) {
  const navigate = useNavigate();
  const { slug: routeSlug } = useParams();
  const [editorSearchParams] = useSearchParams();
  const forceGalleryScope = editorSearchParams.get('scope') === 'gallery';
  const [agentScopeLoaded, setAgentScopeLoaded] = useState(false);
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [markdown, setMarkdown] = useState(EMPTY_SKILL_MARKDOWN);
  const [skillName, setSkillName] = useState('');
  const [skillSlug, setSkillSlug] = useState('');
  const [skillDescription, setSkillDescription] = useState('');
  const [skillHomepage, setSkillHomepage] = useState('');
  const [skillCategory, setSkillCategory] = useState('');
  const [capabilityScope, setCapabilityScope] = useState<CapabilityScope>('general');
  const [skillFiles, setSkillFiles] = useState<GeneralSkillFile[]>([
    { path: 'SKILL.md', content: EMPTY_SKILL_MARKDOWN, size: EMPTY_SKILL_MARKDOWN.length, mime_type: 'text/markdown' },
  ]);
  const [skillDirectories, setSkillDirectories] = useState<string[]>([]);
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(() => new Set());
  const [selectedFolderPath, setSelectedFolderPath] = useState<string | null>(null);
  const [selectedSlug, setSelectedSlug] = useState<string>();
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [runResult, setRunResult] = useState<GeneralSkillRunResponse | null>(null);
  const [liveResult, setLiveResult] = useState<Partial<GeneralSkillRunResponse> | null>(null);
  const [resultExpanded, setResultExpanded] = useState(false);
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [selectedRunModelId, setSelectedRunModelId] = useState(
    () => window.localStorage.getItem(`${GENERAL_SKILL_RUN_MODEL_STORAGE_KEY}:${TENANT_ID}`) || '',
  );
  const [loading, setLoading] = useState(false);
  // 两段加载的第二段：完整文件包（MB 级）拉取中，文件树区显示骨架提示
  const [filesLoading, setFilesLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFilePath, setSelectedFilePath] = useState('SKILL.md');
  const [editorScroll, setEditorScroll] = useState({ top: 0, left: 0 });
  const [markdownPreviewOpen, setMarkdownPreviewOpen] = useState(false);
  const [clawhubModalOpen, setClawhubModalOpen] = useState(false);
  const [clawhubSource, setClawhubSource] = useState('');
  const [clawhubLoading, setClawhubLoading] = useState(false);
  // zip / Markdown 包上传进度（0-100），仅在上传期间展示
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  // 双阶段：uploading = 字节上传中（0-95），parsing = 服务端解析中（96-100）
  const [uploadStage, setUploadStage] = useState<'uploading' | 'parsing'>('uploading');
  const [agentImportOpen, setAgentImportOpen] = useState(false);
  const [agentImportMode, setAgentImportMode] = useState<GeneralSkillImportMode>('plaza');
  const [agentImportLoading, setAgentImportLoading] = useState(false);
  const [agentImportProgress, setAgentImportProgress] = useState<{ done: number; total: number; current: string } | null>(null);
  const [agentImportAgents, setAgentImportAgents] = useState<AgentProfileRead[]>([]);
  const [agentImportSourceAgentId, setAgentImportSourceAgentId] = useState('');
  const [agentImportSourceSkills, setAgentImportSourceSkills] = useState<GeneralSkillRead[]>([]);
  const [agentImportSelectedSkillIds, setAgentImportSelectedSkillIds] = useState<string[]>([]);
  const [agentId, setAgentId] = useState(readEmployeeScope);
  const [isOverallAgent, setIsOverallAgent] = useState(true);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [deleteSkillTarget, setDeleteSkillTarget] = useState<GeneralSkillRead | null>(null);
  const [deleteFileTarget, setDeleteFileTarget] = useState<GeneralSkillFile | null>(null);
  const [deleteFolderTarget, setDeleteFolderTarget] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<GeneralSkillFile | null>(null);
  const [renameFolderTarget, setRenameFolderTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [createEntryMode, setCreateEntryMode] = useState<'file' | 'folder' | null>(null);
  const [createEntryValue, setCreateEntryValue] = useState('');
  const [importPrepareOpen, setImportPrepareOpen] = useState(false);
  const [missingRefsPrompt, setMissingRefsPrompt] = useState<MissingReferencesPrompt | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const clawhubAbortRef = useRef<AbortController | null>(null);
  const importPrepareActionRef = useRef<null | (() => void | Promise<void>)>(null);
  const knownFolderPathsRef = useRef<Set<string>>(new Set());

  const selectedSkill = useMemo(
    () => rows.find((row) => row.slug === selectedSlug),
    [rows, selectedSlug],
  );
  const activeResult = runResult || liveResult;
  const selectedFile = useMemo(
    () => skillFiles.find((file) => file.path === selectedFilePath) || skillFiles[0],
    [skillFiles, selectedFilePath],
  );
  const folderPaths = useMemo(
    () => skillFolderPaths(skillFiles, skillDirectories),
    [skillFiles.map((file) => file.path).join('\n'), skillDirectories.join('\n')],
  );
  const skillFileTree = useMemo(
    () => buildSkillFileTree(skillFiles, skillDirectories),
    [skillFiles, skillDirectories],
  );
  const selectedFileLanguage = useMemo(() => languageFromFilePath(selectedFile?.path), [selectedFile?.path]);
  const selectedFileCanPreview = selectedFileLanguage === 'markdown';
  const isNew = mode === 'new';
  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const canManageCurrentScope = isOverallAgent
    ? true
    : currentAgent
      ? canManageEmployeeAgent(currentAgent, currentUser)
      : isEnterpriseAdmin(currentUser);
  // 从开放广场「技能广场」进入（?scope=gallery）时，返回/新建都应回到广场列表，
  // 而不是员工技能列表（enterprise/general-skills）。但 MyCreatedSkillsPanel
  // （数字员工-技能管理）进来时会显式带上 return=employee_skills：那是广场技能
  // 从员工技能 tab 编辑的入口，返回仍应去技能管理 tab，不被 scope 拉去平台广场。
  const explicitReturn = editorSearchParams.get('return');
  const skillsLandingRoute = explicitReturn === 'employee_skills'
    ? '/enterprise/general-skills'
    : forceGalleryScope
      ? '/enterprise/platform/general-skills'
      : '/enterprise/general-skills';
  const pageTitle = isNew ? '新建空白技能' : '编辑技能';
  const pageDescription = isOverallAgent
    ? (isNew
      ? '填写技能定义并编辑 SKILL.md，保存后可在右侧运行测试。'
      : '维护技能广场中的技能定义、文件包和运行测试。')
    : (isNew
      ? '为当前数字员工创建技能，填写基本信息并编辑技能文件。'
      : '维护当前数字员工技能的定义、文件包和运行测试。');

  const loadDetail = () => {
    if (mode !== 'edit' || !routeSlug) return null;
    const detailSuffix = agentId && !isOverallAgent ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    // 两段加载：第一段 include_files=0（元信息 + SKILL.md，KB 级）秒出表单；
    // 第二段完整详情补文件包，文件树区显示「加载文件中…」。大包编辑不再整个白屏等。
    const editorQuery = new URLSearchParams({ tenant_id: TENANT_ID });
    if (agentId && !isOverallAgent) editorQuery.set('agent_id', agentId);
    const metaPromise = api
      .get<GeneralSkillRead>(
        `/api/enterprise/general-skills/${encodeURIComponent(routeSlug)}?${editorQuery.toString()}&include_files=0`,
      )
      .then((meta) => {
        editSkill({ ...meta, skill_files: [], skill_directories: [] });
        setFilesLoading(true);
        // rows 里预填轻行；随后完整行到达后覆盖
        setRows((current) =>
          current.some((item) => item.id === meta.id)
            ? current.map((item) => (item.id === meta.id ? meta : item))
            : [...current, meta],
        );
        return api
          .get<GeneralSkillRead>(
            `/api/enterprise/general-skills/${encodeURIComponent(routeSlug)}?${editorQuery.toString()}`,
          )
          .then((full) => {
            setFilesLoading(false);
            // 只在用户还没动过表单时才用完整行覆盖，避免把用户已编辑的内容冲掉
            if (!hasUnsavedEditingChanges()) {
              editSkill(full);
            }
            setRows((current) =>
              current.some((item) => item.id === full.id)
                ? current.map((item) => (item.id === full.id ? full : item))
                : [...current, full],
            );
          });
      })
      .catch((error) => {
        setFilesLoading(false);
        notify.error(error instanceof Error ? error.message : '加载技能失败');
      });
    return metaPromise;
  };

  const load = () => {
    const agentSuffix = agentId && !isOverallAgent ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    // 列表只要元信息（include_files=0）：文件包单租户 MB 级，编辑详情由 loadDetail 单独拉
    const listPromise = api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentSuffix}&include_files=0`)
      .then((items) => {
        setRows(items);
      })
      .catch((error) => notify.error(error.message));
    const detailPromise = loadDetail();
    return detailPromise || listPromise;
  };

  useEffect(() => {
    if (mode === 'new') newSkill();
  }, [mode]);

  useEffect(() => {
    if (mode === 'new' || !agentScopeLoaded) return;
    void load();
  }, [agentId, mode, routeSlug, forceGalleryScope, agentScopeLoaded]);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setAgents(items);
        if (forceGalleryScope) {
          // 广场态不依赖 overall agent 是否被命中，也不写全局作用域
          setIsOverallAgent(true);
        } else {
          const scopedAgent = items.find((item) => item.id === agentId);
          if (scopedAgent && scopedAgent.id !== agentId) {
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, scopedAgent.id);
            setAgentId(scopedAgent.id);
          }
          setIsOverallAgent(Boolean(scopedAgent?.is_overall ?? true));
        }
        setAgentScopeLoaded(true);
      })
      .catch(() => {
        setIsOverallAgent(true);
        setAgentScopeLoaded(true);
      });
  }, [agentId, forceGalleryScope]);

  useEffect(() => {
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((items) => {
        const enabled = items.filter((item) => item.enabled);
        setModelConfigs(enabled);
        setSelectedRunModelId((current) => {
          if (current && enabled.some((item) => item.id === current)) return current;
          const fallback = enabled.find((item) => item.is_default)?.id || enabled[0]?.id || '';
          if (fallback) {
            window.localStorage.setItem(`${GENERAL_SKILL_RUN_MODEL_STORAGE_KEY}:${TENANT_ID}`, fallback);
          }
          return fallback;
        });
      })
      .catch(() => setModelConfigs([]));
  }, []);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      if (forceGalleryScope) return;
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setAgentId(next && !isTeamScope(next) ? next : readEmployeeScope());
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, [forceGalleryScope]);

  useEffect(() => {
    folderInputRef.current?.setAttribute('webkitdirectory', '');
    folderInputRef.current?.setAttribute('directory', '');
  }, []);

  useEffect(() => {
    if (!skillFiles.length) return;
    if (!skillFiles.some((file) => file.path === selectedFilePath)) {
      const skillFile = skillFiles.find((file) => file.path.split('/').pop()?.toLowerCase() === 'skill.md');
      setSelectedFilePath(skillFile?.path || skillFiles[0].path);
    }
  }, [skillFiles, selectedFilePath]);

  useEffect(() => {
    const currentFolderPaths = new Set(folderPaths);
    const previousFolderPaths = knownFolderPathsRef.current;
    setExpandedFolders((current) => {
      const next = new Set(Array.from(current).filter((path) => currentFolderPaths.has(path)));
      folderPaths.forEach((path) => {
        if (!previousFolderPaths.has(path)) next.add(path);
      });
      return next;
    });
    knownFolderPathsRef.current = currentFolderPaths;
  }, [folderPaths.join('\n')]);

  useEffect(() => {
    if (selectedFolderPath && !folderPaths.includes(selectedFolderPath)) {
      setSelectedFolderPath(null);
    }
  }, [folderPaths.join('\n'), selectedFolderPath]);

  useEffect(() => {
    setEditorScroll({ top: 0, left: 0 });
  }, [selectedFilePath]);

  useEffect(() => {
    if (!selectedFileCanPreview) {
      setMarkdownPreviewOpen(false);
    }
  }, [selectedFileCanPreview]);

  /** 命中「缺参考文件」的 400 时挂起二次确认；返回 true 表示已接管错误提示。 */
  function promptForMissingReferences(error: unknown, retry: () => void | Promise<void>): boolean {
    const paths = missingSkillReferencePaths(error);
    if (!paths.length) return false;
    setMissingRefsPrompt({ paths, retry });
    return true;
  }

  function confirmMissingReferences() {
    const prompt = missingRefsPrompt;
    setMissingRefsPrompt(null);
    if (prompt) void prompt.retry();
  }

  function hasUnsavedEditingChanges(): boolean {
    if (!editingSlug) return false;
    const original = rows.find((row) => row.slug === editingSlug);
    if (!original) return false;
    const stableSlug = editingSlug || skillSlug;
    return (
      markdown !== original.skill_markdown
      || skillName !== original.name
      || stableSlug !== original.slug
      || skillDescription !== (original.description || '')
      || skillCategory !== (original.category || '')
      || skillHomepage !== (original.homepage || '')
      || capabilityScope !== normalizeCapabilityScope(original.capability_scope)
      || normalizedSkillFiles(skillFiles) !== normalizedSkillFiles(
        original.skill_files?.length ? original.skill_files : [{ path: 'SKILL.md', content: original.skill_markdown }],
      )
      || [...skillDirectories].sort().join('\n') !== [...(original.skill_directories || [])].sort().join('\n')
    );
  }

  /**
   * 保存技能（新建 / 编辑共用）。这不是「导入外部技能包」，而是「保存当前编辑器里的内容」：
   * 用户可以先把 SKILL.md 写好、再逐个补齐它引用的参考文件（如 references/products/aiapp.md），
   * 所以保存一律带 allow_missing_references，**不**弹「技能包缺少 SKILL.md 引用文件」的二次确认。
   * 那个拦截只属于「导入整包」的动作：上传 zip（/import-package）和从开源平台拉取（/import-skillhub）。
   */
  async function importSkill(
    options: { stayInEditor?: boolean } = {},
  ): Promise<GeneralSkillRead | null> {
    // 广场作用域：新建技能全员可发布；编辑已有技能仅创建者或管理员
    if (isOverallAgent) {
      if (editingSlug) {
        const existing = rows.find((item) => item.slug === editingSlug);
        if (!(isEnterpriseAdmin(currentUser) || existing?.metadata?.owner_user_id === currentUser?.id)) {
          notify.error('只有创建者或管理员可以编辑该技能');
          return null;
        }
      }
    } else if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return null;
    }
    if (!markdown.trim()) {
      notify.warning('请先粘贴或上传 SKILL.md');
      return null;
    }
    // 「新建」还是「编辑既有技能」在进入函数时就定下来：保存成功后要据此决定落点。
    const isCreating = !editingSlug;
    setSaving(true);
    try {
      const row = await api.post<GeneralSkillRead>('/api/enterprise/general-skills/import', {
        tenant_id: TENANT_ID,
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        name: skillName.trim() || undefined,
        slug: editingSlug || skillSlug.trim() || undefined,
        description: skillDescription.trim() || undefined,
        // 总是显式上送：空串 = 清空分类，与后端「None 表示不涉及分类」区分开
        category: skillCategory.trim(),
        homepage: skillHomepage.trim() || undefined,
        capability_scope: capabilityScope,
        markdown,
        files: skillFiles.length ? skillFiles : [{ path: 'SKILL.md', content: markdown }],
        directories: skillDirectories,
        status: 'published',
        original_slug: editingSlug || undefined,
        // 保存不做缺引用校验：参考文件允许在编辑器里随后补齐（见 importSkill 的说明）
        allow_missing_references: true,
      });
      notify.success(editingSlug ? `已保存 ${row.name}` : `已新增 ${row.name}`);
      setSelectedSlug(row.slug);
      setEditingSlug(row.slug);
      setMarkdown(row.skill_markdown);
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setSkillCategory(row.category || '');
      setCapabilityScope(normalizeCapabilityScope(row.capability_scope));
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSkillDirectories(row.skill_directories || []);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
      setSelectedFolderPath(null);
      setRows((current) => {
        const withoutSaved = current.filter((item) => item.id !== row.id && item.slug !== row.slug);
        return [row, ...withoutSaved];
      });
      const scopeQuery = row.metadata?.scope === 'open_gallery' ? '?scope=gallery' : '';
      if (isCreating && !options.stayInEditor) {
        // 创建完成回技能广场列表，而不是停在新技能的编辑页（replace 掉 /new，避免返回又回到空表单）
        navigate(skillsLandingRoute, { replace: true });
        return row;
      }
      navigate(`/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit${scopeQuery}`, { replace: false });
      return row;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存技能失败');
      return null;
    } finally {
      setSaving(false);
    }
  }

  function newSkill() {
    setMarkdown(EMPTY_SKILL_MARKDOWN);
    setSkillName('');
    setSkillSlug('');
    setSkillDescription('');
    setSkillHomepage('');
    setSkillCategory('');
    setCapabilityScope('general');
    setSkillFiles([{ path: 'SKILL.md', content: EMPTY_SKILL_MARKDOWN, size: EMPTY_SKILL_MARKDOWN.length, mime_type: 'text/markdown' }]);
    setSkillDirectories([]);
    setSelectedFilePath('SKILL.md');
    setSelectedFolderPath(null);
    setEditingSlug(null);
    setSelectedSlug(undefined);
    setQuery('');
    setRunResult(null);
    setLiveResult(null);
    setResultExpanded(false);
    setMarkdownPreviewOpen(false);
  }

  function editSkill(row: GeneralSkillRead) {
    setMarkdown(row.skill_markdown);
    setSkillName(row.name);
    setSkillSlug(row.slug);
    setSkillDescription(row.description || '');
    setSkillHomepage(row.homepage || '');
    setSkillCategory(row.category || '');
    setCapabilityScope(normalizeCapabilityScope(row.capability_scope));
    setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
    setSkillDirectories(row.skill_directories || []);
    setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
    setSelectedFolderPath(null);
    setSelectedSlug(row.slug);
    setEditingSlug(row.slug);
    setRunResult(null);
    setLiveResult(null);
    setResultExpanded(false);
    setMarkdownPreviewOpen(false);
  }

  function replaceRow(row: GeneralSkillRead) {
    setRows((current) => current.map((item) => (item.id === row.id ? row : item)));
    if (editingSlug === row.slug) {
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setSkillCategory(row.category || '');
      setCapabilityScope(normalizeCapabilityScope(row.capability_scope));
      setMarkdown(row.skill_markdown);
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSkillDirectories(row.skill_directories || []);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
      setSelectedFolderPath(null);
    }
  }

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    if (isOverallAgent) {
      if (!(isEnterpriseAdmin(currentUser) || row.metadata?.owner_user_id === currentUser?.id)) {
        notify.error('只有创建者或管理员可以编辑该技能');
        return;
      }
    } else if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return;
    }
    try {
      const agentSuffix = agentId && !isOverallAgent ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${row.slug}/${published ? 'publish' : 'archive'}?tenant_id=${TENANT_ID}${agentSuffix}`,
      );
      replaceRow(next);
      notify.success(published ? '已启用技能' : '已停用技能');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : published ? '发布失败' : '下线失败');
    }
  }

  async function runDeleteSkill() {
    const row = deleteSkillTarget;
    if (!row) return;
    if (isOverallAgent) {
      if (!(isEnterpriseAdmin(currentUser) || row.metadata?.owner_user_id === currentUser?.id)) {
        notify.error('只有创建者或管理员可以编辑该技能');
        return;
      }
    } else if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return;
    }
    const branchMode = !isOverallAgent;
    try {
      const agentSuffix = agentId && !isOverallAgent ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      await api.delete(`/api/enterprise/general-skills/${row.slug}?tenant_id=${TENANT_ID}${agentSuffix}`);
      const nextRows = rows.filter((item) => item.id !== row.id);
      setRows(nextRows);
      if (selectedSlug === row.slug || editingSlug === row.slug) {
        const next = nextRows[0];
        if (next) {
          setSelectedSlug(next.slug);
          editSkill(next);
        } else {
          setSelectedSlug(undefined);
          newSkill();
        }
      }
      notify.success(branchMode ? '已移除技能' : '已删除技能');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleteSkillTarget(null);
    }
  }

  function startImportedDraft() {
    setEditingSlug(null);
    setSelectedSlug(undefined);
    setRunResult(null);
    setLiveResult(null);
    setResultExpanded(false);
    setMarkdownPreviewOpen(false);
  }

  async function withImportPreparation(importAction: () => void | Promise<void>) {
    if (!hasUnsavedEditingChanges()) {
      await importAction();
      return;
    }
    importPrepareActionRef.current = importAction;
    setImportPrepareOpen(true);
  }

  async function confirmImportPrepareSave() {
    const action = importPrepareActionRef.current;
    setImportPrepareOpen(false);
    // 这一支是「先保存再继续导入」，保存后必须留在编辑器里接续动作，不能回列表
    const saved = await importSkill({ stayInEditor: true });
    if (saved && action) await action();
    importPrepareActionRef.current = null;
  }

  async function confirmImportPrepareSkip() {
    const action = importPrepareActionRef.current;
    setImportPrepareOpen(false);
    importPrepareActionRef.current = null;
    if (action) await action();
  }

  function requestImport(kind: 'file' | 'folder') {
    void withImportPreparation(() => {
      if (kind === 'folder') {
        folderInputRef.current?.click();
        return;
      }
      fileInputRef.current?.click();
    });
  }

  function requestClawHubImport() {
    void withImportPreparation(() => {
      clawhubAbortRef.current?.abort();
      clawhubAbortRef.current = null;
      setClawhubLoading(false);
      setClawhubSource('');
      setClawhubModalOpen(true);
    });
  }

  function cancelClawHubImport() {
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = null;
    setClawhubLoading(false);
    setClawhubModalOpen(false);
  }

  function requestAgentImport(mode: GeneralSkillImportMode) {
    void withImportPreparation(async () => {
      try {
        const agents = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
        const firstSource = mode === 'plaza'
          ? openGalleryAgentId(agents)
          : visibleEmployeeAgents(agents, currentUser, { activeOnly: true, excludeAgentId: agentId })[0]?.id || '';
        setAgentImportMode(mode);
        setAgentImportAgents(agents);
        setAgentImportSourceAgentId(firstSource);
        setAgentImportSelectedSkillIds([]);
        setAgentImportOpen(true);
        if (firstSource) {
          await loadAgentImportSourceSkills(firstSource);
        } else {
          setAgentImportSourceSkills([]);
        }
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '加载员工列表失败');
      }
    });
  }

  async function loadAgentImportSourceSkills(sourceAgentId: string) {
    setAgentImportSourceSkills([]);
    setAgentImportSelectedSkillIds([]);
    if (!sourceAgentId) return;
    try {
      const sourceRows = await api.get<GeneralSkillRead[]>(
        `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(sourceAgentId)}&include_files=0`,
      );
      const existingIds = new Set(rows.map((item) => item.id));
      // 已绑到当前员工的技能也要展示（disabled + 「已添加」标记）：全部静默过滤掉会
      // 让「从广场复制」弹窗明明有技能力却一片空白，看起来像没有可用技能。
      setAgentImportSourceSkills(sourceRows
        .filter((item) => item.status === 'published')
        .map((item) => ({ ...item, __is_bound: existingIds.has(item.id) })));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载来源技能失败');
    }
  }

  async function submitAgentImportSkills() {
    if (!agentId) {
      notify.warning('请先选择一个数字员工');
      return;
    }
    if (!agentImportSourceAgentId) {
      notify.warning(agentImportMode === 'plaza' ? '请选择开放广场' : '请选择复制来源');
      return;
    }
    if (!agentImportSelectedSkillIds.length) {
      notify.warning('请选择要复制的技能');
      return;
    }
    setAgentImportLoading(true);
    try {
      // 大包复制服务端耗时与单个技能包体积成正比，一批一 POST 无法展示真实进度。
      // 改为逐个提交：每完成 1 个推进一格，进度是真实的；失败项不中断整批。
      const done: string[] = [];
      const failed: string[] = [];
      for (const [index, resourceId] of agentImportSelectedSkillIds.entries()) {
        const sourceSkill = agentImportSourceSkills.find((item) => item.id === resourceId);
        setAgentImportProgress({ done: index, total: agentImportSelectedSkillIds.length, current: sourceSkill?.name || '' });
        try {
          await api.post(`/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/import`, {
            tenant_id: TENANT_ID,
            source_agent_id: agentImportSourceAgentId,
            resource_type: 'general_skill',
            resource_ids: [resourceId],
          });
          done.push(resourceId);
        } catch (error) {
          console.warn('复制技能失败', sourceSkill?.name || resourceId, error);
          failed.push(sourceSkill?.name || resourceId);
        }
      }
      setAgentImportProgress({ done: agentImportSelectedSkillIds.length, total: agentImportSelectedSkillIds.length, current: '' });
      if (failed.length) {
        notify.error(`已复制 ${agentImportSelectedSkillIds.length - failed.length} 个，失败 ${failed.length} 个：${failed.slice(0, 5).join('、')}${failed.length > 5 ? '…' : ''}`);
      } else {
        notify.success(`已复制 ${done.length} 个技能`);
      }
      setAgentImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制技能失败');
    } finally {
      setAgentImportLoading(false);
      setAgentImportProgress(null);
    }
  }

  async function importClawHubSource(allowMissingReferences = false) {
    if (!clawhubSource.trim()) {
      notify.warning('请输入开源平台地址、GitHub 仓库或 SKILL.md 链接');
      return;
    }
    const controller = new AbortController();
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = controller;
    setClawhubLoading(true);
    try {
      const row = await api.postWithSignal<GeneralSkillRead>('/api/enterprise/general-skills/import-skillhub', {
        tenant_id: TENANT_ID,
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        source: clawhubSource.trim(),
        status: 'published',
        allow_missing_references: allowMissingReferences,
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已新增 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setClawhubModalOpen(false);
      // 从开源平台导入＝创建新技能，导入完成回技能广场列表看结果
      navigate(skillsLandingRoute, { replace: true });
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
      if (promptForMissingReferences(error, () => importClawHubSource(true))) return;
      notify.error(error instanceof Error ? error.message : '从开源平台导入失败');
    } finally {
      if (clawhubAbortRef.current === controller) {
        clawhubAbortRef.current = null;
        setClawhubLoading(false);
      }
    }
  }

  async function importSkillPackageFile(file: File, allowMissingReferences = false) {
    // zip / Markdown 导入不再直接调 /import-package 落库保存：先走 /preview 解析，
    // 把解析出的元信息与文件树填进编辑器表单，用户确认内容、补齐引用后再手动保存。
    // 只有无缺失引用时直接填表；缺引用时保持旧弹窗确认，确认后仍填表不落库。
    // 大包走 multipart 直传（/preview-multipart）：省 27MB 级 base64-JSON 编解码，
    // 字节上传有真实进度；双阶段语义见 uploadWithProgress（parsing = 服务端解析中）。
    const controller = new AbortController();
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = controller;
    setClawhubLoading(true);
    setUploadStage('uploading');
    setUploadPercent(0);
    try {
      const agentSuffix = !isOverallAgent && agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const payload = await api.uploadWithProgress<{
        filename: string;
        name?: string | null;
        slug?: string | null;
        description?: string | null;
        homepage?: string | null;
        markdown: string;
        files: { path: string; content: string; size?: number | null; mime_type?: string | null }[];
        directories: string[];
      }>(
        '/api/enterprise/general-skills/import-package/preview-multipart',
        file,
        `tenant_id=${TENANT_ID}${agentSuffix}`,
        (percent, stage) => {
          setUploadStage(stage);
          setUploadPercent(percent);
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      startImportedDraft();
      setSkillFiles(payload.files.map((item) => ({
        path: item.path,
        content: item.content,
        size: item.size ?? item.content.length,
        mime_type: item.mime_type || undefined,
      })));
      setSkillDirectories(payload.directories || []);
      setMarkdownPreviewOpen(false);
      setSkillName(payload.name || '');
      // slug 只作为表单预填（保存时后端仍会做唯一性兜底），保持与手动新建一致
      setSkillSlug(payload.slug || '');
      setSkillDescription(payload.description || '');
      setSkillHomepage(payload.homepage || '');
      setMarkdown(payload.markdown);
      const skillFilePath = payload.files.find((item) => item.path.split('/').pop()?.toLowerCase() === 'skill.md')?.path;
      setSelectedFilePath(skillFilePath || payload.files[0]?.path || 'SKILL.md');
      setSelectedFolderPath(null);
      notify.success(`已读取 ${payload.files.length} 个文件，可继续编辑后保存`);
      setClawhubModalOpen(false);
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
      if (promptForMissingReferences(error, () => importSkillPackageFile(file, true))) return;
      notify.error(error instanceof Error ? error.message : '上传技能包失败');
    } finally {
      if (clawhubAbortRef.current === controller) {
        clawhubAbortRef.current = null;
        setClawhubLoading(false);
      }
    }
  }

  function updateSelectedFile(text: string) {
    if (!selectedFile) return;
    setSkillFiles((current) => current.map((file) => (
      file.path === selectedFile.path
        ? { ...file, content: text, size: text.length }
        : file
    )));
    if (selectedFile.path.split('/').pop()?.toLowerCase() === 'skill.md') {
      setMarkdown(text);
    }
  }

  function parentDirectory(path: string): string {
    const parts = normalizeSkillFilePath(path).split('/');
    parts.pop();
    return parts.join('/');
  }

  function nextAvailableEntryPath(parentPath: string, mode: 'file' | 'folder'): string {
    const baseName = mode === 'file' ? 'notes.md' : '新建文件夹';
    let candidateName = baseName;
    let index = 2;
    const occupied = new Set([...skillFiles.map((file) => file.path), ...folderPaths]);
    let candidate = parentPath ? `${parentPath}/${candidateName}` : candidateName;
    while (occupied.has(candidate)) {
      candidateName = mode === 'file' ? `notes-${index}.md` : `新建文件夹-${index}`;
      candidate = parentPath ? `${parentPath}/${candidateName}` : candidateName;
      index += 1;
    }
    return candidate;
  }

  function openCreateEntry(mode: 'file' | 'folder', parentPath?: string) {
    const resolvedParent = parentPath ?? selectedFolderPath ?? parentDirectory(selectedFilePath);
    setCreateEntryMode(mode);
    setCreateEntryValue(nextAvailableEntryPath(resolvedParent, mode));
  }

  function pathHasFileAncestor(path: string, ignoredFilePath?: string): boolean {
    const parts = path.split('/');
    return parts.slice(0, -1).some((_, index) => {
      const parent = parts.slice(0, index + 1).join('/');
      return parent !== ignoredFilePath && skillFiles.some((file) => file.path === parent);
    });
  }

  function runCreateEntry() {
    const mode = createEntryMode;
    if (!mode) return;
    const normalized = normalizeSkillFilePath(createEntryValue);
    if (!isValidSkillFilePath(normalized)) {
      notify.error(mode === 'file' ? '文件名不能为空或包含无效路径' : '文件夹名称不能为空或包含无效路径');
      return;
    }
    if (pathHasFileAncestor(normalized)) {
      notify.error('上级路径中存在同名文件');
      return;
    }
    if (skillFiles.some((file) => file.path === normalized) || folderPaths.includes(normalized)) {
      notify.error(mode === 'file' ? '已存在同名文件或文件夹' : '已存在同名文件夹或文件');
      return;
    }
    if (mode === 'folder') {
      setSkillDirectories((current) => [...current, normalized]);
      setSelectedFolderPath(normalized);
      setExpandedFolders((current) => new Set(current).add(normalized));
    } else {
      setSkillFiles((current) => [
        ...current,
        { path: normalized, content: '', size: 0, mime_type: mimeTypeFromSkillFilePath(normalized) },
      ]);
      setSelectedFilePath(normalized);
      setSelectedFolderPath(null);
    }
    setCreateEntryMode(null);
  }

  function deleteSelectedEntry() {
    if (selectedFolderPath) {
      deleteSkillFolder(selectedFolderPath);
      return;
    }
    if (selectedFile) deleteSkillFile(selectedFile);
  }

  function deleteSkillFile(target: GeneralSkillFile) {
    if (target.path.split('/').pop()?.toLowerCase() === 'skill.md') {
      notify.warning('SKILL.md 是技能入口，不能删除');
      return;
    }
    setDeleteFileTarget(target);
  }

  function runDeleteFile() {
    const target = deleteFileTarget;
    if (!target) return;
    setSkillFiles((current) => current.filter((file) => file.path !== target.path));
    setDeleteFileTarget(null);
  }

  function deleteSkillFolder(path: string) {
    const prefix = `${path}/`;
    if (skillFiles.some((file) => file.path.startsWith(prefix) && file.path.split('/').pop()?.toLowerCase() === 'skill.md')) {
      notify.warning('包含 SKILL.md 的文件夹不能删除');
      return;
    }
    setDeleteFolderTarget(path);
  }

  function runDeleteFolder() {
    const target = deleteFolderTarget;
    if (!target) return;
    const prefix = `${target}/`;
    setSkillFiles((current) => current.filter((file) => !file.path.startsWith(prefix)));
    setSkillDirectories((current) => current.filter((path) => path !== target && !path.startsWith(prefix)));
    setExpandedFolders((current) => new Set(Array.from(current).filter((path) => path !== target && !path.startsWith(prefix))));
    setSelectedFolderPath(null);
    setDeleteFolderTarget(null);
  }

  function renameSkillFile(target: GeneralSkillFile) {
    if (target.path.split('/').pop()?.toLowerCase() === 'skill.md') {
      notify.warning('SKILL.md 是技能入口，不能重命名');
      return;
    }
    setRenameTarget(target);
    setRenameFolderTarget(null);
    setRenameValue(target.path);
  }

  function renameSkillFolder(path: string) {
    setRenameTarget(null);
    setRenameFolderTarget(path);
    setRenameValue(path);
  }

  function runRenameFile() {
    const target = renameTarget;
    if (!target) return;
    {
      const nextPath = renameValue;
      {
        const normalized = normalizeSkillFilePath(nextPath);
        if (!isValidSkillFilePath(normalized)) {
          notify.error('文件名不能为空或包含无效路径');
          return;
        }
        if (normalized === target.path) {
          setRenameTarget(null);
          return;
        }
        if (skillFiles.some((file) => file.path === normalized) || folderPaths.includes(normalized)) {
          notify.error('已存在同名文件或文件夹');
          return;
        }
        if (pathHasFileAncestor(normalized, target.path)) {
          notify.error('上级路径中存在同名文件');
          return;
        }
        setSkillFiles((current) => current.map((file) => (
          file.path === target.path
            ? { ...file, path: normalized }
            : file
        )));
        if (selectedFilePath === target.path) {
          setSelectedFilePath(normalized);
        }
        setRenameTarget(null);
      }
    }
  }

  function runRenameFolder() {
    const target = renameFolderTarget;
    if (!target) return;
    const normalized = normalizeSkillFilePath(renameValue);
    if (!isValidSkillFilePath(normalized)) {
      notify.error('文件夹名称不能为空或包含无效路径');
      return;
    }
    if (normalized === target) {
      setRenameFolderTarget(null);
      return;
    }
    if (normalized.startsWith(`${target}/`)) {
      notify.error('文件夹不能移动到自身内部');
      return;
    }
    if (
      skillFiles.some((file) => file.path === normalized)
      || folderPaths.some((path) => path === normalized && path !== target)
      || pathHasFileAncestor(normalized)
    ) {
      notify.error('目标位置已存在同名文件或文件夹');
      return;
    }
    const prefix = `${target}/`;
    const replacePrefix = (path: string) => (
      path === target ? normalized : path.startsWith(prefix) ? `${normalized}/${path.slice(prefix.length)}` : path
    );
    setSkillFiles((current) => current.map((file) => ({ ...file, path: replacePrefix(file.path) })));
    setSkillDirectories((current) => Array.from(new Set([
      ...current.map(replacePrefix),
      normalized,
    ])));
    setExpandedFolders((current) => new Set(Array.from(current).map(replacePrefix)));
    if (selectedFilePath.startsWith(prefix)) setSelectedFilePath(replacePrefix(selectedFilePath));
    setSelectedFolderPath(normalized);
    setRenameFolderTarget(null);
  }

  async function runSkill() {
    const slug = selectedSkill?.slug;
    if (!slug) {
      notify.warning('请先导入技能');
      return;
    }
    if (!query.trim()) {
      notify.warning('请输入测试问题');
      return;
    }
    setResultExpanded(true);
    setLoading(true);
    setRunResult(null);
    setLiveResult({
      skill_slug: slug,
      execution_trace: [],
      generated_code: '',
      stdout: '',
      stderr: '',
      structured_result: {},
      reply: '',
    });
    const controller = new AbortController();
    let timedOut = false;
    let debugSessionId = '';
    let debugTurnId = '';
    const receivedTrace: Record<string, unknown>[] = [];
    let timeoutId = 0;
    const resetIdleTimeout = () => {
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(() => {
        timedOut = true;
        if (debugSessionId && debugTurnId) {
          void api.post(`/api/chat/sessions/${debugSessionId}/cancel`, {
            tenant_id: TENANT_ID,
            turn_id: debugTurnId,
          }).catch(() => undefined);
        }
        controller.abort();
      }, GENERAL_SKILL_RUN_IDLE_TIMEOUT_MS);
    };
    resetIdleTimeout();
    try {
      let completed = false;
      await streamPost(
        `/api/enterprise/general-skills/${slug}/run/stream`,
        {
          tenant_id: TENANT_ID,
          agent_id: !isOverallAgent && agentId ? agentId : undefined,
          user_id: 'enterprise_demo',
          query,
          model_config_id: selectedRunModelId || undefined,
          max_attempts: 10,
        },
        (item) => {
          resetIdleTimeout();
          if (item.event === 'stream_started') {
            debugSessionId = typeof item.data.session_id === 'string' ? item.data.session_id : '';
            debugTurnId = typeof item.data.client_turn_id === 'string' ? item.data.client_turn_id : '';
          }
          if (item.event === 'trace') {
            const traceItem = item.data;
            receivedTrace.push(traceItem);
            setLiveResult((current) => {
              const previous = current || { skill_slug: slug, execution_trace: [] };
              const executionTrace = [...(previous.execution_trace || []), traceItem];
              const nextCode = typeof traceItem.code === 'string' && traceItem.code.trim()
                ? traceItem.code
                : previous.generated_code || '';
              const nextStructured = typeof traceItem.structured_result === 'object' && traceItem.structured_result
                ? traceItem.structured_result as Record<string, unknown>
                : previous.structured_result || {};
              const chunk = typeof traceItem.text === 'string' ? traceItem.text : '';
              const phase = typeof traceItem.phase === 'string' ? traceItem.phase : '';
              return {
                ...previous,
                execution_trace: executionTrace,
                generated_code: nextCode,
                stdout: phase === 'stdout_chunk'
                  ? `${previous.stdout || ''}${chunk}`
                  : typeof traceItem.stdout_preview === 'string' ? traceItem.stdout_preview : previous.stdout || '',
                stderr: phase === 'stderr_chunk'
                  ? `${previous.stderr || ''}${chunk}`
                  : typeof traceItem.stderr_preview === 'string' ? traceItem.stderr_preview : previous.stderr || '',
                structured_result: nextStructured,
              };
            });
          }
          if (item.event === 'complete') {
            const result = item.data as unknown as GeneralSkillRunResponse;
            completed = true;
            setRunResult({
              ...result,
              execution_trace: result.execution_trace?.length
                ? result.execution_trace
                : receivedTrace,
            });
            setLiveResult(null);
            notify.success('运行完成');
          }
          if (item.event === 'error') {
            const text = typeof item.data.message === 'string' ? item.data.message : '运行失败';
            completed = true;
            setLiveResult((current) => ({
              ...(current || { skill_slug: slug, execution_trace: [] }),
              stderr: text,
              structured_result: { success: false, error: text },
              reply: '运行失败',
            }));
            notify.error(text);
          }
        },
        controller.signal,
      );
      if (!completed) {
        notify.warning('运行流已结束，但未收到最终结果');
      }
    } catch (error) {
      const text = timedOut
        ? '技能测试 10 分钟内未收到新的执行事件，请检查模型或稍后重试。'
        : error instanceof Error ? error.message : '运行失败';
      setLiveResult((current) => ({
        ...(current || { skill_slug: slug, execution_trace: [] }),
        stderr: text,
        structured_result: { success: false, error: text },
        reply: '运行失败',
      }));
      notify.error(text);
    } finally {
      window.clearTimeout(timeoutId);
      setLoading(false);
    }
  }

  async function importSingleFile(target: File) {
    const text = await target.text();
    const nextFile = { path: 'SKILL.md', content: text, size: target.size, mime_type: target.type || 'text/markdown' };
    startImportedDraft();
    setSkillFiles([nextFile]);
    setSkillDirectories([]);
    setSelectedFilePath('SKILL.md');
    setSelectedFolderPath(null);
    setMarkdown(text);
    setMarkdownPreviewOpen(false);
    applyMetadata(text, { setSkillName, setSkillSlug, setSkillDescription, setSkillHomepage });
    notify.success(`已读取 ${target.name}`);
  }

  async function importSkillPackage(targets: DroppedSkillFile[]) {
    if (!targets.length) return;
    const nextFiles: GeneralSkillFile[] = [];
    let failedCount = 0;
    for (const { file, path } of targets) {
      try {
        const text = await file.text();
        nextFiles.push({
          path,
          content: text,
          size: file.size,
          mime_type: file.type || undefined,
        });
      } catch {
        failedCount += 1;
      }
    }
    if (!nextFiles.length) {
      notify.error('没有读取到可导入的技能文件');
      return;
    }
    nextFiles.sort((a, b) => a.path.localeCompare(b.path));
    startImportedDraft();
    setSkillFiles(nextFiles);
    setSkillDirectories([]);
    setMarkdownPreviewOpen(false);
    const skillFile = nextFiles.find((item) => item.path.split('/').pop()?.toLowerCase() === 'skill.md');
    if (skillFile) {
      setMarkdown(skillFile.content);
      setSelectedFilePath(skillFile.path);
      setSelectedFolderPath(null);
      applyMetadata(skillFile.content, { setSkillName, setSkillSlug, setSkillDescription, setSkillHomepage });
      notify.success(`已读取 ${nextFiles.length} 个文件${failedCount ? `，跳过 ${failedCount} 个无法读取文件` : ''}`);
    } else {
      setSelectedFilePath(nextFiles[0]?.path || 'SKILL.md');
      setSelectedFolderPath(null);
      notify.warning('文件夹中没有找到 SKILL.md');
    }
  }

  async function importFolderFiles(fileList: FileList | null) {
    await importSkillPackage(Array.from(fileList || []).map((file) => ({ file, path: packagePath(file) })));
  }

  async function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const target = event.target.files?.[0];
    if (target) {
      if (isSkillPackageArchive(target)) {
        await importSkillPackageFile(target);
      } else {
        await importSingleFile(target);
      }
    }
    event.target.value = '';
  }

  async function handleFolderInputChange(event: ChangeEvent<HTMLInputElement>) {
    await importFolderFiles(event.target.files);
    event.target.value = '';
  }

  function acceptsFileDrop(event: DragEvent<HTMLElement>): boolean {
    return Array.from(event.dataTransfer.types || []).includes('Files');
  }

  function handleDragEnter(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    setDragActive(true);
  }

  function handleDragOver(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    setDragActive(true);
  }

  function handleDragLeave(event: DragEvent<HTMLElement>) {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) return;
    setDragActive(false);
  }

  async function handleDrop(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    setDragActive(false);
    const dropped = await droppedSkillFiles(event.dataTransfer);
    if (!dropped.length) return;
    await withImportPreparation(async () => {
      if (dropped.length === 1 && !dropped[0].path.includes('/')) {
        if (isSkillPackageArchive(dropped[0].file)) {
          await importSkillPackageFile(dropped[0].file);
        } else {
          await importSingleFile(dropped[0].file);
        }
        return;
      }
      await importSkillPackage(dropped);
    });
  }

  const isLiveRunning = loading && !runResult;

  const importMenu = canManageCurrentScope ? (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UIButton variant="outline" className={RETURN_BUTTON_CLASS}>
          <UploadOutlined className="size-[14px]!" />
          导入
        </UIButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestImport('file')}>选择文件</DropdownMenuItem>
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestImport('folder')}>选择文件夹</DropdownMenuItem>
        {!isOverallAgent && (
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestAgentImport('plaza')}>
            <UploadOutlined />
            从广场复制
          </DropdownMenuItem>
        )}
        <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestClawHubImport()}>
          <GithubOutlined />
          从开源平台导入
        </DropdownMenuItem>
        {!isOverallAgent && (
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => requestAgentImport('employee')}>
            <TeamOutlined />
            从数字员工复制技能
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  ) : null;

  return (
    <div
      className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]"
      aria-busy={loading || saving}
    >
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title={pageTitle}
        description={pageDescription}
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap justify-end gap-[16px]">
        <UIButton variant="outline" className={RETURN_BUTTON_CLASS} onClick={() => navigate(skillsLandingRoute)}>
          <IconArrowRight className="size-3.5 rotate-180" />
          返回
        </UIButton>
        {!isNew && canManageCurrentScope && (
          <UIButton variant="outline" className={RETURN_BUTTON_CLASS} onClick={() => navigate(`/enterprise/general-skills/new${forceGalleryScope ? '?scope=gallery' : ''}`)}>
            <PlusOutlined />
            新建技能
          </UIButton>
        )}
        {importMenu}
        {canManageCurrentScope && (
          <UIButton disabled={saving} className={PRIMARY_BUTTON_CLASS} onClick={() => void importSkill()}>
            保存
          </UIButton>
        )}
      </div>

      <div className="grid grid-cols-1 gap-[20px] xl:grid-cols-2 xl:items-start">
          <SectionCard title="基本信息">
            <div className="grid grid-cols-1 gap-[16px] md:grid-cols-2">
              <Field label="技能名称">
                <Input
                  aria-label="技能名称"
                  value={skillName}
                  onChange={(event) => setSkillName(event.target.value)}
                  disabled={!canManageCurrentScope}
                  placeholder="例如 天气查询、代码审查"
                />
              </Field>
              <Field label="Slug">
                <Input
                  value={skillSlug}
                  onChange={(event) => {
                    if (editingSlug) return;
                    setSkillSlug(event.target.value);
                  }}
                  disabled={!canManageCurrentScope || Boolean(editingSlug)}
                  placeholder={editingSlug ? '创建后不可修改' : '用于路由和接口路径，例如 weather-zh'}
                />
              </Field>
              <Field label="描述">
                <Input
                  value={skillDescription}
                  onChange={(event) => setSkillDescription(event.target.value)}
                  disabled={!canManageCurrentScope}
                  placeholder="用于员工选择技能时的说明"
                />
              </Field>
              <Field label="主页链接">
                <Input
                  value={skillHomepage}
                  onChange={(event) => setSkillHomepage(event.target.value)}
                  disabled={!canManageCurrentScope}
                  placeholder="可选，参考文档或项目主页"
                />
              </Field>
              <Field label="标签分类">
                <UISelect
                  value={skillCategory || GENERAL_SKILL_CATEGORY_NONE}
                  onValueChange={(value) => setSkillCategory(value === GENERAL_SKILL_CATEGORY_NONE ? '' : value)}
                  disabled={!canManageCurrentScope}
                >
                  <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-full')} aria-label="标签分类">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={GENERAL_SKILL_CATEGORY_NONE}>未分类</SelectItem>
                    {GENERAL_SKILL_CATEGORIES.map((item) => (
                      <SelectItem key={item} value={item}>{item}</SelectItem>
                    ))}
                  </SelectContent>
                </UISelect>
              </Field>
              <div className="md:col-span-2">
                <CapabilityScopeControl
                  value={capabilityScope}
                  onChange={setCapabilityScope}
                  disabled={!canManageCurrentScope}
                  resourceType="skill"
                />
              </div>
            </div>
          </SectionCard>

          <SectionCard
            className="xl:col-start-2 xl:row-start-1"
            title="运行测试"
            extra={(
              <div className="flex flex-wrap items-center justify-end gap-[8px]">
                <ModelConfigDropdown
                  models={modelConfigs}
                  value={selectedRunModelId}
                  onChange={(modelId) => {
                    setSelectedRunModelId(modelId);
                    window.localStorage.setItem(`${GENERAL_SKILL_RUN_MODEL_STORAGE_KEY}:${TENANT_ID}`, modelId);
                  }}
                />
                <UIButton disabled={loading || !selectedSkill?.slug} className={PRIMARY_BUTTON_CLASS} onClick={() => void runSkill()}>
                  <ExperimentOutlined />
                  运行
                </UIButton>
              </div>
            )}
          >
            <div className="flex flex-col gap-[12px]">
              <Field label="选择技能">
                <UISelect value={selectedSkill?.slug} onValueChange={setSelectedSlug}>
                  <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-full')}>
                    <SelectValue placeholder={isNew && !selectedSkill ? '保存后可选择并测试' : '选择技能'} />
                  </SelectTrigger>
                  <SelectContent>
                    {rows.map((row) => (
                      <SelectItem key={row.slug} value={row.slug}>{`${row.name} / ${row.slug}`}</SelectItem>
                    ))}
                  </SelectContent>
                </UISelect>
              </Field>
              <Field label="测试问题">
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="输入要测试的问题"
                />
              </Field>
            </div>
          </SectionCard>

          <SectionCard
            className={cn(
              'order-4 flex min-h-0 flex-col xl:col-span-2 xl:row-start-3',
              dragActive && SKILL_EDITOR_DRAG_ACTIVE_CLASS,
            )}
            bodyClassName="relative flex min-h-0 flex-1 flex-col p-0"
            onDragEnter={handleDragEnter}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            title={(
              <span className="flex items-center gap-[8px]">
                <IconProfileFile className="size-[14px] shrink-0 text-[#757f9c]" />
                <span>技能文件</span>
              </span>
            )}
          >
            <input
              ref={fileInputRef}
              data-testid="general-skill-package-input"
              className={HIDDEN_FILE_INPUT_CLASS}
              type="file"
              accept=".zip,.md,.markdown,.txt"
              onChange={handleFileInputChange}
              hidden
              aria-hidden="true"
              tabIndex={-1}
            />
            <input
              ref={folderInputRef}
              className={HIDDEN_FILE_INPUT_CLASS}
              type="file"
              multiple
              {...FOLDER_INPUT_PROPS}
              onChange={handleFolderInputChange}
              hidden
              aria-hidden="true"
              tabIndex={-1}
            />
            {dragActive && (
              <div className={SKILL_DROP_HINT_CLASS}>
                <UploadOutlined />
                <span>释放以导入 SKILL.md、zip 技能包或完整技能文件夹</span>
              </div>
            )}
            {clawhubLoading && uploadPercent !== null && (
              <div aria-live="polite" className="absolute inset-x-0 top-[64px] z-10 mx-auto w-[280px] rounded-[14px] border border-[#e4e9f2] bg-white/95 p-[14px] shadow-[0_8px_24px_rgba(0,0,0,0.08)]">
                <p className="mb-[8px] flex items-center justify-between text-[12px] text-[#464C5E]">
                  <span>
                    {uploadStage === 'parsing'
                      ? '服务端解析中，大包可能需要十几秒…'
                      : '正在上传技能包…'}
                  </span>
                  <span className="tabular-nums text-[#757F9C]">{uploadPercent}%</span>
                </p>
                <div
                  role="progressbar"
                  aria-label="技能包上传进度"
                  aria-valuenow={uploadPercent}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  className="h-[6px] w-full overflow-hidden rounded-full bg-[#eef1f7]"
                >
                  <div
                    className="h-full rounded-full bg-[#18181a] transition-[width] duration-150"
                    style={{ width: `${uploadPercent}%` }}
                  />
                </div>
              </div>
            )}
            <div className={SKILL_FILE_EDITOR_CLASS}>
              <aside className={SKILL_FILE_TREE_CLASS}>
                <div className={SKILL_FILE_TREE_HEADER_CLASS}>
                  <IconFolder className="size-[14px] shrink-0 text-[#757f9c]" />
                  <span>文件系统</span>
                </div>
                {filesLoading && (
                  <div
                    aria-live="polite"
                    className="flex flex-1 flex-col items-center justify-center gap-[8px] px-[16px] text-center text-[12px] text-[#858b9c]"
                  >
                    <div className="size-[16px] animate-spin rounded-full border-2 border-[#e4e9f2] border-t-[#18181a]" />
                    <p>正在加载技能文件……</p>
                    <p className="text-[11px] leading-[16px] text-[#a7adbb]">大包文件较多，加载完成后即可查看/编辑全部文件；表单内容可先编辑。</p>
                  </div>
                )}
                {!filesLoading && (
                <>
                <div className={SKILL_FILE_TREE_LIST_CLASS} role="tree" aria-label="技能文件系统">
                  {skillFileTree.map((node) => (
                    <SkillFileTreeEntry
                      key={`${node.kind}:${node.path}`}
                      node={node}
                      depth={0}
                      expandedFolders={expandedFolders}
                      selectedFilePath={selectedFilePath}
                      selectedFolderPath={selectedFolderPath}
                      onToggleFolder={(path) => {
                        setSelectedFolderPath(path);
                        setExpandedFolders((current) => {
                          const next = new Set(current);
                          if (next.has(path)) next.delete(path);
                          else next.add(path);
                          return next;
                        });
                      }}
                      onSelectFile={(path) => {
                        setSelectedFilePath(path);
                        setSelectedFolderPath(null);
                      }}
                      onCreateEntry={openCreateEntry}
                      onRenameFile={renameSkillFile}
                      onRenameFolder={renameSkillFolder}
                      onDeleteFile={deleteSkillFile}
                      onDeleteFolder={deleteSkillFolder}
                    />
                  ))}
                </div>
                <div className={SKILL_FILE_TREE_ACTIONS_CLASS}>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <UIButton variant="outline" className={RETURN_BUTTON_CLASS}>
                        <IconAdd className="size-[14px]" />
                        新建
                        <IconChevronDown className="size-[12px]" />
                      </UIButton>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start" className={MENU_CONTENT_CLASS}>
                      <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => openCreateEntry('file')}>
                        <FilePlus2 />
                        新建文件
                      </DropdownMenuItem>
                      <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => openCreateEntry('folder')}>
                        <FolderPlus />
                        新建文件夹
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <UIButton
                    variant="outline"
                    onClick={deleteSelectedEntry}
                    className={DELETE_BUTTON_CLASS}
                  >
                    <IconTrash className="size-[14px]" />
                    删除
                  </UIButton>
                </div>
                </>
                )}
              </aside>
              <section className={SKILL_FILE_PANE_CLASS}>
                <div className={SKILL_FILE_TAB_CLASS}>
                  <IconProfileFile className="size-[14px] shrink-0 text-[#757f9c]" />
                  <span className="min-w-0 truncate text-[#18181a]">{selectedFile?.path || '未选择文件'}</span>
                  <div className="ml-auto flex items-center gap-1">
                    {selectedFileCanPreview && (
                      <button
                        type="button"
                        className={SKILL_FILE_TAB_ACTION_BUTTON_CLASS}
                        aria-label={markdownPreviewOpen ? '切换到编辑' : '切换到渲染'}
                        aria-pressed={markdownPreviewOpen}
                        title={markdownPreviewOpen ? '编辑' : '渲染'}
                        onClick={() => setMarkdownPreviewOpen((current) => !current)}
                      >
                        {markdownPreviewOpen ? <EyeOff className="size-[14px]" /> : <Eye className="size-[14px]" />}
                        <span>{markdownPreviewOpen ? '编辑' : '渲染'}</span>
                      </button>
                    )}
                  </div>
                </div>
                {selectedFileCanPreview && markdownPreviewOpen ? (
                  <div className={SKILL_MARKDOWN_PREVIEW_CLASS}>
                    <div className={SKILL_MARKDOWN_PREVIEW_BODY_CLASS}>
                      {renderMarkdownBlocks(selectedFile?.content || '暂无内容')}
                    </div>
                  </div>
                ) : (
                  <div className={SKILL_CODE_EDITOR_CLASS} data-language={selectedFileLanguage}>
                    <pre className={SKILL_CODE_HIGHLIGHT_CLASS} aria-hidden="true">
                      <code
                        className={SKILL_CODE_HIGHLIGHT_CODE_CLASS}
                        style={{
                          transform: `translate(${-editorScroll.left}px, ${-editorScroll.top}px)`,
                        }}
                      >
                        {renderCodeTokens(selectedFile?.content || '\u200b', selectedFileLanguage)}
                      </code>
                    </pre>
                    <textarea
                      className={SKILL_CODE_INPUT_CLASS}
                      value={selectedFile?.content || ''}
                      onChange={(event) => updateSelectedFile(event.target.value)}
                      onScroll={(event) => setEditorScroll({
                        top: event.currentTarget.scrollTop,
                        left: event.currentTarget.scrollLeft,
                      })}
                      spellCheck={false}
                    />
                  </div>
                )}
              </section>
            </div>
          </SectionCard>

          <SectionCard
            className="order-3 min-h-0 xl:col-span-2 xl:row-start-2"
            bodyClassName={cn('min-h-0 overflow-auto p-[18px]', !resultExpanded && 'hidden')}
            title={(
              <span className="flex items-center gap-[8px]">
                <IconPlay className="size-[14px] shrink-0 text-[#757f9c]" />
                <span>运行结果</span>
                {activeResult && (
                  isLiveRunning
                    ? <span className="inline-flex items-center gap-[4px] rounded-full bg-[#e6f4ff] px-[8px] py-px text-[12px] font-bold text-[#0958d9]">运行中</span>
                    : resultSucceeded(activeResult)
                    ? <span className="inline-flex items-center gap-[4px] rounded-full bg-[#eafbf0] px-[8px] py-px text-[12px] font-bold text-[#018434]"><CheckCircleOutlined />成功</span>
                    : <span className="inline-flex items-center gap-[4px] rounded-full bg-[#fce7e7] px-[8px] py-px text-[12px] font-bold text-[#d20b0b]"><CloseCircleOutlined />失败</span>
                )}
              </span>
            )}
            extra={(
              <button
                type="button"
                className="inline-flex size-[32px] items-center justify-center rounded-[6px] text-[#757f9c] transition-colors hover:bg-[#f2f3f7] hover:text-[#18181a]"
                aria-label={resultExpanded ? '收起运行结果' : '展开运行结果'}
                aria-expanded={resultExpanded}
                onClick={() => setResultExpanded((current) => !current)}
              >
                <IconChevronDown
                  className={cn('size-[14px] transition-transform', resultExpanded && 'rotate-180')}
                />
              </button>
            )}
          >
            {activeResult ? (
              <div className={SKILL_RESULT_LAYOUT_CLASS}>
                {(() => {
                  const traceItems = activeResult.execution_trace || [];
                  const latestCodeIndex = traceItems.reduce(
                    (latest, traceItem, traceIndex) => (traceItemCode(traceItem) ? traceIndex : latest),
                    -1,
                  );
                  return (
                    <>
                <section className={SKILL_REPLY_PANEL_CLASS}>
                  <div className={SKILL_SECTION_LABEL_CLASS}>最终回复</div>
                  <p className={SKILL_REPLY_TEXT_CLASS}>
                    {activeResult.reply || (loading ? '正在运行技能...' : '暂无回复')}
                  </p>
                </section>

                <section>
                  <div className={SKILL_SECTION_LABEL_CLASS}>执行流程</div>
                  <div className={SKILL_TRACE_LIST_CLASS}>
                    {traceItems.map((item, index) => {
                      const phase = typeof item.phase === 'string' ? item.phase : '';
                      const detail = traceDetail(item);
                      const code = traceItemCode(item);
                      const codeTitle = typeof item.attempt === 'number'
                        ? `第 ${item.attempt} 次 Python runner`
                        : 'Python runner';
                      return (
                        <div className={SKILL_TRACE_ITEM_CLASS} key={`${phase || 'phase'}-${index}`}>
                          <div className={SKILL_TRACE_DOT_CLASS} />
                          <div className={SKILL_TRACE_ITEM_BODY_CLASS}>
                            <div className={SKILL_TRACE_TITLE_CLASS}>{PHASE_LABELS[phase] || String(item.message || phase || '执行')}</div>
                            <div className={SKILL_TRACE_MESSAGE_CLASS}>{String(item.message || '')}</div>
                            {detail && (
                              <RunCodePanel
                                className="mt-2"
                                title={phase === 'code_finished' ? '查看执行结果' : phase === 'stdout_chunk' ? '查看运行输出' : '查看详情'}
                                code={detail}
                                language={codeLanguage(detail)}
                                defaultOpen={phase === 'code_finished' || phase === 'code_timeout'}
                              />
                            )}
                            {code && (
                              <details className={cn(SKILL_TRACE_CODE_DETAILS_CLASS, 'mt-[10px]')} open={index === latestCodeIndex}>
                                <summary className={SKILL_TRACE_CODE_SUMMARY_CLASS}>
                                  {codeTitle}
                                  <TraceDisclosureLabel />
                                </summary>
                                <CodeBlock className={SKILL_CODE_BLOCK_CLASS} code={code} language="python" />
                              </details>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section>
                  <div className={SKILL_SECTION_LABEL_CLASS}>运行输出</div>
                  <div className={SKILL_OUTPUT_STACK_CLASS}>
                    <RunCodePanel
                      title="结构化结果"
                      code={formatJson(activeResult.structured_result) || '无结构化结果'}
                      language="json"
                      defaultOpen
                    />
                    <RunCodePanel
                      title="stdout"
                      code={formatJson(activeResult.stdout) || '无 stdout'}
                      language={codeLanguage(formatJson(activeResult.stdout), 'text')}
                    />
                    <RunCodePanel
                      title="stderr"
                      code={formatJson(activeResult.stderr) || '无 stderr'}
                      language={codeLanguage(formatJson(activeResult.stderr), 'text')}
                    />
                  </div>
                </section>
                    </>
                  );
                })()}
              </div>
            ) : (
              <div className="flex min-h-[120px] flex-col items-center justify-center gap-[8px] text-center text-[13px] text-muted-foreground">
                运行后将在这里显示回复、执行流程、代码和输出
              </div>
            )}
          </SectionCard>
      </div>
      <ClawHubDialog
        open={clawhubModalOpen}
        loading={clawhubLoading}
        source={clawhubSource}
        onSourceChange={setClawhubSource}
        onClose={cancelClawHubImport}
        onSubmit={() => void importClawHubSource()}
      />
      <ResourceImportDialog
        open={agentImportOpen}
        loading={agentImportLoading}
        progress={agentImportProgress}
        icon={<IconSkill className="size-[14px] shrink-0" />}
        title={agentImportMode === 'plaza' ? '从广场复制技能' : '从数字员工复制技能'}
        sourcePlaceholder={agentImportMode === 'plaza' ? '选择开放广场' : '选择复制来源'}
        sources={agentImportMode === 'plaza'
          ? openGalleryImportSourceOptions(agentImportAgents, '开放广场')
          : visibleEmployeeAgents(agentImportAgents, currentUser, { activeOnly: true, excludeAgentId: agentId })
            .map((item) => ({ value: item.id, label: item.name }))}
        sourceId={agentImportSourceAgentId}
        itemsLabel="选择技能"
        items={agentImportSourceSkills.map((item) => ({
          id: item.id,
          disabled: Boolean((item as { __is_bound?: boolean }).__is_bound),
          disabledHint: '已添加到当前员工',
          label: (
            <>
              {item.name}
              <span className="text-[#858b9c]"> · {item.slug}</span>
            </>
          ),
        }))}
        selectedIds={agentImportSelectedSkillIds}
        emptyText="没有可复制的技能"
        note={agentImportMode === 'plaza'
          ? '从开放广场复制可用技能；不会覆盖当前编辑区内容。'
          : '从数字员工复制可用技能；不会覆盖当前编辑区内容。'}
        onSourceChange={(value) => {
          setAgentImportSourceAgentId(value);
          void loadAgentImportSourceSkills(value);
        }}
        onSelectedChange={setAgentImportSelectedSkillIds}
        onClose={() => setAgentImportOpen(false)}
        onSubmit={() => void submitAgentImportSkills()}
      />

      <MissingReferencesConfirm
        prompt={missingRefsPrompt}
        onCancel={() => setMissingRefsPrompt(null)}
        onConfirm={confirmMissingReferences}
      />

      <ConfirmDialog
        open={Boolean(deleteSkillTarget)}
        onOpenChange={(open) => !open && setDeleteSkillTarget(null)}
        title={deleteSkillTarget ? `${isOverallAgent ? '删除' : '移除'}技能「${deleteSkillTarget.name}」？` : ''}
        description={isOverallAgent
          ? '删除后该技能不会再出现在组织技能库中，此操作不可撤销。'
          : '这只会在当前数字员工中隐藏该技能；开放广场和其他数字员工仍然保留。'}
        confirmText={isOverallAgent ? '删除' : '移除'}
        onConfirm={() => void runDeleteSkill()}
      />

      <ConfirmDialog
        open={Boolean(deleteFileTarget)}
        onOpenChange={(open) => !open && setDeleteFileTarget(null)}
        title={deleteFileTarget ? `删除文件「${deleteFileTarget.path}」？` : ''}
        description="删除后需要重新导入或手动新建该文件。"
        confirmText="删除"
        onConfirm={runDeleteFile}
      />

      <ConfirmDialog
        open={Boolean(deleteFolderTarget)}
        onOpenChange={(open) => !open && setDeleteFolderTarget(null)}
        title={deleteFolderTarget ? `删除文件夹「${deleteFolderTarget}」？` : ''}
        description="文件夹内的所有文件和子文件夹都会一并删除。"
        confirmText="删除"
        onConfirm={runDeleteFolder}
      />

      <Dialog open={Boolean(createEntryMode)} onOpenChange={(open) => { if (!open) setCreateEntryMode(null); }}>
        <DialogContent aria-describedby={undefined} className="flex w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[420px]">
          <DialogTitle className="border-b border-border px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            {createEntryMode === 'folder' ? '新建文件夹' : '新建文件'}
          </DialogTitle>
          <div className="px-[24px] py-[16px]">
            <Input
              autoFocus
              value={createEntryValue}
              placeholder={createEntryMode === 'folder' ? '例如 references' : '例如 references/guide.md'}
              onChange={(event) => setCreateEntryValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  runCreateEntry();
                }
              }}
            />
          </div>
          <div className="flex items-center justify-end gap-[8px] bg-background px-[24px] py-[12px]">
            <UIButton
              variant="outline"
              onClick={() => setCreateEntryMode(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              onClick={runCreateEntry}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              新建
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={importPrepareOpen}
        onOpenChange={(open) => { if (!open) { setImportPrepareOpen(false); importPrepareActionRef.current = null; } }}
      >
        <DialogContent aria-describedby={undefined} className="flex w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[460px]">
          <DialogTitle className="border-b border-border px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            导入新技能前是否保存当前技能？
          </DialogTitle>
          <p className="px-[24px] py-[16px] text-[13px] leading-[20px] text-[#4f5669]">
            你正在编辑现有技能。导入会进入新建状态，不会覆盖当前技能。
          </p>
          <div className="flex items-center justify-end gap-[8px] bg-background px-[24px] py-[12px]">
            <UIButton
              variant="outline"
              onClick={() => { setImportPrepareOpen(false); importPrepareActionRef.current = null; }}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] bg-white px-[14px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              variant="outline"
              onClick={() => void confirmImportPrepareSkip()}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] bg-white px-[14px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              不保存，继续导入
            </UIButton>
            <UIButton
              onClick={() => void confirmImportPrepareSave()}
              className="h-[32px] rounded-[10px] bg-[#18181a] px-[14px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              保存并发布
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(renameTarget || renameFolderTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setRenameTarget(null);
            setRenameFolderTarget(null);
          }
        }}
      >
        <DialogContent aria-describedby={undefined} className="flex w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[420px]">
          <DialogTitle className="border-b border-border px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            {renameFolderTarget ? '重命名文件夹' : '重命名文件'}
          </DialogTitle>
          <div className="px-[24px] py-[16px]">
            <Input
              autoFocus
              value={renameValue}
              onChange={(event) => setRenameValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  if (renameFolderTarget) runRenameFolder();
                  else runRenameFile();
                }
              }}
            />
          </div>
          <div className="flex items-center justify-end gap-[8px] bg-background px-[24px] py-[12px]">
            <UIButton
              variant="outline"
              onClick={() => {
                setRenameTarget(null);
                setRenameFolderTarget(null);
              }}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              onClick={renameFolderTarget ? runRenameFolder : runRenameFile}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              重命名
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
