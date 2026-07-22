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
import { Ban, CircleCheck, Copy, Users } from 'lucide-react';
import { ContextMenu } from 'radix-ui';

import { api, streamPost, TENANT_ID } from '../api/client';
import { isEnterpriseAdmin, type EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import { ConfirmDialog } from '@/components/ConfirmDialog';
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
import type { AgentProfileRead, GeneralSkillRead, GeneralSkillRunResponse, ModelConfigRead } from '../types';

const GENERAL_SKILL_PAGE_SIZE = 10;
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
const SKILL_CODE_EDITOR_CLASS =
  'relative min-h-0 overflow-hidden bg-[#fafafa] font-mono text-[13px] leading-[1.7] tab-[2] shadow-[inset_0_1px_0_#e3e7f1]';
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
const GENERAL_SKILL_RUN_TIMEOUT_MS = 120_000;
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

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || '');
      resolve(value.includes(',') ? value.split(',', 2)[1] : value);
    };
    reader.onerror = () => reject(reader.error || new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
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
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | GeneralSkillRead['status']>('all');
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [isOverallAgent, setIsOverallAgent] = useState(true);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [clawhubModalOpen, setClawhubModalOpen] = useState(false);
  const [clawhubSource, setClawhubSource] = useState('');
  const [clawhubLoading, setClawhubLoading] = useState(false);
  const clawhubAbortRef = useRef<AbortController | null>(null);
  const [agentImportOpen, setAgentImportOpen] = useState(false);
  const [agentImportMode, setAgentImportMode] = useState<GeneralSkillImportMode>('plaza');
  const [agentImportLoading, setAgentImportLoading] = useState(false);
  const [agentImportAgents, setAgentImportAgents] = useState<AgentProfileRead[]>([]);
  const [agentImportSourceAgentId, setAgentImportSourceAgentId] = useState('');
  const [agentImportSourceSkills, setAgentImportSourceSkills] = useState<GeneralSkillRead[]>([]);
  const [agentImportSelectedSkillIds, setAgentImportSelectedSkillIds] = useState<string[]>([]);
  const [agentScopeLoaded, setAgentScopeLoaded] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<GeneralSkillRead | null>(null);
  const [deleting, setDeleting] = useState(false);

  const pageTitle = isOverallAgent ? '技能广场' : '技能';
  const listLabel = isOverallAgent ? '技能广场列表' : '技能列表';
  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const canManageCurrentScope = currentAgent
    ? canManageEmployeeAgent(currentAgent, currentUser)
    : isEnterpriseAdmin(currentUser) && isOverallAgent;

  const load = () => {
    const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    setLoading(true);
    return api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentSuffix}`)
      .then(setRows)
      .catch((error) => notify.error(error.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

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
      const detail = (event as CustomEvent<{ agentId?: string }>).detail;
      setAgentId(detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    return rows.filter((row) => {
      const matchesStatus = statusFilter === 'all' || row.status === statusFilter;
      const haystack = [
        row.name,
        row.slug,
        row.description,
        row.homepage,
        resourceCreatorName(row),
      ].filter(Boolean).join(' ').toLowerCase();
      return matchesStatus && (!keyword || haystack.includes(keyword));
    });
  }, [rows, searchText, statusFilter]);

  const pagination = useClientPagination(filteredRows, GENERAL_SKILL_PAGE_SIZE, `${searchText}|${statusFilter}`);

  const stats = useMemo(() => ({
    total: rows.length,
    published: rows.filter((row) => row.status === 'published').length,
    draft: rows.filter((row) => row.status === 'draft').length,
    archived: rows.filter((row) => row.status === 'archived').length,
  }), [rows]);

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    try {
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${row.slug}/${published ? 'publish' : 'archive'}?tenant_id=${TENANT_ID}${agentSuffix}`,
      );
      setRows((current) => current.map((item) => (item.id === next.id ? next : item)));
      notify.success(published ? '已启用技能' : '已停用技能');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : published ? '启用失败' : '停用失败');
    }
  }

  async function confirmDeleteSkill() {
    const row = deleteTarget;
    if (!row) return;
    const branchMode = !isOverallAgent;
    setDeleting(true);
    try {
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
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
        `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(sourceAgentId)}`,
      );
      const existingIds = new Set(rows.map((item) => item.id));
      const publishedRows = sourceRows.filter((item) => item.status === 'published' && !existingIds.has(item.id));
      setAgentImportSourceSkills(publishedRows);
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
      await api.post(`/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/import`, {
        tenant_id: TENANT_ID,
        source_agent_id: agentImportSourceAgentId,
        resource_type: 'general_skill',
        resource_ids: agentImportSelectedSkillIds,
      });
      notify.success(`已复制 ${agentImportSelectedSkillIds.length} 个技能`);
      setAgentImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制技能失败');
    } finally {
      setAgentImportLoading(false);
    }
  }

  async function importClawHubSource() {
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
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已新增 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setClawhubModalOpen(false);
      navigate(`/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit`);
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
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
    if (isOverallAgent && !canManageCurrentScope) {
      return null;
    }
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
            onSelect={() => navigate(`/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit`)}
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
          <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
          <DropdownMenuItem
            variant="destructive"
            className={MENU_ITEM_DANGER_CLASS}
            onSelect={() => setDeleteTarget(row)}
          >
            <IconTrash />
            {isOverallAgent ? '删除' : '移除'}
          </DropdownMenuItem>
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
          <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>
          <span>{row.skill_files?.length || 1} 个文件 · {formatDateTime(row.updated_at)}</span>
        </div>
      </article>
    );
  };

  const listEmptyText = isOverallAgent
    ? canManageCurrentScope ? '暂无技能，点击「新增」创建一个吧' : '暂无技能'
    : '当前员工暂无技能';

  return (
    <div className={embedded ? undefined : 'min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]'}>
      {!embedded && (
        <>
          <AppHeader onLogout={onLogout} userName={currentUser?.username} title={pageTitle} />
          <div className="mt-[20px] mb-[16px] flex items-center justify-end gap-[12px]">
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
                  新增
                  <IconChevronDown className="size-[12px]" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
                  <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => navigate('/enterprise/general-skills/new')}>
                    <IconAdd />
                    新建技能
                  </DropdownMenuItem>
                  {!isOverallAgent && (
                    <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void requestAgentImport('plaza')}>
                      <Copy />
                      从广场复制
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
        </>
      )}

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
    </div>
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
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [markdown, setMarkdown] = useState(EMPTY_SKILL_MARKDOWN);
  const [skillName, setSkillName] = useState('');
  const [skillSlug, setSkillSlug] = useState('');
  const [skillDescription, setSkillDescription] = useState('');
  const [skillHomepage, setSkillHomepage] = useState('');
  const [skillFiles, setSkillFiles] = useState<GeneralSkillFile[]>([
    { path: 'SKILL.md', content: EMPTY_SKILL_MARKDOWN, size: EMPTY_SKILL_MARKDOWN.length, mime_type: 'text/markdown' },
  ]);
  const [selectedSlug, setSelectedSlug] = useState<string>();
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [runResult, setRunResult] = useState<GeneralSkillRunResponse | null>(null);
  const [liveResult, setLiveResult] = useState<Partial<GeneralSkillRunResponse> | null>(null);
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [selectedRunModelId, setSelectedRunModelId] = useState(
    () => window.localStorage.getItem(`${GENERAL_SKILL_RUN_MODEL_STORAGE_KEY}:${TENANT_ID}`) || '',
  );
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFilePath, setSelectedFilePath] = useState('SKILL.md');
  const [editorScroll, setEditorScroll] = useState({ top: 0, left: 0 });
  const [clawhubModalOpen, setClawhubModalOpen] = useState(false);
  const [clawhubSource, setClawhubSource] = useState('');
  const [clawhubLoading, setClawhubLoading] = useState(false);
  const [agentImportOpen, setAgentImportOpen] = useState(false);
  const [agentImportMode, setAgentImportMode] = useState<GeneralSkillImportMode>('plaza');
  const [agentImportLoading, setAgentImportLoading] = useState(false);
  const [agentImportAgents, setAgentImportAgents] = useState<AgentProfileRead[]>([]);
  const [agentImportSourceAgentId, setAgentImportSourceAgentId] = useState('');
  const [agentImportSourceSkills, setAgentImportSourceSkills] = useState<GeneralSkillRead[]>([]);
  const [agentImportSelectedSkillIds, setAgentImportSelectedSkillIds] = useState<string[]>([]);
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [isOverallAgent, setIsOverallAgent] = useState(true);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [deleteSkillTarget, setDeleteSkillTarget] = useState<GeneralSkillRead | null>(null);
  const [deleteFileTarget, setDeleteFileTarget] = useState<GeneralSkillFile | null>(null);
  const [renameTarget, setRenameTarget] = useState<GeneralSkillFile | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [importPrepareOpen, setImportPrepareOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const clawhubAbortRef = useRef<AbortController | null>(null);
  const importPrepareActionRef = useRef<null | (() => void | Promise<void>)>(null);

  const selectedSkill = useMemo(
    () => rows.find((row) => row.slug === selectedSlug),
    [rows, selectedSlug],
  );
  const activeResult = runResult || liveResult;
  const selectedFile = useMemo(
    () => skillFiles.find((file) => file.path === selectedFilePath) || skillFiles[0],
    [skillFiles, selectedFilePath],
  );
  const selectedFileLanguage = useMemo(() => languageFromFilePath(selectedFile?.path), [selectedFile?.path]);
  const isNew = mode === 'new';
  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const canManageCurrentScope = currentAgent
    ? canManageEmployeeAgent(currentAgent, currentUser)
    : isEnterpriseAdmin(currentUser) && isOverallAgent;
  const pageTitle = isNew ? '新建空白技能' : '编辑技能';
  const pageDescription = isOverallAgent
    ? (isNew
      ? '填写技能定义并编辑 SKILL.md，保存后可在右侧运行测试。'
      : '维护技能广场中的技能定义、文件包和运行测试。')
    : (isNew
      ? '为当前数字员工创建技能，填写基本信息并编辑技能文件。'
      : '维护当前数字员工技能的定义、文件包和运行测试。');

  const load = () => {
    const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    return api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentSuffix}`)
      .then((items) => {
        setRows(items);
        if (mode === 'edit') {
          const target = items.find((item) => item.slug === routeSlug);
          if (target) {
            editSkill(target);
          } else if (routeSlug) {
            notify.error('未找到要编辑的技能');
          }
        }
      })
      .catch((error) => notify.error(error.message));
  };

  useEffect(() => {
    if (mode === 'new') {
      newSkill();
    }
    void load();
  }, [agentId, mode, routeSlug]);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setAgents(items);
        setIsOverallAgent(Boolean(items.find((item) => item.id === agentId)?.is_overall ?? true));
      })
      .catch(() => setIsOverallAgent(true));
  }, [agentId]);

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
      const detail = (event as CustomEvent<{ agentId?: string }>).detail;
      setAgentId(detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

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
    setEditorScroll({ top: 0, left: 0 });
  }, [selectedFilePath]);

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
      || skillHomepage !== (original.homepage || '')
      || normalizedSkillFiles(skillFiles) !== normalizedSkillFiles(
        original.skill_files?.length ? original.skill_files : [{ path: 'SKILL.md', content: original.skill_markdown }],
      )
    );
  }

  async function importSkill(): Promise<GeneralSkillRead | null> {
    if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return null;
    }
    if (!markdown.trim()) {
      notify.warning('请先粘贴或上传 SKILL.md');
      return null;
    }
    setSaving(true);
    try {
      const row = await api.post<GeneralSkillRead>('/api/enterprise/general-skills/import', {
        tenant_id: TENANT_ID,
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        name: skillName.trim() || undefined,
        slug: editingSlug || skillSlug.trim() || undefined,
        description: skillDescription.trim() || undefined,
        homepage: skillHomepage.trim() || undefined,
        markdown,
        files: skillFiles.length ? skillFiles : [{ path: 'SKILL.md', content: markdown }],
        status: 'published',
        original_slug: editingSlug || undefined,
      });
      notify.success(editingSlug ? `已保存 ${row.name}` : `已新增 ${row.name}`);
      setSelectedSlug(row.slug);
      setEditingSlug(row.slug);
      setMarkdown(row.skill_markdown);
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
      setRows((current) => {
        const withoutSaved = current.filter((item) => item.id !== row.id && item.slug !== row.slug);
        return [row, ...withoutSaved];
      });
      navigate(`/enterprise/general-skills/${encodeURIComponent(row.slug)}/edit`, { replace: !editingSlug });
      void load();
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
    setSkillFiles([{ path: 'SKILL.md', content: EMPTY_SKILL_MARKDOWN, size: EMPTY_SKILL_MARKDOWN.length, mime_type: 'text/markdown' }]);
    setSelectedFilePath('SKILL.md');
    setEditingSlug(null);
    setSelectedSlug(undefined);
    setQuery('');
    setRunResult(null);
    setLiveResult(null);
  }

  function editSkill(row: GeneralSkillRead) {
    setMarkdown(row.skill_markdown);
    setSkillName(row.name);
    setSkillSlug(row.slug);
    setSkillDescription(row.description || '');
    setSkillHomepage(row.homepage || '');
    setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
    setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
    setSelectedSlug(row.slug);
    setEditingSlug(row.slug);
    setRunResult(null);
  }

  function replaceRow(row: GeneralSkillRead) {
    setRows((current) => current.map((item) => (item.id === row.id ? row : item)));
    if (editingSlug === row.slug) {
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setMarkdown(row.skill_markdown);
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
    }
  }

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return;
    }
    try {
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
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
    if (!canManageCurrentScope) {
      notify.error('只有管理员可以编辑技能广场内容');
      return;
    }
    const branchMode = !isOverallAgent;
    try {
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
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
    const saved = await importSkill();
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
        `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(sourceAgentId)}`,
      );
      const existingIds = new Set(rows.map((item) => item.id));
      setAgentImportSourceSkills(sourceRows.filter((item) => item.status === 'published' && !existingIds.has(item.id)));
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
      await api.post(`/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/import`, {
        tenant_id: TENANT_ID,
        source_agent_id: agentImportSourceAgentId,
        resource_type: 'general_skill',
        resource_ids: agentImportSelectedSkillIds,
      });
      notify.success(`已复制 ${agentImportSelectedSkillIds.length} 个技能`);
      setAgentImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制技能失败');
    } finally {
      setAgentImportLoading(false);
    }
  }

  async function importClawHubSource() {
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
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已新增 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setSelectedSlug(row.slug);
      editSkill(row);
      setClawhubModalOpen(false);
      void load();
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
      notify.error(error instanceof Error ? error.message : '从开源平台导入失败');
    } finally {
      if (clawhubAbortRef.current === controller) {
        clawhubAbortRef.current = null;
        setClawhubLoading(false);
      }
    }
  }

  async function importSkillPackageFile(file: File) {
    const controller = new AbortController();
    clawhubAbortRef.current?.abort();
    clawhubAbortRef.current = controller;
    setClawhubLoading(true);
    try {
      const contentBase64 = await fileToBase64(file);
      if (controller.signal.aborted) return;
      const row = await api.postWithSignal<GeneralSkillRead>('/api/enterprise/general-skills/import-package', {
        tenant_id: TENANT_ID,
        agent_id: !isOverallAgent && agentId ? agentId : undefined,
        filename: file.name,
        content_base64: contentBase64,
        status: 'published',
      }, controller.signal);
      if (controller.signal.aborted) return;
      notify.success(`已上传 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setSelectedSlug(row.slug);
      editSkill(row);
      setClawhubModalOpen(false);
      void load();
    } catch (error) {
      if (isAbortError(error)) {
        notify.info('已取消导入');
        return;
      }
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

  function addSkillFile() {
    const base = 'notes.md';
    let candidate = base;
    let index = 2;
    while (skillFiles.some((file) => file.path === candidate)) {
      candidate = `notes-${index}.md`;
      index += 1;
    }
    setSkillFiles((current) => [...current, { path: candidate, content: '', size: 0, mime_type: 'text/markdown' }]);
    setSelectedFilePath(candidate);
  }

  function deleteSelectedFile() {
    if (!selectedFile) return;
    deleteSkillFile(selectedFile);
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

  function renameSkillFile(target: GeneralSkillFile) {
    setRenameTarget(target);
    setRenameValue(target.path);
  }

  function runRenameFile() {
    const target = renameTarget;
    if (!target) return;
    {
      const nextPath = renameValue;
      {
        const normalized = normalizeSkillFilePath(nextPath);
        if (!normalized) {
          notify.error('文件名不能为空');
          return;
        }
        if (normalized === target.path) {
          setRenameTarget(null);
          return;
        }
        if (skillFiles.some((file) => file.path === normalized)) {
          notify.error('已存在同名文件');
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
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, GENERAL_SKILL_RUN_TIMEOUT_MS);
    try {
      let completed = false;
      await streamPost(
        `/api/enterprise/general-skills/${slug}/run/stream`,
        {
          tenant_id: TENANT_ID,
          agent_id: agentId || undefined,
          user_id: 'enterprise_demo',
          query,
          model_config_id: selectedRunModelId || undefined,
          max_attempts: 10,
        },
        (item) => {
          if (item.event === 'trace') {
            const traceItem = item.data;
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
            setRunResult(result);
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
        ? '技能运行超时，请检查模型或稍后重试。'
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
    setSelectedFilePath('SKILL.md');
    setMarkdown(text);
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
    const skillFile = nextFiles.find((item) => item.path.split('/').pop()?.toLowerCase() === 'skill.md');
    if (skillFile) {
      setMarkdown(skillFile.content);
      setSelectedFilePath(skillFile.path);
      applyMetadata(skillFile.content, { setSkillName, setSkillSlug, setSkillDescription, setSkillHomepage });
      notify.success(`已读取 ${nextFiles.length} 个文件${failedCount ? `，跳过 ${failedCount} 个无法读取文件` : ''}`);
    } else {
      setSelectedFilePath(nextFiles[0]?.path || 'SKILL.md');
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
        <UIButton variant="outline" className={RETURN_BUTTON_CLASS} onClick={() => navigate('/enterprise/general-skills')}>
          <IconArrowRight className="size-3.5 rotate-180" />
          返回技能
        </UIButton>
        {!isNew && canManageCurrentScope && (
          <UIButton variant="outline" className={RETURN_BUTTON_CLASS} onClick={() => navigate('/enterprise/general-skills/new')}>
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

      <div className="grid grid-cols-1 gap-[20px] xl:grid-cols-2 xl:grid-rows-[auto_minmax(0,1fr)] xl:items-stretch">
          <SectionCard title="基本信息">
            <div className="grid grid-cols-1 gap-[16px] md:grid-cols-2">
              <Field label="技能名称">
                <Input
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
              'flex h-full min-h-0 flex-col xl:col-start-1 xl:row-start-2',
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
            <div className={SKILL_FILE_EDITOR_CLASS}>
              <aside className={SKILL_FILE_TREE_CLASS}>
                <div className={SKILL_FILE_TREE_HEADER_CLASS}>
                  <IconFolder className="size-[14px] shrink-0 text-[#757f9c]" />
                  <span>文件</span>
                </div>
                <div className={SKILL_FILE_TREE_LIST_CLASS}>
                  {skillFiles.map((file) => (
                    <ContextMenu.Root key={file.path}>
                      <ContextMenu.Trigger asChild>
                        <button
                          type="button"
                          className={skillFileNodeClass(file.path === selectedFile?.path)}
                          onClick={() => setSelectedFilePath(file.path)}
                          onContextMenu={() => setSelectedFilePath(file.path)}
                          title={file.path}
                        >
                          <IconProfileFile className="size-[14px] shrink-0" />
                          <span className="min-w-0 truncate">{file.path}</span>
                        </button>
                      </ContextMenu.Trigger>
                      <ContextMenu.Portal>
                        <ContextMenu.Content className={MENU_CONTENT_CLASS}>
                          <ContextMenu.Item className={MENU_ITEM_CLASS} onSelect={() => renameSkillFile(file)}>
                            <EditOutlined />
                            重命名
                          </ContextMenu.Item>
                          <ContextMenu.Item className={MENU_ITEM_DANGER_CLASS} onSelect={() => deleteSkillFile(file)}>
                            <DeleteOutlined />
                            删除
                          </ContextMenu.Item>
                        </ContextMenu.Content>
                      </ContextMenu.Portal>
                    </ContextMenu.Root>
                  ))}
                </div>
                <div className={SKILL_FILE_TREE_ACTIONS_CLASS}>
                  <UIButton variant="outline" onClick={addSkillFile} className={RETURN_BUTTON_CLASS}>
                    <IconAdd className="size-[14px]" />
                    新建文件
                  </UIButton>
                  <UIButton
                    variant="outline"
                    onClick={deleteSelectedFile}
                    className={DELETE_BUTTON_CLASS}
                  >
                    <IconTrash className="size-[14px]" />
                    删除
                  </UIButton>
                </div>
              </aside>
              <section className={SKILL_FILE_PANE_CLASS}>
                <div className={SKILL_FILE_TAB_CLASS}>
                  <IconProfileFile className="size-[14px] shrink-0 text-[#757f9c]" />
                  <span className="min-w-0 truncate text-[#18181a]">{selectedFile?.path || '未选择文件'}</span>
                </div>
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
              </section>
            </div>
          </SectionCard>

          <SectionCard
            className="flex h-full min-h-0 flex-col xl:col-start-2 xl:row-start-2"
            bodyClassName="flex min-h-0 flex-1 flex-col overflow-auto p-[18px]"
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
              <div className="flex min-h-[560px] flex-1 flex-col items-center justify-center gap-[8px] text-center text-[13px] text-muted-foreground xl:min-h-0">
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

      <Dialog open={Boolean(renameTarget)} onOpenChange={(open) => { if (!open) setRenameTarget(null); }}>
        <DialogContent aria-describedby={undefined} className="flex w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[420px]">
          <DialogTitle className="border-b border-border px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            重命名文件
          </DialogTitle>
          <div className="px-[24px] py-[16px]">
            <Input
              autoFocus
              value={renameValue}
              onChange={(event) => setRenameValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  runRenameFile();
                }
              }}
            />
          </div>
          <div className="flex items-center justify-end gap-[8px] bg-background px-[24px] py-[12px]">
            <UIButton
              variant="outline"
              onClick={() => setRenameTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              onClick={runRenameFile}
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
