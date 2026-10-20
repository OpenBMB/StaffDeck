import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import IconPlus from '../assets/icons/plus.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import { SearchEmptyState } from '@/components/SearchEmptyState';

import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import AgentShareDialog from '../components/AgentShareDialog';
import EmployeeApiKeyDialog from '../components/EmployeeApiKeyDialog';
import EmployeeCard from '../components/EmployeeCard';
import MyCreatedSkillsPanel from '../components/MyCreatedSkillsPanel';
import MyCreatedToolsPanel from '../components/MyCreatedToolsPanel';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import { RevealGroup } from '../components/RevealGroup';
import {
  canManageEmployeeAgent,
  canSelectCurrentEmployeeAgent,
  employeeDisplayName,
  employeeDisplayNameWithCreator,
  employeeProfile,
} from '../employee';
import {
  emitAgentRosterRefresh,
  emitAgentScopeChange,
  isTeamScope,
  onAgentRosterRefresh,
  persistSharedAgentScope,
  readEmployeeScope,
} from '../lib/agent-scope-storage';
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
  const [apiKeyAgent, setApiKeyAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [shareAgent, setShareAgent] = useState<AgentProfileRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [selectingAgentId, setSelectingAgentId] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [employeeFilter, setEmployeeFilter] = useState<'all' | 'online' | 'offline'>('all');
  // 页面级 tab：数字员工 / 技能管理 / 工具管理。
  const [pageTab, setPageTab] = useState<'employees' | 'skills' | 'tools'>(() => {
    if (window.location.hash.includes('skills')) return 'skills';
    if (window.location.hash.includes('tools')) return 'tools';
    return 'employees';
  });
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    () => readEmployeeScope() || null,
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

  // 员工列表是每个页面各自拉取的，创建 / 编辑 / 上下线 / 删除发生在别的页面
  // 或 App 侧边栏弹窗时，本页收不到任何信号。订阅花名册广播，避免新建员工后
  // 必须手动刷新页面才能看到。
  useEffect(
    () =>
      onAgentRosterRefresh(() => {
        void load();
      }),
    [],
  );

  useEffect(() => {
    const handler = (event: Event) => {
      const next = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setSelectedAgentId(next && !isTeamScope(next) ? next : readEmployeeScope() || null);
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
  const filteredEmployees = employees.filter((item) => {
    const profile = employeeProfile(item);
    const keyword = searchTerm.trim().toLowerCase();
    const matchesFilter = employeeFilter === 'all'
      || (employeeFilter === 'online' && item.status === 'active')
      || (employeeFilter === 'offline' && item.status !== 'active');
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
      // 本页也订阅了该事件，会自行重新拉取，无需再单独 await load()。
      emitAgentRosterRefresh();
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
      // 本页也订阅了该事件，会自行重新拉取，无需再单独 await load()。
      emitAgentRosterRefresh();
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
      if (readEmployeeScope() === row.id) {
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
      // 本页也订阅了该事件，会自行重新拉取，无需再单独 await load()。
      emitAgentRosterRefresh();
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
  ];

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
      />


      <UnderlineTabs
        className="mb-[16px]"
        aria-label="我的数字员工页面切换"
        value={pageTab}
        onChange={setPageTab}
        items={[
          { value: 'employees', label: '数字员工' },
          { value: 'skills', label: '技能管理' },
          { value: 'tools', label: '工具管理' },
        ]}
      />

      {pageTab === 'employees' ? (
        <>
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

      <div className="mb-[16px] flex h-[50px] w-full items-center gap-[6px] rounded-[20px] bg-white px-[20px] text-[#757F9C] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
        <IconSearch className="size-[20px] shrink-0" />
        <input
          autoComplete="off"
          data-1p-ignore="true"
          data-lpignore="true"
          data-bwignore="true"
          value={searchTerm}
          onChange={(event) => setSearchTerm(event.target.value)}
          placeholder="搜索"
          aria-label="搜索员工"
          className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
        />
      </div>

      <UnderlineTabs
        className="mb-[16px]"
        aria-label="数字员工分类"
        value={employeeFilter}
        onChange={setEmployeeFilter}
        items={employeeTabs}
      />
      <RevealGroup
        className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]"
        itemCount={filteredEmployees.length}
      >
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
            onApiKeys={() => setApiKeyAgent(employee)}
            onShare={() => setShareAgent(employee)}
          />
        ))}
        {!filteredEmployees.length && (
          <AgentsEmptyState />
        )}
      </RevealGroup>
        </>
      ) : pageTab === 'skills' ? (
        <div className="mt-[24px]">
          <div className="flex items-center justify-end mb-[16px]">
            <UIButton
              data-guide-target="skills-create"
              type="button"
              onClick={() => navigate('/enterprise/general-skills/new?scope=gallery')}
              className="h-[38px] rounded-[10px] bg-[#18181a] px-[20px] text-[13px] font-normal text-white hover:bg-[#303030]"
            >
              <IconPlus className="size-[16px]" />
              创建技能
            </UIButton>
          </div>
          {/* 我创建的技能：跨作用域汇总（广场技能 + 挂在员工下的技能）。 */}
          <MyCreatedSkillsPanel agents={agents} />
        </div>
      ) : (
        <div className="mt-[24px]">
          <div className="flex flex-wrap items-center justify-end gap-[10px] mb-[16px]">
            <UIButton
              type="button"
              variant="outline"
              onClick={() => navigate('/enterprise/tools/import/swagger')}
              className="h-[38px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[13px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              从 Swagger 导入
            </UIButton>
            <UIButton
              type="button"
              onClick={() => navigate('/enterprise/tools/new')}
              className="h-[38px] rounded-[10px] bg-[#18181a] px-[20px] text-[13px] font-normal text-white hover:bg-[#303030]"
            >
              <IconPlus className="size-[16px]" />
              新建工具
            </UIButton>
          </div>
          {/* 我创建的工具：跨作用域汇总（广场工具 + 挂在员工下的工具）。 */}
          <MyCreatedToolsPanel agents={agents} />
        </div>
      )}
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
      <EmployeeApiKeyDialog
        agent={apiKeyAgent}
        open={Boolean(apiKeyAgent)}
        onClose={() => setApiKeyAgent(null)}
      />
      <AgentShareDialog
        agent={shareAgent}
        open={Boolean(shareAgent)}
        onClose={() => setShareAgent(null)}
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
    <SearchEmptyState
      title="没有匹配的数字员工"
      description="调整筛选条件，或换个关键词再试试"
    />
  );
}
