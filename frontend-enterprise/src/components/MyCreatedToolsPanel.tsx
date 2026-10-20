import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { EyeOff, FlaskConical, UploadCloud } from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import {
  announceEnterpriseCapabilityCatalogChange,
  subscribeEnterpriseCapabilityCatalogRefresh,
} from '@/lib/capability-catalog-events';
import {
  MENU_CONTENT_CLASS,
  MENU_ITEM_CLASS,
  MENU_ITEM_DANGER_CLASS,
  MOBILE_CARD_CLASS,
  OUTLINE_ACTION_BUTTON_SM_CLASS,
  formatDateTime,
} from '@/lib/enterprise-ui';

import { api, TENANT_ID } from '../api/client';
import { employeeDisplayName, openGalleryAgentId } from '../employee';
import IconEdit from '../assets/icons/edit.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconTool from '../assets/icons/plaza-tool.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import { StatusBadge } from '../pages/scheduled-tasks/StatusBadge';
import type { AgentProfileRead, ToolAccessRequestRead, ToolRead } from '../types';
import { ConfirmDialog } from './ConfirmDialog';
import { DataTable, type DataTableColumn } from './DataTable';

function toolOwnerAgentId(row: ToolRead): string {
  const value = row.metadata?.owner_agent_id;
  return typeof value === 'string' ? value.trim() : '';
}

function toolScopeLabel(row: ToolRead, agents: AgentProfileRead[]): string {
  const agentId = toolOwnerAgentId(row);
  if (!agentId) return '工具广场';
  return employeeDisplayName(agents.find((item) => item.id === agentId));
}

