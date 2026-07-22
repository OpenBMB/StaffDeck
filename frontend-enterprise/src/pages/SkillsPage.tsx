import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Ban, CircleCheck, Copy, Eye, RotateCcw, Upload, Users } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
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
  Select,
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
} from '@/lib/enterprise-ui';
import { DetailField } from '@/components/DetailField';
import { ResourceImportDialog } from '@/components/ResourceImportDialog';

import { api, TENANT_ID } from '../api/client';
import IconAdd from '../assets/icons/add.svg?react';
import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconClipboard from '../assets/icons/cap-clipboard.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconHistory from '../assets/icons/profile-history.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconSkill from '../assets/icons/plaza-skill.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import { isEnterpriseAdmin, type EnterpriseAuthUser } from '../auth';
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
import type { AgentProfileRead, SkillRead, SkillVersionRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const SKILL_PAGE_SIZE = 10;
const RANKING_PAGE_SIZE = 10;

const STATUS_BADGE: Record<SkillRead['status'], { tone: BadgeTone; text: string }> = {
  draft: { tone: 'blue', text: '草稿' },
  published: { tone: 'green', text: '已启用' },
  archived: { tone: 'gray', text: '已停用' },
};

type RankingMode = 'calls' | 'positive' | 'negative';
type RankingScope = 'current' | 'total';
type RankedSkill = SkillRead & { rank: number };
type RankingModalState = { mode: RankingMode; scope: RankingScope };
type SkillStatusFilter = 'all' | SkillRead['status'];
type BranchFilter = 'all' | 'synced' | 'diverged' | 'inactive';
type NumericSkillMetric =
  | 'call_count'
  | 'positive_feedback_count'
  | 'negative_feedback_count'
  | 'positive_rate'
  | 'negative_rate'
  | 'total_call_count'
  | 'total_positive_feedback_count'
  | 'total_negative_feedback_count'
  | 'total_positive_rate'
  | 'total_negative_rate'
  | 'recent_call_count'
  | 'recent_positive_feedback_count'
  | 'recent_negative_feedback_count'
  | 'recent_positive_rate'
  | 'recent_negative_rate';

export default function SkillsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [rows, setRows] = useState<SkillRead[]>([]);
  const [versionRows, setVersionRows] = useState<SkillVersionRead[]>([]);
  const [versionSkill, setVersionSkill] = useState<SkillRead | null>(null);
  const [detailVersion, setDetailVersion] = useState<SkillVersionRead | null>(null);
  const [rankingModal, setRankingModal] = useState<RankingModalState | null>(null);
  const [positiveScope, setPositiveScope] = useState<RankingScope>('current');
  const [negativeScope, setNegativeScope] = useState<RankingScope>('current');
  const [versionModalOpen, setVersionModalOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [isOverallAgent, setIsOverallAgent] = useState(() => {
    const stored = window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '';
    return !stored || stored.includes('overall');
  });
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState<SkillStatusFilter>('all');
  const [branchFilter, setBranchFilter] = useState<BranchFilter>('all');
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const currentAgent = useMemo(() => agents.find((item) => item.id === agentId), [agents, agentId]);
  const canManageCurrentScope = currentAgent
    ? canManageEmployeeAgent(currentAgent, currentUser)
    : isEnterpriseAdmin(currentUser) && isOverallAgent;
  const [importOpen, setImportOpen] = useState(false);
  const [importMode, setImportMode] = useState<'plaza' | 'employee'>('plaza');
  const [importSourceAgentId, setImportSourceAgentId] = useState('');
  const [importSourceSkills, setImportSourceSkills] = useState<SkillRead[]>([]);
  const [importSelectedSkillIds, setImportSelectedSkillIds] = useState<string[]>([]);
  const [importLoading, setImportLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SkillRead | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<SkillVersionRead | null>(null);
  const [promoteTarget, setPromoteTarget] = useState<SkillRead | null>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const suffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const result = await api.get<SkillRead[]>(`/api/enterprise/skills?tenant_id=${TENANT_ID}${suffix}`);
      setRows(result);
      const agentRows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(agentRows);
      setIsOverallAgent(Boolean(agentRows.find((item) => item.id === agentId)?.is_overall ?? true));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  useEffect(() => {
    if (searchParams.get('add') !== 'plaza') return;
    if (agents.length === 0) return;
    const resourceId = searchParams.get('resourceId') || undefined;
    if (isOverallAgent) {
      notify.warning('请先选择一个数字员工，再从广场复制 SOP');
    } else {
      void openImport('plaza', resourceId);
    }
    const next = new URLSearchParams(searchParams);
    next.delete('add');
    next.delete('resourceId');
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents.length, isOverallAgent, searchParams, setSearchParams]);

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
      const matchesKeyword =
        !keyword ||
        [
          row.name,
          row.skill_id,
          row.business_domain || '',
          row.description || '',
          row.version,
          resourceCreatorName(row),
        ].some((value) => value.toLowerCase().includes(keyword));
      const matchesStatus = statusFilter === 'all' || row.status === statusFilter;
      const branchState = row.branch_status === 'inactive' ? 'inactive' : row.branch_sync_state || 'synced';
      const matchesBranch = isOverallAgent || branchFilter === 'all' || branchState === branchFilter;
      return matchesKeyword && matchesStatus && matchesBranch;
    });
  }, [branchFilter, isOverallAgent, rows, searchText, statusFilter]);

  const pagination = useClientPagination(filteredRows, SKILL_PAGE_SIZE, `${searchText}|${statusFilter}|${branchFilter}`);

  const rankingRows = useMemo(
    () => ({
      calls: rankByMetric(rows, 'total_call_count'),
      positiveCurrent: rankByMetric(rows, 'positive_rate', 'positive_feedback_count', 'call_count'),
      positiveTotal: rankByMetric(rows, 'total_positive_rate', 'total_positive_feedback_count', 'total_call_count'),
      negativeCurrent: rankByMetric(rows, 'negative_rate', 'negative_feedback_count', 'call_count'),
      negativeTotal: rankByMetric(rows, 'total_negative_rate', 'total_negative_feedback_count', 'total_call_count'),
    }),
    [rows],
  );

  const positiveRankingRows = positiveScope === 'current' ? rankingRows.positiveCurrent : rankingRows.positiveTotal;
  const negativeRankingRows = negativeScope === 'current' ? rankingRows.negativeCurrent : rankingRows.negativeTotal;
  const rankingModalRows = rankingModal ? rankingRowsFor(rankingRows, rankingModal.mode, rankingModal.scope) : [];
  const rankingPagination = useClientPagination(rankingModalRows, RANKING_PAGE_SIZE, rankingModal);

  const columns: DataTableColumn<SkillRead>[] = [
    {
      key: 'name',
      title: 'SOP 名称',
      width: 170,
      className: 'text-[#18181a]',
      render: (row) => (
        <span className="block truncate" title={row.name}>
          {row.name}
        </span>
      ),
    },
    {
      key: 'skill_id',
      title: 'SOP ID',
      width: 170,
      render: (row) => (
        <span className="block truncate" title={row.skill_id}>
          {row.skill_id}
        </span>
      ),
    },
    {
      key: 'business_domain',
      title: '业务域',
      width: 120,
      render: (row) => <span className="block truncate">{row.business_domain || '-'}</span>,
    },
    { key: 'version', title: '版本', width: 80, render: (row) => row.version },
    {
      key: 'branch',
      title: '本地版本',
      width: 110,
      render: (row) => renderBranchBadge(row, isOverallAgent),
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
    { key: 'call_count', title: '调用次数', width: 90, render: (row) => `${row.call_count || 0} 次` },
    { key: 'positive_rate', title: '好评率', width: 90, render: (row) => percent(row.positive_rate) },
    { key: 'negative_rate', title: '差评率', width: 90, render: (row) => percent(row.negative_rate) },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => renderActions(row),
    },
  ];

  function renderActions(row: SkillRead) {
    if (isOverallAgent && !canManageCurrentScope) {
      return (
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="SOP 操作"
            className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
          >
            <IconMore className="size-3.5" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void openVersions(row)}>
              <IconHistory />
              版本管理
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      );
    }
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="SOP 操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => openEdit(row)}>
            <IconEdit />
            {isOverallAgent ? '编辑' : '编辑本地版本'}
          </DropdownMenuItem>
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void openVersions(row)}>
            <IconHistory />
            版本管理
          </DropdownMenuItem>
          {isOverallAgent && row.status !== 'draft' && (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void markDraft(row)}>
              <IconEdit />
              转为草稿
            </DropdownMenuItem>
          )}
          {row.status === 'published' ? (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void archive(row)}>
              <Ban />
              {isOverallAgent ? '停用' : '停用本地版本'}
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void publish(row)}>
              <CircleCheck />
              {isOverallAgent ? '启用' : '启用本地版本'}
            </DropdownMenuItem>
          )}
          {!isOverallAgent && (
            <>
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void syncFromOverall(row)}>
                <IconRefresh />
                从广场同步
              </DropdownMenuItem>
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => setPromoteTarget(row)}>
                <Upload />
                发布到广场
              </DropdownMenuItem>
            </>
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

  const renderMobileCard = (row: SkillRead) => {
    const preset = STATUS_BADGE[row.status] || { tone: 'gray' as BadgeTone, text: row.status };
    return (
      <article className={MOBILE_CARD_CLASS} key={row.id}>
        <div className="flex min-w-0 items-start justify-between gap-[10px]">
          <div className="min-w-0">
            <strong className="block truncate text-[14px] font-semibold text-[#18181a]">{row.name}</strong>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.skill_id}</span>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">创建者：{resourceCreatorName(row) || '-'}</span>
          </div>
          {renderActions(row)}
        </div>
        <div className="mt-[10px] flex flex-wrap items-center gap-[4px]">
          <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>
          {renderBranchBadge(row, isOverallAgent)}
          {row.business_domain && <StatusBadge tone="gray">{row.business_domain}</StatusBadge>}
        </div>
        <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
          <span>调用 {row.call_count || 0} 次</span>
          <span>
            好评 {percent(row.positive_rate)} · 差评 {percent(row.negative_rate)}
          </span>
        </div>
      </article>
    );
  };

  async function openImport(mode: 'plaza' | 'employee' = 'plaza', selectedResourceId?: string) {
    try {
      const agentRows = agents.length
        ? agents
        : await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(agentRows);
      setImportMode(mode);
      const firstSource = mode === 'plaza'
        ? openGalleryAgentId(agentRows)
        : visibleEmployeeAgents(agentRows, currentUser, { activeOnly: true, excludeAgentId: agentId })[0]?.id || '';
      setImportSourceAgentId(firstSource);
      setImportSelectedSkillIds([]);
      setImportOpen(true);
      if (firstSource) {
        const sourceRows = await loadImportSourceSkills(firstSource);
        if (selectedResourceId && sourceRows.some((item) => item.id === selectedResourceId)) {
          setImportSelectedSkillIds([selectedResourceId]);
        }
      } else {
        setImportSourceSkills([]);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    }
  }

  async function loadImportSourceSkills(sourceAgentId: string): Promise<SkillRead[]> {
    setImportSourceSkills([]);
    setImportSelectedSkillIds([]);
    if (!sourceAgentId) return [];
    try {
      const sourceRows = await api.get<SkillRead[]>(`/api/enterprise/agents/${sourceAgentId}/skills?tenant_id=${TENANT_ID}`);
      const publishedRows = sourceRows.filter((item) => item.status === 'published');
      setImportSourceSkills(publishedRows);
      return publishedRows;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载来源 SOP 失败');
      return [];
    }
  }

  async function submitImportSkills() {
    if (!agentId) {
      notify.warning('请先选择一个数字员工');
      return;
    }
    if (!importSourceAgentId) {
      notify.warning(importMode === 'plaza' ? '请选择开放广场' : '请选择复制来源员工');
      return;
    }
    if (importSelectedSkillIds.length === 0) {
      notify.warning('请选择要复制的 SOP');
      return;
    }
    setImportLoading(true);
    try {
      const result = await api.post<{ imported: Array<Record<string, unknown>>; missing: Array<Record<string, unknown>> }>(
        `/api/enterprise/agents/${agentId}/resources/import`,
        {
          tenant_id: TENANT_ID,
          source_agent_id: importSourceAgentId,
          resource_type: 'skill',
          resource_ids: importSelectedSkillIds,
        },
      );
      const importedCount = result.imported?.length || 0;
      const missingCount = result.missing?.length || 0;
      notify.success(`已复制 ${importedCount} 个 SOP${missingCount ? `，${missingCount} 个未复制` : ''}`);
      setImportOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制失败');
    } finally {
      setImportLoading(false);
    }
  }

  function openCreate() {
    navigate(`/enterprise/skills/distill?mode=create${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ''}`);
  }

  function openEdit(row: SkillRead) {
    const suffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    navigate(`/enterprise/skills/distill?skill_id=${encodeURIComponent(row.skill_id)}${suffix}`);
  }

  async function publish(row: SkillRead) {
    try {
      await api.post(`/api/enterprise/skills/${row.skill_id}/publish?tenant_id=${TENANT_ID}${agentQuery()}`);
      notify.success('已启用');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '启用失败');
    }
  }

  async function archive(row: SkillRead) {
    try {
      await api.post(`/api/enterprise/skills/${row.skill_id}/archive?tenant_id=${TENANT_ID}${agentQuery()}`);
      notify.success('已停用');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '停用失败');
    }
  }

  async function markDraft(row: SkillRead) {
    try {
      await api.post(`/api/enterprise/skills/${row.skill_id}/draft?tenant_id=${TENANT_ID}${agentQuery()}`);
      notify.success('已转为草稿');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '转为草稿失败');
    }
  }

  async function openVersions(row: SkillRead) {
    setVersionSkill(row);
    setVersionModalOpen(true);
    setVersionRows([]);
    try {
      const result = await api.get<SkillVersionRead[]>(
        `/api/enterprise/skills/${encodeURIComponent(row.skill_id)}/versions?tenant_id=${TENANT_ID}${agentQuery()}`,
      );
      setVersionRows(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载版本失败');
    }
  }

  async function showVersionDetail(row: SkillVersionRead) {
    try {
      const result = await api.get<SkillVersionRead>(
        `/api/enterprise/skills/${encodeURIComponent(row.skill_id)}/versions/${encodeURIComponent(row.version)}?tenant_id=${TENANT_ID}${agentQuery()}`,
      );
      setDetailVersion(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载版本详情失败');
    }
  }

  async function syncFromOverall(row: SkillRead) {
    if (!agentId) return;
    try {
      await api.post(
        `/api/enterprise/agents/${agentId}/skills/${encodeURIComponent(row.skill_id)}/sync-from-overall?tenant_id=${TENANT_ID}`,
      );
      notify.success('已从广场同步');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '同步失败');
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    const branchMode = !isOverallAgent;
    setConfirmLoading(true);
    try {
      await api.delete(`/api/enterprise/skills/${row.skill_id}?tenant_id=${TENANT_ID}${agentQuery()}`);
      notify.success(branchMode ? '已移除' : '已删除');
      setDeleteTarget(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : branchMode ? '移除失败' : '删除失败');
    } finally {
      setConfirmLoading(false);
    }
  }

  async function confirmRollback() {
    const row = rollbackTarget;
    if (!row) return;
    setConfirmLoading(true);
    try {
      const result = await api.post<SkillRead>(
        `/api/enterprise/skills/${encodeURIComponent(row.skill_id)}/versions/${encodeURIComponent(row.version)}/rollback?tenant_id=${TENANT_ID}${agentQuery()}`,
      );
      notify.success(`已回滚到 ${row.version}`);
      setRollbackTarget(null);
      await load();
      await openVersions(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '回滚失败');
    } finally {
      setConfirmLoading(false);
    }
  }

  async function confirmPromote() {
    const row = promoteTarget;
    if (!row || !agentId) return;
    setConfirmLoading(true);
    try {
      await api.post(
        `/api/enterprise/agents/${agentId}/skills/${encodeURIComponent(row.skill_id)}/promote-to-overall?tenant_id=${TENANT_ID}`,
      );
      notify.success('已发布到广场');
      setPromoteTarget(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发布失败');
    } finally {
      setConfirmLoading(false);
    }
  }

  function agentQuery() {
    return agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
  }

  const listEmptyText = isOverallAgent
    ? canManageCurrentScope ? '暂无 SOP，点击「新增」创建一个吧' : '暂无 SOP'
    : '当前员工暂无本地 SOP';

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="SOP" />

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
            <DropdownMenuTrigger data-guide-target="sop-create" className="flex h-[34px] items-center gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white outline-none transition-colors hover:bg-[#303030]">
              <IconAdd className="size-[14px]" />
              新增
              <IconChevronDown className="size-[12px]" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => openCreate()}>
                <IconAdd />
                新建空白 SOP
              </DropdownMenuItem>
              {!isOverallAgent && (
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void openImport('plaza')}>
                  <Copy />
                  从广场复制
                </DropdownMenuItem>
              )}
              {!isOverallAgent && (
                <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void openImport('employee')}>
                  <Users />
                  从数字员工复制
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconClipboard className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">{isOverallAgent ? 'SOP 广场列表' : '本地 SOP'}</span>
          </div>

          <div className="flex flex-wrap items-center gap-[16px]">
            <label className="flex h-[34px] w-[260px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
              <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
              <input
                value={searchText}
                placeholder="搜索 SOP 名称、ID、业务域"
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
            <Select value={statusFilter} onValueChange={(value) => setStatusFilter(value as SkillStatusFilter)}>
              <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-[130px]')} aria-label="状态筛选">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部状态</SelectItem>
                <SelectItem value="published">已启用</SelectItem>
                <SelectItem value="draft">草稿</SelectItem>
                <SelectItem value="archived">已停用</SelectItem>
              </SelectContent>
            </Select>
            {!isOverallAgent && (
              <Select value={branchFilter} onValueChange={(value) => setBranchFilter(value as BranchFilter)}>
                <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-[130px]')} aria-label="版本筛选">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部版本</SelectItem>
                  <SelectItem value="synced">已同步</SelectItem>
                  <SelectItem value="diverged">本地版本</SelectItem>
                  <SelectItem value="inactive">已停用</SelectItem>
                </SelectContent>
              </Select>
            )}
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
              aria-label="SOP 列表"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText={listEmptyText}
            />
          </div>

          {filteredRows.length > 0 && (
            <Paginator
              aria-label="SOP 分页"
              className="mt-0 mb-[6px]"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </div>

        <div className="grid grid-cols-1 gap-[16px] lg:grid-cols-3">
          <RankingCard
            title="调用排行"
            rows={rankingRows.calls.slice(0, 5)}
            value={(row) => `${row.total_call_count || 0} 次`}
            onMore={() => setRankingModal({ mode: 'calls', scope: 'total' })}
          />
          <RankingCard
            title="好评 SOP"
            rows={positiveRankingRows.slice(0, 5)}
            value={(row) => percent(positiveScope === 'current' ? row.positive_rate : row.total_positive_rate)}
            version={(row) => rankingVersionText(row, positiveScope)}
            scope={positiveScope}
            onScopeChange={setPositiveScope}
            onMore={() => setRankingModal({ mode: 'positive', scope: positiveScope })}
          />
          <RankingCard
            title="待改进 SOP"
            rows={negativeRankingRows.slice(0, 5)}
            value={(row) => percent(negativeScope === 'current' ? row.negative_rate : row.total_negative_rate)}
            version={(row) => rankingVersionText(row, negativeScope)}
            scope={negativeScope}
            onScopeChange={setNegativeScope}
            onMore={() => setRankingModal({ mode: 'negative', scope: negativeScope })}
          />
        </div>
      </div>

      <ResourceImportDialog
        open={importOpen}
        loading={importLoading}
        icon={<IconSkill className="size-[14px] shrink-0" />}
        title={importMode === 'plaza' ? '从广场复制 SOP' : '从数字员工复制 SOP'}
        sourcePlaceholder={importMode === 'plaza' ? '选择开放广场' : '选择复制来源'}
        sources={importMode === 'plaza'
          ? openGalleryImportSourceOptions(agents, '开放广场')
          : visibleEmployeeAgents(agents, currentUser, { activeOnly: true, excludeAgentId: agentId })
            .map((item) => ({ value: item.id, label: item.name }))}
        sourceId={importSourceAgentId}
        itemsLabel="选择 SOP"
        items={importSourceSkills.map((item) => ({
          id: item.id,
          label: (
            <>
              {item.name}
              <span className="text-[#858b9c]"> · {item.skill_id}</span>
            </>
          ),
        }))}
        selectedIds={importSelectedSkillIds}
        emptyText="没有可复制的 SOP"
        note={
          importMode === 'plaza'
            ? '从开放广场复制可用 SOP；不可复制内容不会出现在列表。'
            : '从数字员工复制可用 SOP；不可见内容不会出现在列表。'
        }
        onSourceChange={(value) => {
          setImportSourceAgentId(value);
          void loadImportSourceSkills(value);
        }}
        onSelectedChange={setImportSelectedSkillIds}
        onClose={() => setImportOpen(false)}
        onSubmit={() => void submitImportSkills()}
      />

      <RankingDialog
        modal={rankingModal}
        rows={rankingPagination.pagedItems}
        page={rankingPagination.page}
        pageCount={rankingPagination.pageCount}
        onPageChange={rankingPagination.setPage}
        total={rankingModalRows.length}
        onClose={() => setRankingModal(null)}
      />

      <VersionsDialog
        open={versionModalOpen}
        skill={versionSkill}
        rows={versionRows}
        loading={loading}
        onDetail={(row) => void showVersionDetail(row)}
        onRollback={(row) => setRollbackTarget(row)}
        onClose={() => {
          setVersionModalOpen(false);
          setVersionSkill(null);
        }}
      />

      <VersionDetailDialog detail={detailVersion} onClose={() => setDetailVersion(null)} />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        loading={confirmLoading}
        title={
          deleteTarget
            ? `${isOverallAgent ? '删除' : '移除'} SOP「${deleteTarget.name}」？`
            : ''
        }
        description={
          isOverallAgent
            ? '删除后不会移除历史对话记录，但组织 SOP 列表中将不再显示该流程。'
            : '这只会在当前数字员工中隐藏该 SOP；开放广场和其他数字员工仍然保留。'
        }
        confirmText={isOverallAgent ? '删除' : '移除'}
        onConfirm={() => void confirmDelete()}
      />

      <ConfirmDialog
        open={Boolean(rollbackTarget)}
        onOpenChange={(open) => !open && setRollbackTarget(null)}
        loading={confirmLoading}
        destructive={false}
        title={rollbackTarget ? `回滚到版本 ${rollbackTarget.version}？` : ''}
        description={
          rollbackTarget
            ? `当前 SOP 将切换为「${rollbackTarget.name}」的 ${rollbackTarget.version} 版本内容，历史对话和反馈数据不会被删除。`
            : ''
        }
        confirmText="回滚"
        onConfirm={() => void confirmRollback()}
      />

      <ConfirmDialog
        open={Boolean(promoteTarget)}
        onOpenChange={(open) => !open && setPromoteTarget(null)}
        loading={confirmLoading}
        destructive={false}
        title={promoteTarget ? `将「${promoteTarget.name}」发布到广场？` : ''}
        description="这会把当前数字员工的本地版本发布为广场可复用的 SOP 新版本。"
        confirmText="发布"
        onConfirm={() => void confirmPromote()}
      />
    </div>
  );
}

function renderBranchBadge(row: SkillRead, isOverallAgent: boolean) {
  if (isOverallAgent) return <StatusBadge tone="gray">广场版</StatusBadge>;
  if (row.branch_status === 'inactive') return <StatusBadge tone="gray">已停用</StatusBadge>;
  const state = row.branch_sync_state || 'synced';
  return state === 'diverged' ? (
    <StatusBadge tone="orange">本地版本</StatusBadge>
  ) : (
    <StatusBadge tone="green">已同步</StatusBadge>
  );
}

function ScopeToggle({ value, onChange }: { value: RankingScope; onChange: (scope: RankingScope) => void }) {
  return (
    <div className="inline-flex items-center rounded-[8px] bg-[#f2f3f7] p-[2px]">
      {(['current', 'total'] as RankingScope[]).map((scope) => (
        <button
          key={scope}
          type="button"
          onClick={() => onChange(scope)}
          className={cn(
            'rounded-[6px] px-[10px] py-[3px] text-[11px] leading-none transition-colors',
            value === scope
              ? 'bg-white text-[#18181a] shadow-sm'
              : 'text-[#858b9c] hover:text-[#18181a]',
          )}
        >
          {scope === 'current' ? '当前' : '总榜'}
        </button>
      ))}
    </div>
  );
}

function RankingCard({
  title,
  rows,
  value,
  version,
  scope,
  onScopeChange,
  onMore,
}: {
  title: string;
  rows: RankedSkill[];
  value: (row: RankedSkill) => string;
  version?: (row: RankedSkill) => string;
  scope?: RankingScope;
  onScopeChange?: (scope: RankingScope) => void;
  onMore: () => void;
}) {
  return (
    <section className="flex flex-col rounded-[14px] border border-[#eef0f4] bg-white p-[16px]">
      <header className="mb-[8px] flex items-center justify-between gap-[8px]">
        <span className="text-[13px] font-medium text-[#18181a]">{title}</span>
        <div className="flex items-center gap-[8px]">
          {scope && onScopeChange && <ScopeToggle value={scope} onChange={onScopeChange} />}
          <button
            type="button"
            onClick={onMore}
            className="text-[12px] text-[#1a71ff] transition-colors hover:text-[#4a8dff]"
          >
            查看更多
          </button>
        </div>
      </header>
      {rows.length === 0 ? (
        <div className="py-[28px] text-center text-[12px] text-[#858b9c]">暂无数据</div>
      ) : (
        <div className="flex flex-col">
          {rows.map((row) => (
            <div
              key={`${title}_${row.skill_id}`}
              className="flex items-center gap-[10px] border-b border-[#f2f3f7] py-[9px] last:border-0"
            >
              <span className="grid size-[20px] shrink-0 place-items-center rounded-[6px] bg-[#f6f6f6] text-[11px] leading-none text-[#464c5e]">
                {row.rank}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12px] text-[#18181a]" title={row.name}>
                  {row.name}
                </div>
                {version && <div className="text-[11px] text-[#858b9c]">{version(row)}</div>}
              </div>
              <strong className="shrink-0 text-[12px] font-medium text-[#18181a]">{value(row)}</strong>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function RankingDialog({
  modal,
  rows,
  page,
  pageCount,
  onPageChange,
  total,
  onClose,
}: {
  modal: RankingModalState | null;
  rows: RankedSkill[];
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
  total: number;
  onClose: () => void;
}) {
  const mode = modal?.mode || 'calls';
  const scope = modal?.scope || 'total';
  const columns: DataTableColumn<RankedSkill>[] = [
    { key: 'rank', title: '排名', width: 60, render: (row) => row.rank },
    {
      key: 'name',
      title: 'SOP 名称',
      width: 180,
      className: 'text-[#18181a]',
      render: (row) => (
        <span className="block min-[180px]" title={row.name}>
          {row.name}
        </span>
      ),
    },
    {
      key: 'skill_id',
      width: 80,
      title: 'SOP ID',
      render: (row) => (
        <span className="block truncate" title={row.skill_id}>
          {row.skill_id}
        </span>
      ),
    },
    { key: 'version', title: scope === 'current' ? '版本' : '版本范围', width: 110, render: (row) => rankingVersionText(row, scope) },
    {
      key: 'domain',
      title: '业务域',
      width: 120,
      render: (row) => <span className="block truncate">{row.business_domain || '-'}</span>,
    },
    { key: 'metric', title: rankingMetricTitle(mode, scope), width: 120, render: (row) => rankingMetricValue(row, mode, scope) },
    { key: 'calls', title: '调用次数', render: (row) => `${rankingCalls(row, scope)} 次` },
    { key: 'pos', title: '好评率', render: (row) => percent(rankingPositiveRate(row, scope)) },
    { key: 'neg', title: '差评率', render: (row) => percent(rankingNegativeRate(row, scope)) },
    { key: 'fb', title: '反馈数', render: (row) => rankingFeedbackText(row, scope) },
  ];
  return (
    <Dialog open={Boolean(modal)} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[1000px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconSkill className="size-[14px] shrink-0" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {rankingTitle(mode, scope)}
          </DialogTitle>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[12px]">
          <div className="overflow-x-auto">
            <DataTable
              aria-label="SOP 排行"
              className="min-w-[900px]"
              columns={columns}
              data={rows}
              rowKey={(row) => row.skill_id}
              emptyText="暂无数据"
            />
          </div>
          {total > 0 && (
            <Paginator aria-label="排行分页" page={page} pageCount={pageCount} onChange={onPageChange} />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function VersionsDialog({
  open,
  skill,
  rows,
  loading,
  onDetail,
  onRollback,
  onClose,
}: {
  open: boolean;
  skill: SkillRead | null;
  rows: SkillVersionRead[];
  loading: boolean;
  onDetail: (row: SkillVersionRead) => void;
  onRollback: (row: SkillVersionRead) => void;
  onClose: () => void;
}) {
  const columns: DataTableColumn<SkillVersionRead>[] = [
    { key: 'version', title: '版本', width: 100, className: 'text-[#18181a]', render: (row) => row.version },
    {
      key: 'name',
      title: 'SOP 名称',
      render: (row) => (
        <span className="block truncate" title={row.name}>
          {row.name}
        </span>
      ),
    },
    {
      key: 'domain',
      title: '业务域',
      width: 130,
      render: (row) => <span className="block truncate">{row.business_domain || '-'}</span>,
    },
    { key: 'calls', title: '调用次数', width: 100, render: (row) => `${row.call_count || 0} 次` },
    { key: 'pos', title: '好评率', width: 90, render: (row) => percent(row.positive_rate) },
    { key: 'neg', title: '差评率', width: 90, render: (row) => percent(row.negative_rate) },
    { key: 'updated', title: '更新时间', width: 120, render: (row) => row.updated_at.slice(0, 10) },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => {
        const isCurrent = row.version === skill?.version;
        return (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label="版本操作"
              className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
            >
              <IconMore className="size-3.5" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => onDetail(row)}>
                <Eye />
                查看详情
              </DropdownMenuItem>
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                disabled={isCurrent}
                onSelect={() => onRollback(row)}
              >
                <RotateCcw />
                {isCurrent ? '当前版本' : '回滚到此版本'}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        );
      },
    },
  ];
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[960px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconHistory className="size-[14px] shrink-0" />
          <DialogTitle className="min-w-0 truncate text-[14px] font-normal leading-none text-[#757f9c]">
            版本管理{skill ? `：${skill.name}` : ''}
          </DialogTitle>
        </div>

        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-[12px]">
          <div className="overflow-x-auto">
            <DataTable
              aria-label="SOP 版本"
              className="min-w-[820px]"
              columns={columns}
              data={rows}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无版本记录"
            />
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function VersionDetailDialog({
  detail,
  onClose,
}: {
  detail: SkillVersionRead | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={Boolean(detail)} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[900px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconSkill className="size-[14px] shrink-0" />
          <DialogTitle className="min-w-0 truncate text-[14px] font-normal leading-none text-[#757f9c]">
            {detail ? `版本详情：${detail.name} / ${detail.version}` : '版本详情'}
          </DialogTitle>
        </div>

        {detail && (
          <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[12px]">
            <div className="grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1">
              {/* <DetailField label="SOP ID">{detail.skill_id}</DetailField> */}
              <DetailField label="版本">{detail.version}</DetailField>
              <DetailField label="业务域">{detail.business_domain || '-'}</DetailField>
              <DetailField label="状态">{statusText(detail.status)}</DetailField>
              <DetailField label="调用次数">{detail.call_count || 0} 次</DetailField>
              <DetailField label="好评率">{percent(detail.positive_rate)}</DetailField>
              <DetailField label="差评率">{percent(detail.negative_rate)}</DetailField>
              <DetailField label="更新时间">{detail.updated_at.slice(0, 10)}</DetailField>
            </div>
            <pre className="overflow-x-auto rounded-[12px] bg-[#f6f6f6] p-[14px] text-[12px] leading-[1.7] text-[#464c5e] wrap-anywhere whitespace-pre-wrap">
              {skillSourceText(detail)}
            </pre>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function rankByMetric(
  rows: SkillRead[],
  field: NumericSkillMetric,
  tieBreaker?: NumericSkillMetric,
  callTieBreaker: NumericSkillMetric = 'total_call_count',
): RankedSkill[] {
  return [...rows]
    .sort((a, b) => {
      const primary = (b[field] || 0) - (a[field] || 0);
      if (primary !== 0) return primary;
      if (tieBreaker) {
        const secondary = (b[tieBreaker] || 0) - (a[tieBreaker] || 0);
        if (secondary !== 0) return secondary;
      }
      return (b[callTieBreaker] || 0) - (a[callTieBreaker] || 0);
    })
    .map((row, index) => ({ ...row, rank: index + 1 }));
}

function percent(value: number | undefined): string {
  return `${Math.round((value || 0) * 100)}%`;
}

function rankingTitle(mode: RankingMode, scope: RankingScope): string {
  if (mode === 'calls') return '完整排行：全历史调用';
  if (mode === 'positive') return scope === 'current' ? '完整排行：当前版本好评率' : '完整排行：历史总榜好评率';
  return scope === 'current' ? '完整排行：当前版本差评率' : '完整排行：历史总榜差评率';
}

function rankingRowsFor(
  rows: {
    calls: RankedSkill[];
    positiveCurrent: RankedSkill[];
    positiveTotal: RankedSkill[];
    negativeCurrent: RankedSkill[];
    negativeTotal: RankedSkill[];
  },
  mode: RankingMode,
  scope: RankingScope,
): RankedSkill[] {
  if (mode === 'calls') return rows.calls;
  if (mode === 'positive') return scope === 'current' ? rows.positiveCurrent : rows.positiveTotal;
  return scope === 'current' ? rows.negativeCurrent : rows.negativeTotal;
}

function rankingVersionText(row: SkillRead, scope: RankingScope): string {
  return scope === 'current' ? `v${row.version}` : '全版本';
}

function rankingMetricTitle(mode: RankingMode, scope: RankingScope): string {
  if (mode === 'calls') return '全历史调用';
  if (mode === 'positive') return scope === 'current' ? '当前好评率' : '总好评率';
  return scope === 'current' ? '当前差评率' : '总差评率';
}

function rankingMetricValue(row: SkillRead, mode: RankingMode, scope: RankingScope): string {
  if (mode === 'calls') return `${row.total_call_count || 0} 次`;
  if (mode === 'positive') return percent(scope === 'current' ? row.positive_rate : row.total_positive_rate);
  return percent(scope === 'current' ? row.negative_rate : row.total_negative_rate);
}

function rankingCalls(row: SkillRead, scope: RankingScope): number {
  return scope === 'current' ? row.call_count || 0 : row.total_call_count || 0;
}

function rankingPositiveRate(row: SkillRead, scope: RankingScope): number {
  return scope === 'current' ? row.positive_rate || 0 : row.total_positive_rate || 0;
}

function rankingNegativeRate(row: SkillRead, scope: RankingScope): number {
  return scope === 'current' ? row.negative_rate || 0 : row.total_negative_rate || 0;
}

function rankingFeedbackText(row: SkillRead, scope: RankingScope): string {
  if (scope === 'current') {
    return `${row.positive_feedback_count || 0}/${row.negative_feedback_count || 0}`;
  }
  return `${row.total_positive_feedback_count || 0}/${row.total_negative_feedback_count || 0}`;
}

function statusText(status: string): string {
  return STATUS_BADGE[status as SkillRead['status']]?.text || status;
}

function skillSourceText(row: SkillVersionRead): string {
  const skill = row.content;
  const nodes = skillGraphSteps(skill);
  return [
    `# ${skill.name}`,
    `- skill_id: ${skill.skill_id}`,
    `- version: ${skill.version}`,
    `- business_domain: ${skill.business_domain || '-'}`,
    `- description: ${skill.description || '-'}`,
    `- trigger_intents: ${formatList(skill.trigger_intents)}`,
    `- user_utterance_examples: ${formatList(skill.user_utterance_examples)}`,
    `- goal: ${formatList(skill.goal)}`,
    `- required_info: ${formatList(skill.required_info)}`,
    `- response_rules: ${formatList(skill.response_rules)}`,
    '',
    '## 详细节点',
    ...nodes.flatMap((step, index) => [
      '',
      `### 节点 ${index + 1}: ${String(step.name || step.node_id || '-')}`,
      `- node_id: ${String(step.node_id || '-')}`,
      `- node_type: ${String(step.type || 'collect_info')}`,
      `- condition: ${String(step.condition || '-')}`,
      `- instruction: ${String(step.instruction || '-')}`,
      `- expected_user_info: ${formatList(step.expected_user_info)}`,
      `- allowed_actions: ${formatList(step.allowed_actions)}`,
    ]),
  ].join('\n');
}

function skillGraphSteps(skill: SkillVersionRead['content']): Array<Record<string, unknown>> {
  if (Array.isArray(skill.nodes) && skill.nodes.length > 0) {
    return skill.nodes.map((node, index) => ({
      node_id: node.node_id || `node_${index + 1}`,
      type: node.type || 'collect_info',
      condition: node.condition || '',
      name: node.name || node.node_id || `节点 ${index + 1}`,
      instruction: node.instruction || '',
      expected_user_info: Array.isArray(node.expected_user_info) ? node.expected_user_info : [],
      allowed_actions: Array.isArray(node.allowed_actions) ? node.allowed_actions : [],
    }));
  }
  return [];
}

function formatList(value: unknown): string {
  if (!Array.isArray(value) || value.length === 0) return '-';
  return value.map(String).join(', ');
}
