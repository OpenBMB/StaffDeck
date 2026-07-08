import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { notify } from '@/components/ui/app-toast';

import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { StatCard } from '@/components/StatCard';
import { Button as UIButton } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui';
import { MOBILE_CARD_CLASS } from '@/lib/enterprise-ui';

import { api, TENANT_ID } from '../../api/client';
import IconAdd from '../../assets/icons/add.svg?react';
import IconAlignJustify from '../../assets/icons/align-justify.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import IconTable from '../../assets/icons/table.svg?react';
import { useClientPagination } from '../../hooks/useClientPagination';
import type { AgentProfileRead, ScheduledTaskRead, ScheduledTaskRunRead } from '../../types';
import { StatusBadge, TaskRunResultBadge, TaskStatusBadge } from '../scheduled-tasks/StatusBadge';
import { TaskActionsMenu } from '../scheduled-tasks/TaskActionsMenu';
import { TaskSection } from '../scheduled-tasks/TaskSection';
import {
  ENTERPRISE_AGENT_STORAGE_KEY,
  RUN_FILTER_TABS,
  TASK_FILTER_TABS,
  TASK_PAGE_SIZE,
  formatSchedule,
  formatTime,
  matchesRunFilter,
  matchesTaskFilter,
  type RunListFilter,
  type TaskListFilter,
} from '../scheduled-tasks/shared';

export {
  ScheduledTaskEditPage,
  ScheduledTaskNewPage,
  type ScheduledTaskPageProps,
} from '../scheduled-tasks/ScheduledTaskEditorPage';

const MOBILE_CARD_HEAD_CLASS = 'flex min-w-0 items-start justify-between gap-[10px]';
const MOBILE_META_CLASS =
  'mt-[12px] grid grid-cols-2 gap-[8px] max-[520px]:grid-cols-1 [&>span]:min-w-0 [&>span]:rounded-[10px] [&>span]:border [&>span]:border-[#eef0f4] [&>span]:bg-[#fafbfc] [&>span]:px-[10px] [&>span]:py-[9px] [&>span]:text-[12px] [&>span]:leading-[1.45] [&>span]:text-[#18181a] [&>span]:[overflow-wrap:anywhere] [&_b]:mb-[3px] [&_b]:block [&_b]:text-[11px] [&_b]:font-semibold [&_b]:text-[#858b9c]';
const MOBILE_TITLE_CLASS =
  'min-w-0 wrap-break-word text-[14px] font-semibold text-[#18181a]';
const MOBILE_SUMMARY_CLASS = 'mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]';

