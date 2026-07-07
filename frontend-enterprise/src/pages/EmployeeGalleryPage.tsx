import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

import IconSearch from '../assets/icons/search.svg?react';

import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { isGalleryEmployee, type EnterpriseAuthUser } from '../auth';

import AppHeader from '../components/AppHeader';
import { ConfirmDialog } from '../components/ConfirmDialog';
import EmployeeAvatarEditor from '../components/EmployeeAvatarEditor';
import EmployeeCard from '../components/EmployeeCard';
import EmployeeProfileEditor from '../components/EmployeeProfileEditor';
import {
  canManageEmployeeAgent,
  employeeDisplayName,
  employeeProfile,
  visibleEmployeeAgents,
} from '../employee';
import type { AgentProfileRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type GalleryScope = 'all' | 'mine' | 'gallery';

export default function EmployeeGalleryPage({
  currentUser,
  isAdmin = false,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onLogout?: () => void;
}) {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [avatarAgent, setAvatarAgent] = useState<AgentProfileRead | null>(null);
  const [profileAgent, setProfileAgent] = useState<AgentProfileRead | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AgentProfileRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const [scope, setScope] = useState<GalleryScope>('all');
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

  const overallAgent = agents.find((item) => item.is_overall);
  // Keep these tabs aligned with the rest of the app:
  // - 所有员工: employees the current user can access and chat with
  // - 我的数字员工: employees the current user can manage/edit
  // - 数字员工广场: public employees not already listed as mine
  const availableAgents = useMemo(
    () => visibleEmployeeAgents(agents, currentUser, { activeOnly: true }),
    [agents, currentUser],
  );
  const myEmployees = useMemo(
    () => availableAgents.filter((item) => canManageEmployeeAgent(item, currentUser)),
    [availableAgents, currentUser],
  );
  const galleryEmployees = useMemo(() => {
    const myIds = new Set(myEmployees.map((item) => item.id));
    return availableAgents.filter((item) => isGalleryEmployee(item) && !myIds.has(item.id));
  }, [availableAgents, myEmployees]);

  const scopedEmployees = scope === 'mine'
    ? myEmployees
    : scope === 'gallery'
      ? galleryEmployees
      : availableAgents;

  const filteredEmployees = scopedEmployees.filter((item) => {
    const profile = employeeProfile(item);
    const keyword = searchTerm.trim().toLowerCase();
    if (!keyword) return true;
    return [
      employeeDisplayName(item),
      profile.roleName,
      item.description || '',
      profile.workStyles.join(' '),
      profile.expertiseTags.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword));
  });

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
      if (window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) === row.id && overallAgent) {
        window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, overallAgent.id);
        window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: overallAgent.id } }));
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

  const galleryTabs: UnderlineTabItem<GalleryScope>[] = [
    { value: 'all', label: '所有员工' },
    { value: 'mine', label: '我的数字员工' },
    { value: 'gallery', label: '数字员工广场' },
  ];

  const emptyText = searchTerm ? '没有匹配的数字员工' : '暂无数字员工';

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
              aria-label="搜索数字员工"
              className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181A] outline-none placeholder:text-[#757F9C]"
            />
          </div>
        )}
      />

      <UnderlineTabs
        className="mt-[36px] mb-[16px]"
        aria-label="数字员工分类"
        value={scope}
        onChange={setScope}
        items={galleryTabs}
      />

      <div className="grid auto-rows-[minmax(262px,auto)] grid-cols-1 content-start gap-[32px] sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 max-[900px]:gap-[18px]">
        {filteredEmployees.map((employee) => (
          <EmployeeCard
            key={employee.id}
            employee={employee}
            canManage={canManageEmployeeAgent(employee, currentUser)}
            showMenu={false}
            onOpen={() => startEmployeeChat(employee)}
            onStatus={(status) => void updateStatus(employee, status)}
            onGallery={(published) => void updateGalleryState(employee, published)}
            onDelete={() => setDeleteTarget(employee)}
            onAvatar={() => setAvatarAgent(employee)}
            onEdit={() => setProfileAgent(employee)}
            onChat={() => startEmployeeChat(employee)}
          />
        ))}
        {!filteredEmployees.length && (
          <div className="grid h-[262px] w-[294px] max-w-full place-items-center content-center gap-[10px] rounded-[18px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] font-bold text-[#8b94aa]">
            <IconSearch className="size-[20px] shrink-0" />
            <span>{emptyText}</span>
          </div>
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
