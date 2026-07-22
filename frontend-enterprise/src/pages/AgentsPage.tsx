import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import IconPlus from '../assets/icons/plus.svg?react';
import IconSearch from '../assets/icons/search.svg?react';

import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeCard from '../components/EmployeeCard';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import {
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
} from '../employee';
import { emitAgentScopeChange, persistSharedAgentScope } from '../lib/agent-scope-storage';
import type { AgentProfileRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

export default function AgentsPage({
  currentUser,
  isAdmin = false,
  onCreateAgent,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onCreateAgent?: () => void;
  onLogout?: () => void;
}) {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [avatarAgent, setAvatarAgent] = useState<AgentProfileRead | null>(null);
  const [profileAgent, setProfileAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [selectingAgentId, setSelectingAgentId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [employeeFilter, setEmployeeFilter] = useState<'all' | 'online' | 'offline' | 'pending'>('all');
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY),
  );
  const navigate = useNavigate();

  async function load() {
    setLoading(true);
    try {
      const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ agentId?: string }>).detail;
      setSelectedAgentId(detail?.agentId ?? window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY));
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', handler);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', handler);
  }, []);

  const employees = useMemo(
    () => agents.filter((item) => !item.is_overall && canManageEmployeeAgent(item, currentUser)),
    [agents, currentUser],
  );
  const offlineEmployees = employees.filter((item) => item.status !== 'active');
  const onlineEmployees = employees.filter((item) => item.status === 'active');
  const pendingEmployees = employees.filter((item) => {
    const metadata = item.metadata || {};
    return item.status === 'pending'
      || metadata.review_status === 'pending'
      || metadata.approval_status === 'pending'
      || metadata.audit_status === 'pending';
  });
  const filteredEmployees = employees.filter((item) => {
    const profile = employeeProfile(item);
    const keyword = searchTerm.trim().toLowerCase();
    const matchesFilter = employeeFilter === 'all'
      || (employeeFilter === 'online' && item.status === 'active')
      || (employeeFilter === 'offline' && item.status !== 'active')
      || (employeeFilter === 'pending' && pendingEmployees.includes(item));
    if (!matchesFilter) return false;
    if (!keyword) return true;
    return [
      employeeDisplayName(item),
      employeeDisplayNameWithCreator(item),
      profile.roleName,
      item.description || '',
      profile.workStyles.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword));
  });

  async function selectEmployee(row: AgentProfileRead) {
    if (selectingAgentId) return;
    setSelectingAgentId(row.id);
    try {
      let selectedRow = row;
      if (!canSelectCurrentEmployeeAgent(row, currentUser, { activeOnly: true })) {
        selectedRow = await api.post<AgentProfileRead>(
          `/api/chat/agents/${encodeURIComponent(row.id)}/use?tenant_id=${TENANT_ID}`,
          {},
        );
        updateAgentInList(selectedRow);
      }
      setSelectedAgentId(selectedRow.id);
      persistSharedAgentScope(selectedRow.id, currentUser?.id);
      emitAgentScopeChange(selectedRow.id);
      navigate('/enterprise/dashboard');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setSelectingAgentId(null);
    }
  }

  function startEmployeeChat(row: AgentProfileRead) {
    navigate(`/workspace/chat/draft/${row.id}`);
  }

  async function updateStatus(row: AgentProfileRead, status: 'active' | 'archived') {
    try {
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: TENANT_ID,
        status,
        metadata: row.metadata || {},
      });
      notify.success(status === 'active' ? '员工已上线' : '员工已下线');
      await load();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新员工状态失败');
    }
  }

  async function updateGalleryState(row: AgentProfileRead, published: boolean) {
    try {
      const metadata = {
        ...(row.metadata || {}),
        published_to_gallery: published,
        gallery_published_at: published ? new Date().toISOString() : undefined,
        gallery_published_by: published ? currentUser?.username : undefined,
      };
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: TENANT_ID,
        metadata,
      });
      notify.success(published ? '已发布到广场' : '已从广场下架');
      await load();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新广场状态失败');
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/agents/${row.id}?tenant_id=${TENANT_ID}`);
      if (window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) === row.id) {
        const nextAgent = employees.find((item) => item.id !== row.id && item.status === 'active')
          || employees.find((item) => item.id !== row.id);
        if (nextAgent) {
          window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, nextAgent.id);
          window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: nextAgent.id } }));
        } else {
          window.localStorage.removeItem(ENTERPRISE_AGENT_STORAGE_KEY);
          window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: '' } }));
        }
      }
      notify.success('员工已删除');
      setDeleteTarget(null);
      await load();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除员工失败');
    } finally {
      setDeleting(false);
    }
  }

  function updateAgentInList(row: AgentProfileRead) {
    setAgents((current) => current.map((item) => (item.id === row.id ? row : item)));
  }

  const employeeTabs: UnderlineTabItem<typeof employeeFilter>[] = [
    { value: 'all', label: '全部员工' },
    { value: 'online', label: '在线员工' },
    { value: 'offline', label: '下线员工' },
  ];

  const summaryCardClass =
    'flex h-[100px] flex-1 basis-[220px] items-center gap-[16px] rounded-[20px] bg-[#f6f6f6] px-[32px] py-[20px] text-left transition-shadow';
  const summaryStats: { key: typeof employeeFilter; value: number; label: string; sub: string }[] = [
    { key: 'all', value: employees.length, label: '员工总数', sub: `${onlineEmployees.length}位在线` },
    { key: 'offline', value: offlineEmployees.length, label: '下线员工', sub: '0位在线' },
    {
      key: 'pending',
      value: pendingEmployees.length,
      label: '待审批',
      sub: `${pendingEmployees.filter((item) => item.status === 'active').length}位在线`,
    },
  ];

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex h-[50px] w-full items-center gap-[6px] rounded-[20px] bg-white px-[20px] text-[#757F9C] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
            <IconSearch className="size-[20px] shrink-0" />
            <input
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="搜索"
              aria-label="搜索员工"
              className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
            />
          </div>
        )}
      />


      <div className="flex flex-wrap items-stretch gap-[20px] my-[36px]" aria-label="数字员工统计">
        {summaryStats.map((stat) => (
          <button
            key={stat.key}
            type="button"
            aria-pressed={employeeFilter === stat.key}
            onClick={() => setEmployeeFilter(stat.key)}
            className={cn(
              summaryCardClass,
            )}
          >
            <span className="shrink-0 text-[34px] font-semibold leading-none text-[#18181A]">{stat.value}</span>
            <span className="flex min-w-0 flex-col gap-[4px]">
              <span className="whitespace-nowrap text-[14px] text-[#464C5E]">{stat.label}</span>
              <span className="whitespace-nowrap text-[12px] text-[#757F9C]">{stat.sub}</span>
            </span>
          </button>
        ))}
        <button data-guide-target="agents-create" type="button" onClick={onCreateAgent} className={cn(summaryCardClass, 'hover:shadow-[0_16px_30px_0_rgba(0,0,0,0.10)]')}>
          <span className="grid size-[38px] shrink-0 place-items-center text-[#18181A]">
            <IconPlus className="size-[38px]" />
          </span>
          <span className="flex min-w-0 flex-col gap-[4px]">
            <span className="whitespace-nowrap text-[14px] text-[#464C5E]">创建新员工</span>
            <span className="whitespace-nowrap text-[12px] text-[#757F9C]">几步搭好你的数字员工</span>
          </span>
        </button>
      </div>

      <UnderlineTabs
        className="mb-[16px]"
        aria-label="数字员工分类"
        value={employeeFilter}
        onChange={setEmployeeFilter}
        items={employeeTabs}
      />

      <div className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]">
        {filteredEmployees.map((employee) => (
          <EmployeeCard
            key={employee.id}
            employee={employee}
            busy={selectingAgentId === employee.id}
            canManage={canManageEmployeeAgent(employee, currentUser)}
            selected={employee.id === selectedAgentId}
            onOpen={() => void selectEmployee(employee)}
            onStatus={(status) => void updateStatus(employee, status)}
            onGallery={(published) => void updateGalleryState(employee, published)}
            onDelete={() => setDeleteTarget(employee)}
            onAvatar={() => setAvatarAgent(employee)}
            onEdit={() => setProfileAgent(employee)}
            onChat={() => startEmployeeChat(employee)}
          />
        ))}
        {!filteredEmployees.length && (
          <AgentsEmptyState />
        )}
      </div>
      <EmployeeAvatarEditor
        agent={avatarAgent}
        open={Boolean(avatarAgent)}
        onClose={() => setAvatarAgent(null)}
        onSaved={updateAgentInList}
      />
      <EmployeeProfileEditor
        agent={profileAgent}
        open={Boolean(profileAgent)}
        currentUser={currentUser}
        onClose={() => setProfileAgent(null)}
        onSaved={updateAgentInList}
      />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除员工「${deleteTarget ? employeeDisplayName(deleteTarget) : ''}」？`}
        description="删除后该员工的所有配置将一并移除，操作不可撤销。"
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}

function AgentsEmptyState() {
  return (
    <div className="flex h-[262px] w-full items-center justify-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] text-center">
      <div className="flex max-w-[210px] flex-col items-center">
        <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
          <IconSearch className="size-[16px] shrink-0" />
        </span>
        <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
          没有匹配的数字员工
        </p>
        <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
          调整筛选条件，或换个关键词再试试
        </p>
      </div>
    </div>
  );
}