export default function ScheduledTasksTab() {
  const [rows, setRows] = useState<ScheduledTaskRead[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [agentId, setAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '',
  );
  const [loading, setLoading] = useState(false);
  const [runsOpen, setRunsOpen] = useState(false);
  const [runRows, setRunRows] = useState<ScheduledTaskRunRead[]>([]);
  const [allRunRows, setAllRunRows] = useState<ScheduledTaskRunRead[]>([]);
  const [taskFilter, setTaskFilter] = useState<TaskListFilter>('all');
  const [runFilter, setRunFilter] = useState<RunListFilter>('all');
  const [runLoading, setRunLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ScheduledTaskRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();

  const selectedAgent = agents.find((item) => item.id === agentId) || null;
  const createDisabled = !agentId || Boolean(selectedAgent?.is_overall);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const nextAgentId =
        (event as CustomEvent<{ agentId?: string }>).detail?.agentId ||
        window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) ||
        '';
      setAgentId(nextAgentId);
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    void loadAgents();
  }, []);

  useEffect(() => {
    if (agentId) void load();
  }, [agentId]);

  async function loadAgents() {
    try {
      const result = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(result);
    } catch {
      setAgents([]);
    }
  }

  async function load() {
    setLoading(true);
    try {
      const [result, runResult] = await Promise.all([
        api.get<ScheduledTaskRead[]>(
          `/api/enterprise/scheduled-tasks?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(agentId)}`,
        ),
        api.get<ScheduledTaskRunRead[]>(
          `/api/enterprise/scheduled-tasks/runs?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(agentId)}&limit=200`,
        ),
      ]);
      setRows(result);
      setAllRunRows(runResult);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载定时任务失败');
    } finally {
      setLoading(false);
    }
  }

  async function toggleStatus(row: ScheduledTaskRead) {
    if (row.status === 'archived') {
      notify.warning('已删除的定时任务不能重新启用');
      return;
    }
    if (row.status === 'completed') {
      notify.warning('已完成的定时任务可编辑后重新启用');
      return;
    }
    const nextStatus = row.status === 'active' ? 'paused' : 'active';
    try {
      await api.put<ScheduledTaskRead>(`/api/enterprise/scheduled-tasks/${row.id}`, {
        tenant_id: TENANT_ID,
        status: nextStatus,
      });
      notify.success(nextStatus === 'active' ? '定时任务已启用' : '定时任务已暂停');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新定时任务失败');
    }
  }

  async function runNow(row: ScheduledTaskRead) {
    if (row.status === 'archived') {
      notify.warning('已删除的定时任务不能运行');
      return;
    }
    try {
      const run = await api.post<ScheduledTaskRunRead>(
        `/api/enterprise/scheduled-tasks/${row.id}/run-now?tenant_id=${TENANT_ID}`,
      );
      notify.success(run.session_id ? '已创建独立任务会话，后台开始执行' : '已触发后台执行');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '立即执行失败');
    }
  }

  function remove(row: ScheduledTaskRead) {
    setDeleteTarget(row);
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/scheduled-tasks/${deleteTarget.id}?tenant_id=${TENANT_ID}`);
      notify.success('已删除');
      setDeleteTarget(null);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除定时任务失败');
    } finally {
      setDeleting(false);
    }
  }

  async function openRuns(row: ScheduledTaskRead) {
    setRunsOpen(true);
    setRunLoading(true);
    try {
      const result = await api.get<ScheduledTaskRunRead[]>(
        `/api/enterprise/scheduled-tasks/${row.id}/runs?tenant_id=${TENANT_ID}`,
      );
      setRunRows(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载执行记录失败');
    } finally {
      setRunLoading(false);
    }
  }

  function openChatSession(sessionId?: string) {
    if (!sessionId) return;
    window.open(`/workspace/chat/${sessionId}`, '_blank', 'noopener,noreferrer');
  }

  const activeRows = rows.filter((item) => item.status === 'active');
  const taskRows = rows.filter((item) => item.status !== 'archived');
  const completedCount = taskRows.filter((item) => item.status === 'completed').length;
  const visibleRows = taskRows.filter((item) => matchesTaskFilter(item, taskFilter));
  const visibleRunRows = allRunRows.filter((item) => matchesRunFilter(item, runFilter));

  const taskPagination = useClientPagination(visibleRows, TASK_PAGE_SIZE, taskFilter);
  const runPagination = useClientPagination(visibleRunRows, TASK_PAGE_SIZE, runFilter);
  const runsModalPagination = useClientPagination(runRows, TASK_PAGE_SIZE, runRows);

  const renderTaskActions = (row: ScheduledTaskRead) => (
    <TaskActionsMenu
      task={row}
      onViewRuns={openRuns}
      onEdit={(task) => navigate(`/enterprise/scheduled-tasks/${task.id}/edit`)}
      onRunNow={runNow}
      onToggleStatus={toggleStatus}
      onDelete={remove}
    />
  );

  const taskColumns: DataTableColumn<ScheduledTaskRead>[] = [
    {
      key: 'title',
      title: '定时任务',
      className: 'whitespace-normal',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[4px]">
          <span className="font-medium leading-[18px] text-[#18181a]">{row.title}</span>
          <span className="truncate">{row.prompt}</span>
        </div>
      ),
    },
    {
      key: 'schedule',
      title: '计划',
      width: 200,
      className: 'whitespace-normal [overflow-wrap:anywhere]',
      render: (row) => formatSchedule(row),
    },
    { key: 'status', title: '状态', width: 120, render: (row) => <TaskStatusBadge status={row.status} /> },
    { key: 'next', title: '下次执行', width: 160, render: (row) => formatTime(row.next_run_at) },
    { key: 'runCount', title: '已执行', width: 120, render: (row) => `${row.run_count || 0} 次` },
    {
      key: 'lastResult',
      title: '最近结果',
      width: 120,
      render: (row) =>
        row.last_status ? (
          <TaskRunResultBadge status={row.last_status} />
        ) : (
          <span>暂无</span>
        ),
    },
    { key: 'actions', title: '操作', width: 100, render: renderTaskActions },
  ];

  const runColumns: DataTableColumn<ScheduledTaskRunRead>[] = [
    {
      key: 'task',
      title: '定时任务',
      width: 240,
      className: 'whitespace-normal',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate">{row.task_title || row.scheduled_task_id}</span>
          {row.task_status === 'archived' && <ArchivedTag />}
        </div>
      ),
    },
    { key: 'status', title: '状态', width: 120, render: (row) => <TaskRunResultBadge status={row.status} /> },
    {
      key: 'scheduled',
      title: '计划时间',
      width: 160,
      render: (row) => formatTime(row.scheduled_for),
    },
    {
      key: 'finished',
      title: '完成时间',
      width: 160,
      render: (row) => formatTime(row.finished_at),
    },
    {
      key: 'result',
      title: '结果',
      className: 'whitespace-normal',
      render: (row) => (
        <span className="wrap-break-word">{row.result_summary || row.error || '暂无'}</span>
      ),
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      render: (row) => (
        <UIButton
          variant="link"
          disabled={!row.session_id}
          onClick={() => openChatSession(row.session_id)}
          className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
        >
          查看会话
        </UIButton>
      ),
    },
  ];

  const runModalColumns: DataTableColumn<ScheduledTaskRunRead>[] = [
    {
      key: 'scheduled',
      title: '计划时间',
      width: 170,
      render: (row) => formatTime(row.scheduled_for),
    },
    { key: 'status', title: '状态', width: 100, render: (row) => <TaskRunResultBadge status={row.status} /> },
    {
      key: 'session',
      title: '会话',
      width: 200,
      className: 'whitespace-normal',
      render: (row) =>
        row.session_id ? (
          <button
            type="button"
            onClick={() => openChatSession(row.session_id)}
            className="max-w-full truncate text-left text-[#1a71ff] transition-colors hover:text-[#4a8dff]"
          >
            {row.session_id}
          </button>
        ) : (
          '未生成'
        ),
    },
    {
      key: 'result',
      title: '结果',
      className: 'whitespace-normal',
      render: (row) => (
        <span className="wrap-break-word">{row.result_summary || row.error || '暂无'}</span>
      ),
    },
  ];

  const renderTaskMobileCard = (row: ScheduledTaskRead) => (
    <article className={MOBILE_CARD_CLASS} key={row.id}>
      <div className={MOBILE_CARD_HEAD_CLASS}>
        <strong className={MOBILE_TITLE_CLASS}>{row.title}</strong>
        <TaskStatusBadge status={row.status} />
      </div>
      <p className={MOBILE_SUMMARY_CLASS}>{row.prompt}</p>
      <div className={MOBILE_META_CLASS}>
        <span>
          <b>计划</b>
          {formatSchedule(row)}
        </span>
        <span>
          <b>下次</b>
          {formatTime(row.next_run_at)}
        </span>
        <span>
          <b>已执行</b>
          {row.run_count || 0} 次
        </span>
        <span>
          <b>最近</b>
          {row.last_status ? <TaskRunResultBadge status={row.last_status} /> : '暂无'}
        </span>
      </div>
      <div className="mt-[12px] flex justify-end">{renderTaskActions(row)}</div>
    </article>
  );

  const renderRunMobileCard = (row: ScheduledTaskRunRead) => (
    <article className={MOBILE_CARD_CLASS} key={row.id}>
      <div className={MOBILE_CARD_HEAD_CLASS}>
        <strong className={MOBILE_TITLE_CLASS}>{row.task_title || row.scheduled_task_id}</strong>
        <TaskRunResultBadge status={row.status} />
      </div>
      {row.task_status === 'archived' && (
        <div className="mt-[10px]">
          <ArchivedTag />
        </div>
      )}
      <div className={MOBILE_META_CLASS}>
        <span>
          <b>计划时间</b>
          {formatTime(row.scheduled_for)}
        </span>
        <span>
          <b>完成时间</b>
          {formatTime(row.finished_at)}
        </span>
      </div>
      <p className={MOBILE_SUMMARY_CLASS}>{row.result_summary || row.error || '暂无结果'}</p>
      <div className="mt-[12px] flex justify-end">
        <UIButton
          variant="link"
          disabled={!row.session_id}
          onClick={() => openChatSession(row.session_id)}
          className="h-auto gap-1 p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
        >
          <IconSearch className="size-3.5" />
          查看会话
        </UIButton>
      </div>
    </article>
  );

  const actionButtons = (
    <div className="flex justify-end gap-[16px]">
      <UIButton
        onClick={() => navigate('/enterprise/scheduled-tasks/new')}
        disabled={createDisabled}
        className="h-8 w-[100px] gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]"
      >
        <IconAdd className="size-3.5" />
        新增任务
      </UIButton>
    </div>
  );

  const scheduledBody = selectedAgent?.is_overall ? (
    <div className="flex min-h-[200px] items-center justify-center rounded-[14px] bg-[#f6f6f6] text-[13px] text-[#858b9c]">
      请先选择一个数字员工再配置定时任务。
    </div>
  ) : (
    <>
      <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="定时任务统计">
        <StatCard label="待完成" value={activeRows.length} className="basis-[220px]" />
        <StatCard label="已完成" value={completedCount} className="basis-[220px]" />
        <StatCard label="执行记录" value={allRunRows.length} className="basis-[220px]" />
      </div>

      <div className="flex flex-col gap-[24px]">
        <TaskSection
          icon={<IconTable className="size-[14px] shrink-0" />}
          title="任务列表"
          filterTabs={TASK_FILTER_TABS}
          filter={taskFilter}
          onFilterChange={setTaskFilter}
          rows={visibleRows}
          pagedRows={taskPagination.pagedItems}
          columns={taskColumns}
          rowKey={(row) => row.id}
          loading={loading}
          emptyText="暂无定时任务"
          page={taskPagination.page}
          pageCount={taskPagination.pageCount}
          onPageChange={taskPagination.setPage}
          renderMobileCard={renderTaskMobileCard}
        />

        <TaskSection
          icon={<IconAlignJustify className="size-[14px] shrink-0" />}
          title="执行记录"
          filterTabs={RUN_FILTER_TABS}
          filter={runFilter}
          onFilterChange={setRunFilter}
          rows={visibleRunRows}
          pagedRows={runPagination.pagedItems}
          columns={runColumns}
          rowKey={(row) => row.id}
          loading={loading}
          emptyText="暂无执行记录"
          tableSize="compact"
          striped
          bordered
          page={runPagination.page}
          pageCount={runPagination.pageCount}
          onPageChange={runPagination.setPage}
          renderMobileCard={renderRunMobileCard}
        />
      </div>
    </>
  );

  return (
    <>
      <section
        aria-busy={loading}
        className="relative mt-[-2px] flex w-full min-w-0 max-w-full flex-col gap-[24px] overflow-hidden rounded-[18px] bg-white p-[14px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] *:min-w-0 min-[521px]:p-[18px]"
      >
        {actionButtons}
        {scheduledBody}
      </section>

      <Dialog open={runsOpen} onOpenChange={setRunsOpen}>
        <DialogContent
          aria-describedby={undefined}
          className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[920px]"
        >
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconAlignJustify className="size-[14px] shrink-0" />
            <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
              执行记录
            </DialogTitle>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <DataTable
              aria-label="执行记录"
              columns={runModalColumns}
              data={runsModalPagination.pagedItems}
              rowKey={(row) => row.id}
              loading={runLoading}
              emptyText="暂无执行记录"
              size="compact"
              striped
              bordered
            />
          </div>
          {runRows.length > 0 && (
            <Paginator
              aria-label="执行记录分页"
              className="mt-0"
              page={runsModalPagination.page}
              pageCount={runsModalPagination.pageCount}
              onChange={runsModalPagination.setPage}
            />
          )}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除定时任务「${deleteTarget?.title ?? ''}」？`}
        description="删除后不再唤醒该员工，历史执行记录会继续保留。"
        onConfirm={() => void confirmDelete()}
      />
    </>
  );
}

function ArchivedTag() {
  return (
    <StatusBadge tone="gray">任务已删除</StatusBadge>
  );
}