/** 「我创建的工具」维护区：跨广场/员工作用域汇总当前用户创建的工具。 */
export default function MyCreatedToolsPanel({ agents }: { agents: AgentProfileRead[] }) {
  const navigate = useNavigate();
  const [rows, setRows] = useState<ToolRead[]>([]);
  const [requests, setRequests] = useState<ToolAccessRequestRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [requestLoading, setRequestLoading] = useState(false);
  const [reviewingId, setReviewingId] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<ToolRead | null>(null);
  const [deleting, setDeleting] = useState(false);

  const loadRequests = () => {
    setRequestLoading(true);
    return api
      .get<ToolAccessRequestRead[]>(
        `/api/enterprise/tool-access-requests?tenant_id=${TENANT_ID}&status=pending`,
      )
      .then(setRequests)
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载工具审批失败'))
      .finally(() => setRequestLoading(false));
  };

  const load = () => {
    setLoading(true);
    return Promise.all([
      api.get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}&mine=1`),
      loadRequests(),
    ])
      .then(([toolRows]) => setRows(toolRows))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载我的工具失败'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(
    () =>
      subscribeEnterpriseCapabilityCatalogRefresh(() => {
        void load();
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  );

  const referenceTotal = useMemo(
    () => rows.reduce((total, row) => total + (row.reference_count || 0), 0),
    [rows],
  );

  function scopeAgentId(row: ToolRead): string {
    return toolOwnerAgentId(row) || openGalleryAgentId(agents);
  }

  function agentQuery(row: ToolRead): string {
    const agentId = scopeAgentId(row);
    return agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
  }

  function editRouteFor(row: ToolRead): string {
    const agentId = scopeAgentId(row);
    return `/enterprise/tools/${encodeURIComponent(row.id)}/edit${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ''}`;
  }

  function testRouteFor(row: ToolRead): string {
    const agentId = scopeAgentId(row);
    return `/enterprise/tools/${encodeURIComponent(row.id)}/test${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ''}`;
  }

  async function setToolPublished(row: ToolRead, published: boolean) {
    try {
      const updated = await api.post<ToolRead>(
        `/api/enterprise/tools/${encodeURIComponent(row.id)}/${published ? 'publish' : 'unpublish'}?tenant_id=${TENANT_ID}`,
        {},
      );
      setRows((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      notify.success(published ? '已发布到工具广场' : '已从工具广场下架');
      announceEnterpriseCapabilityCatalogChange({
        resourceType: 'tool',
        agentId: toolOwnerAgentId(row) || undefined,
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : published ? '发布失败' : '下架失败');
    }
  }

  async function reviewRequest(row: ToolAccessRequestRead, approve: boolean) {
    if (reviewingId) return;
    setReviewingId(row.id);
    try {
      await api.post(
        `/api/enterprise/tool-access-requests/${row.id}/${approve ? 'approve' : 'reject'}`,
        { tenant_id: TENANT_ID },
      );
      notify.success(approve ? '已通过申请，工具已绑定到该员工' : '已拒绝申请');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '审批失败');
    } finally {
      setReviewingId('');
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row) return;
    setDeleting(true);
    try {
      await api.delete(
        `/api/enterprise/tools/${encodeURIComponent(row.id)}?tenant_id=${TENANT_ID}${agentQuery(row)}`,
      );
      setRows((current) => current.filter((item) => item.id !== row.id));
      notify.success(toolOwnerAgentId(row) ? '已移除工具' : '已删除工具');
      setDeleteTarget(null);
      announceEnterpriseCapabilityCatalogChange({
        resourceType: 'tool',
        agentId: toolOwnerAgentId(row) || undefined,
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  function renderActions(row: ToolRead) {
    const isMcpChild = row.tool_type === 'mcp' && Boolean(row.mcp_server_id);
    const boundToAgent = Boolean(toolOwnerAgentId(row));
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="工具操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          {!isMcpChild && (
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => navigate(editRouteFor(row))}>
              <IconEdit />
              编辑
            </DropdownMenuItem>
          )}
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => navigate(testRouteFor(row))}>
            <FlaskConical />
            测试
          </DropdownMenuItem>
          <DropdownMenuItem
            className={MENU_ITEM_CLASS}
            onSelect={() => void setToolPublished(row, !row.published)}
          >
            {row.published ? <EyeOff /> : <UploadCloud />}
            {row.published ? '从广场下架' : '发布到广场'}
          </DropdownMenuItem>
          <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
          <DropdownMenuItem
            variant="destructive"
            className={MENU_ITEM_DANGER_CLASS}
            onSelect={() => setDeleteTarget(row)}
          >
            <IconTrash />
            {boundToAgent ? '移除' : '删除'}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  const columns: DataTableColumn<ToolRead>[] = [
    {
      key: 'name',
      title: '名称',
      width: 200,
      className: 'text-[#18181a]',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate font-medium leading-[18px] text-[#18181a]" title={row.display_name || row.name}>
            {row.display_name || row.name}
          </span>
          <span className="truncate text-[#858b9c]" title={row.name}>
            {row.name}
          </span>
        </div>
      ),
    },
    {
      key: 'scope',
      title: '归属',
      width: 130,
      render: (row) => (
        <span className="block truncate text-[#858b9c]" title={toolScopeLabel(row, agents)}>
          {toolScopeLabel(row, agents)}
        </span>
      ),
    },
    {
      key: 'type',
      title: '类型',
      width: 90,
      render: (row) => (
        <StatusBadge tone={row.tool_type === 'mcp' ? 'blue' : 'gray'}>
          {row.tool_type === 'mcp' ? 'MCP' : 'HTTP'}
        </StatusBadge>
      ),
    },
    {
      key: 'group',
      title: '分组',
      width: 120,
      render: (row) => <StatusBadge tone="gray">{row.bucket || '未分组'}</StatusBadge>,
    },
    {
      key: 'references',
      title: '被引用',
      width: 110,
      render: (row) => (
        <StatusBadge tone={(row.reference_count || 0) > 0 ? 'blue' : 'gray'}>
          {(row.reference_count || 0) > 0 ? `${row.reference_count} 个员工` : '未引用'}
        </StatusBadge>
      ),
    },
    {
      key: 'gallery',
      title: '广场',
      width: 90,
      render: (row) => (
        <StatusBadge tone={row.published ? 'blue' : 'gray'}>
          {row.published ? '已发布' : '未发布'}
        </StatusBadge>
      ),
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

  function renderMobileCard(row: ToolRead) {
    return (
      <article className={MOBILE_CARD_CLASS} key={row.id}>
        <div className="flex min-w-0 items-start justify-between gap-[10px]">
          <div className="min-w-0">
            <strong className="block truncate text-[14px] font-semibold text-[#18181a]">
              {row.display_name || row.name}
            </strong>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.name}</span>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">
              归属：{toolScopeLabel(row, agents)}
            </span>
          </div>
          {renderActions(row)}
        </div>
        {row.description && (
          <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">
            {row.description}
          </p>
        )}
        <div className="mt-[10px] flex flex-wrap items-center gap-[6px] text-[12px] text-[#858b9c]">
          <StatusBadge tone={row.tool_type === 'mcp' ? 'blue' : 'gray'}>
            {row.tool_type === 'mcp' ? 'MCP' : 'HTTP'}
          </StatusBadge>
          <StatusBadge tone="gray">{row.bucket || '未分组'}</StatusBadge>
          <StatusBadge tone={(row.reference_count || 0) > 0 ? 'blue' : 'gray'}>
            {(row.reference_count || 0) > 0 ? `${row.reference_count} 个员工引用` : '未被引用'}
          </StatusBadge>
          <StatusBadge tone={row.published ? 'blue' : 'gray'}>
            {row.published ? '已发布' : '未发布'}
          </StatusBadge>
          <span className="ml-auto">{formatDateTime(row.updated_at)}</span>
        </div>
      </article>
    );
  }

  return (
    <div className="flex flex-col gap-[28px]">
      <section aria-label="工具使用审批">
        <div className="mb-[14px] flex flex-wrap items-baseline gap-[8px]">
          <h2 className="text-[16px] font-semibold leading-[22px] text-[#18181a]">使用审批</h2>
          <span className="text-[12px] text-[#858b9c]">{requests.length} 个待处理</span>
          <button
            type="button"
            onClick={() => void loadRequests()}
            className="ml-auto inline-flex cursor-pointer items-center gap-[4px] border-0 bg-transparent p-0 text-[12px] text-[#757f9c] transition-colors hover:text-[#18181a]"
          >
            <IconRefresh className={`size-[14px] shrink-0 ${requestLoading ? 'animate-spin' : ''}`} />
            刷新
          </button>
        </div>
        {requestLoading && !requests.length ? (
          <p className="rounded-[16px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[20px] text-center text-[12px] text-[#a7adbb]">
            加载中…
          </p>
        ) : requests.length ? (
          <div className="flex flex-col gap-[10px]">
            {requests.map((row) => (
              <article
                key={row.id}
                className="flex flex-wrap items-center gap-[14px] rounded-[14px] border border-[#eceef1] bg-white px-[16px] py-[14px]"
              >
                <div className="min-w-[220px] flex-1">
                  <div className="flex flex-wrap items-center gap-[8px]">
                    <strong className="text-[13px] font-semibold text-[#18181a]">
                      {row.tool_display_name}
                    </strong>
                    <StatusBadge tone="blue">待审批</StatusBadge>
                  </div>
                  <p className="mt-[4px] text-[12px] leading-[1.55] text-[#858b9c]">
                    {row.requested_by_username} 申请给「{row.agent_name}」使用
                    {row.tool_description ? ` · ${row.tool_description}` : ''}
                  </p>
                  {row.review_note && (
                    <p className="mt-[4px] rounded-[8px] bg-[#f6f7fa] px-[8px] py-[5px] text-[11px] leading-[1.55] text-[#757f9c]">
                      备注：{row.review_note}
                    </p>
                  )}
                </div>
                <span className="text-[11px] text-[#a7adbb]">{formatDateTime(row.created_at)}</span>
                <div className="flex items-center gap-[8px]">
                  <UIButton
                    variant="outline"
                    disabled={Boolean(reviewingId)}
                    onClick={() => void reviewRequest(row, true)}
                    className={`${OUTLINE_ACTION_BUTTON_SM_CLASS} text-[#2cb360]`}
                  >
                    通过
                  </UIButton>
                  <UIButton
                    variant="outline"
                    disabled={Boolean(reviewingId)}
                    onClick={() => void reviewRequest(row, false)}
                    className={`${OUTLINE_ACTION_BUTTON_SM_CLASS} text-[#e5484d]`}
                  >
                    拒绝
                  </UIButton>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="rounded-[16px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[20px] text-center text-[12px] text-[#a7adbb]">
            暂无待审批的工具使用申请
          </p>
        )}
      </section>

      <section aria-label="我创建的工具">
        <div className="mb-[14px] flex flex-wrap items-baseline gap-[8px]">
        <h2 className="text-[16px] font-semibold leading-[22px] text-[#18181a]">我创建的工具</h2>
        <span className="text-[12px] text-[#858b9c]">
          {rows.length} 个 · 合计 {referenceTotal} 个员工引用
        </span>
        <button
          type="button"
          onClick={() => void load()}
          className="ml-auto inline-flex cursor-pointer items-center gap-[4px] border-0 bg-transparent p-0 text-[12px] text-[#757f9c] transition-colors hover:text-[#18181a]"
        >
          <IconRefresh className="size-[14px] shrink-0" />
          刷新
        </button>
      </div>

      {loading && !rows.length ? (
        <p className="rounded-[16px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[24px] text-center text-[12px] text-[#a7adbb]">
          加载中…
        </p>
      ) : rows.length ? (
        <>
          <div className="hidden md:block">
            <DataTable
              columns={columns}
              data={rows}
              rowKey={(row) => row.id}
              aria-label="我创建的工具列表"
            />
          </div>
          <div className="grid gap-[12px] md:hidden">
            {rows.map((row) => renderMobileCard(row))}
          </div>
        </>
      ) : (
        <div className="flex flex-col items-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[26px] text-center">
          <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
            <IconTool className="size-[16px] shrink-0" />
          </span>
          <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
            还没有创建过工具
          </p>
          <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
            创建或导入 HTTP 工具后，可在这里统一编辑、查看引用和删除
          </p>
          <UIButton
            variant="outline"
            className={`mt-[14px] ${OUTLINE_ACTION_BUTTON_SM_CLASS}`}
            onClick={() => navigate('/enterprise/tools')}
          >
            去工具管理
          </UIButton>
        </div>
      )}

      </section>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={
          deleteTarget
            ? `${toolOwnerAgentId(deleteTarget) ? '移除' : '删除'}工具「${deleteTarget.display_name || deleteTarget.name}」？`
            : ''
        }
        description={
          deleteTarget && toolOwnerAgentId(deleteTarget)
            ? '将从该数字员工移除这个工具引用；工具定义本身不会被删除。'
            : '删除后引用该工具的技能将无法继续调用它，操作不可撤销。'
        }
        confirmText={deleteTarget && toolOwnerAgentId(deleteTarget) ? '移除' : '删除'}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}
