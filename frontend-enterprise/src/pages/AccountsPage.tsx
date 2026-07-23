import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { User } from 'lucide-react';

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
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS, MENU_ITEM_DANGER_CLASS, MOBILE_CARD_CLASS, formatDateTime } from '@/lib/enterprise-ui';

import { api, TENANT_ID } from '../api/client';
import IconAccounts from '../assets/icons/sys-accounts.svg?react';
import IconAdd from '../assets/icons/add.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import type { EnterpriseAuthUser } from '../auth';
import { useClientPagination } from '../hooks/useClientPagination';

type EmployeeAccount = {
  id: string;
  tenant_id: string;
  username: string;
  display_name?: string;
  role: 'admin' | 'member';
  created_at?: string;
  updated_at?: string;
};

type AccountDraft = {
  displayName: string;
  password: string;
  role: 'admin' | 'member';
};

type AccountCreateDraft = {
  username: string;
  displayName: string;
  password: string;
  role: 'admin' | 'member';
};

const ACCOUNT_PAGE_SIZE = 10;

export default function AccountsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [rows, setRows] = useState<EmployeeAccount[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [editing, setEditing] = useState<EmployeeAccount | null>(null);
  const [draft, setDraft] = useState<AccountDraft>({ displayName: '', password: '', role: 'member' });
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<AccountCreateDraft>({
    username: '',
    displayName: '',
    password: '',
    role: 'member',
  });
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<EmployeeAccount | null>(null);
  const [deleting, setDeleting] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const result = await api.get<EmployeeAccount[]>(`/api/auth/users?tenant_id=${TENANT_ID}`);
      setRows(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载账号失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return rows;
    return rows.filter((row) =>
      [row.username, row.display_name || '', row.role === 'admin' ? '管理员' : '普通成员']
        .some((value) => value.toLowerCase().includes(keyword)),
    );
  }, [rows, searchText]);

  const pagination = useClientPagination(filteredRows, ACCOUNT_PAGE_SIZE, searchText);

  function openEdit(row: EmployeeAccount) {
    setEditing(row);
    setDraft({ displayName: row.display_name || row.username, password: '', role: row.role });
  }

  function openCreate() {
    setCreateDraft({ username: '', displayName: '', password: '', role: 'member' });
    setCreateOpen(true);
  }

  async function saveCreate() {
    const username = createDraft.username.trim();
    const password = createDraft.password.trim();
    if (!username || !password) {
      notify.error('请填写账号和密码');
      return;
    }
    setCreating(true);
    try {
      await api.post('/api/auth/users', {
        tenant_id: TENANT_ID,
        username,
        password,
        display_name: createDraft.displayName.trim() || username,
        role: createDraft.role,
      });
      notify.success('账号已创建');
      setCreateOpen(false);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建账号失败');
    } finally {
      setCreating(false);
    }
  }

  async function saveEdit() {
    if (!editing) return;
    setSaving(true);
    try {
      await api.put(`/api/auth/users/${editing.id}`, {
        tenant_id: TENANT_ID,
        display_name: draft.displayName.trim() || editing.username,
        password: draft.password.trim() || undefined,
        role: draft.role,
      });
      notify.success('账号已更新');
      setEditing(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存账号失败');
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      await api.delete(`/api/auth/users/${row.id}?tenant_id=${TENANT_ID}`);
      notify.success('账号已删除');
      setDeleteTarget(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除账号失败');
    } finally {
      setDeleting(false);
    }
  }

  function renderActions(row: EmployeeAccount) {
    const isProtected = row.role === 'admin';
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="账号操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => openEdit(row)}>
            <IconEdit />
            编辑
          </DropdownMenuItem>
          <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
          <DropdownMenuItem
            variant="destructive"
            className={MENU_ITEM_DANGER_CLASS}
            disabled={isProtected}
            onSelect={() => setDeleteTarget(row)}
          >
            <IconTrash />
            删除
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  const columns: DataTableColumn<EmployeeAccount>[] = [
    {
      key: 'username',
      title: '用户名',
      width: 220,
      className: 'text-[#18181a]',
      render: (row) => (
        <span className="flex min-w-0 items-center gap-[8px]">
          <span className="grid size-[24px] shrink-0 place-items-center rounded-full bg-[#eef1fb] text-[#7e96dc]">
            <User className="size-[14px]" />
          </span>
          <span className="truncate font-medium">{row.username}</span>
        </span>
      ),
    },
    {
      key: 'display_name',
      title: '显示名',
      width: 200,
      render: (row) => <span className="block truncate">{row.display_name || row.username}</span>,
    },
    {
      key: 'role',
      title: '角色',
      width: 120,
      render: (row) => <span>{row.role === 'admin' ? '管理员' : '普通成员'}</span>,
    },
    { key: 'created', title: '创建时间', width: 180, render: (row) => formatDateTime(row.created_at) },
    { key: 'updated', title: '最近更新', width: 180, render: (row) => formatDateTime(row.updated_at) },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => renderActions(row),
    },
  ];

  const renderMobileCard = (row: EmployeeAccount) => (
    <article className={MOBILE_CARD_CLASS} key={row.id}>
      <div className="flex min-w-0 items-start justify-between gap-[10px]">
        <span className="flex min-w-0 items-center gap-[8px]">
          <span className="grid size-[28px] shrink-0 place-items-center rounded-full bg-[#eef1fb] text-[#7e96dc]">
            <User className="size-[15px]" />
          </span>
          <span className="min-w-0">
            <strong className="block truncate text-[14px] font-semibold text-[#18181a]">{row.username}</strong>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.display_name || row.username}</span>
          </span>
        </span>
        {renderActions(row)}
      </div>
      <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
        <span>创建 {formatDateTime(row.created_at)}</span>
        <span>更新 {formatDateTime(row.updated_at)}</span>
      </div>
    </article>
  );

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="账号管理" />

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
        <UIButton
          onClick={openCreate}
          className="h-[34px] gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white hover:bg-[#303030]"
        >
          <IconAdd className="size-[14px]" />
          新建账号
        </UIButton>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconAccounts className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">账号列表</span>
          </div>

          <label className="flex h-[34px] w-[300px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              value={searchText}
              placeholder="搜索用户名或显示名"
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

          <div className="grid gap-[10px] md:hidden">
            {filteredRows.length ? (
              pagination.pagedItems.map(renderMobileCard)
            ) : (
              <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无账号</div>
            )}
          </div>

          <div className="hidden md:block">
            <DataTable
              aria-label="账号列表"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无账号"
            />
          </div>

          {filteredRows.length > 0 && (
            <Paginator
              aria-label="账号分页"
              className="mt-0 mb-[6px]"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </div>
      </div>

      <AccountDialog
        open={createOpen}
        title="新建账号"
        loading={creating}
        submitText="创建"
        username={{ value: createDraft.username, onChange: (value) => setCreateDraft((prev) => ({ ...prev, username: value })) }}
        displayName={createDraft.displayName}
        onDisplayNameChange={(value) => setCreateDraft((prev) => ({ ...prev, displayName: value }))}
        password={createDraft.password}
        onPasswordChange={(value) => setCreateDraft((prev) => ({ ...prev, password: value }))}
        role={createDraft.role}
        onRoleChange={(value) => setCreateDraft((prev) => ({ ...prev, role: value }))}
        passwordLabel="初始密码"
        onClose={() => setCreateOpen(false)}
        onSubmit={() => void saveCreate()}
      />

      <AccountDialog
        open={Boolean(editing)}
        title={editing ? `编辑账号：${editing.username}` : '编辑账号'}
        loading={saving}
        submitText="保存"
        username={null}
        displayName={draft.displayName}
        onDisplayNameChange={(value) => setDraft((prev) => ({ ...prev, displayName: value }))}
        password={draft.password}
        onPasswordChange={(value) => setDraft((prev) => ({ ...prev, password: value }))}
        role={draft.role}
        onRoleChange={(value) => setDraft((prev) => ({ ...prev, role: value }))}
        roleDisabled={editing?.id === currentUser?.id}
        passwordLabel="新密码"
        passwordPlaceholder="不修改请留空"
        onClose={() => setEditing(null)}
        onSubmit={() => void saveEdit()}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        loading={deleting}
        title={deleteTarget ? `删除账号「${deleteTarget.username}」？` : ''}
        description="删除后该账号无法登录，但其创建的数字员工仍然保留。"
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}

function AccountDialog({
  open,
  title,
  loading,
  submitText,
  username,
  displayName,
  onDisplayNameChange,
  password,
  onPasswordChange,
  role,
  onRoleChange,
  roleDisabled = false,
  passwordLabel,
  passwordPlaceholder,
  onClose,
  onSubmit,
}: {
  open: boolean;
  title: string;
  loading: boolean;
  submitText: string;
  username: { value: string; onChange: (value: string) => void } | null;
  displayName: string;
  onDisplayNameChange: (value: string) => void;
  password: string;
  onPasswordChange: (value: string) => void;
  role: 'admin' | 'member';
  onRoleChange: (value: 'admin' | 'member') => void;
  roleDisabled?: boolean;
  passwordLabel: string;
  passwordPlaceholder?: string;
  onClose: () => void;
  onSubmit: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconAccounts className="size-[14px] shrink-0" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {title}
          </DialogTitle>
        </div>

        <div className="flex flex-col gap-[14px] px-[12px]">
          {username && (
            <LabeledField label="用户名">
              <Input
                value={username.value}
                placeholder="例如 zhang_san"
                onChange={(event) => username.onChange(event.target.value)}
              />
            </LabeledField>
          )}
          <LabeledField label="显示名">
            <Input
              value={displayName}
              placeholder="例如 张三"
              onChange={(event) => onDisplayNameChange(event.target.value)}
            />
          </LabeledField>
          <LabeledField label={passwordLabel}>
            <Input
              type="password"
              value={password}
              placeholder={passwordPlaceholder}
              onChange={(event) => onPasswordChange(event.target.value)}
            />
          </LabeledField>
          <LabeledField label="账号角色">
            <Select
              value={role}
              disabled={roleDisabled}
              onValueChange={(value) => onRoleChange(value as 'admin' | 'member')}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="member">普通成员</SelectItem>
                <SelectItem value="admin">管理员</SelectItem>
              </SelectContent>
            </Select>
          </LabeledField>
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
            {submitText}
          </UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function LabeledField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      {children}
    </label>
  );
}
