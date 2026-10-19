import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ChevronLeft,
  ChevronRight,
  GitBranch,
  GitPullRequest,
  LoaderCircle,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  SendToBack,
  Settings2,
  Trash2,
  X,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import {
  syncAiReviewTaskToPlatform,
  deleteAiReviewTask,
  deleteAiReviewWorkspace,
  fetchAiReviewMergeRequests,
  fetchAiReviewTasks,
  fetchAiReviewWorkspaces,
  retryAiReviewTask,
  type AiReviewMergeRequest,
  type AiReviewTaskStatus,
  type AiReviewTaskSummary,
  type AiReviewWorkspace,
} from '../../api/aiReview';
import { ApiError } from '../../api/client';
import { cn } from '@/lib/utils';
import { formatClientMonthDayTime } from '@/lib/timezone';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import AgentAppBackButton from '../agentApps/AgentAppBackButton';
import {
  PLATFORM_META,
  TASK_STATUS_META,
  duplicateReviewBlockedText,
  isOcrSkipped,
  isTaskInFlight,
  pageNumbers,
  taskPollingInterval,
} from './aiReviewModel';
import AiReviewWorkspaceDialog from './AiReviewWorkspaceDialog';
import AiReviewSettingsDialog from './AiReviewSettingsDialog';
import CreateReviewTaskDialog from './CreateReviewTaskDialog';
import ReviewResultDialog from './ReviewResultDialog';
import type { EnterpriseAuthUser } from '../../auth';

const PAGE_SIZE = 20;

/** 列表分页条：page=0 表示无精确 total（hasMore 继续「下一页」）。 */
function ListPager({
  page,
  totalPages,
  totalLabel,
  hasMore,
  onGo,
}: {
  page: number;
  totalPages: number;
  totalLabel: string;
  hasMore: boolean;
  onGo: (page: number) => void;
}) {
  const pages = totalPages > 1 ? pageNumbers(page, totalPages) : [];
  const prev = () => onGo(Math.max(1, page - 1));
  const next = () => onGo(page + 1);
  return (
    <div className="flex items-center justify-between gap-[10px] pt-[2px]">
      <span className="text-[11px] tabular-nums text-[#a3aaba]">{totalLabel}</span>
      <div className="flex items-center gap-[4px]">
        <button
          type="button"
          aria-label="上一页"
          disabled={page <= 1}
          onClick={prev}
          className="grid size-[24px] place-items-center rounded-[7px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a] disabled:opacity-40"
        >
          <ChevronLeft className="size-[13px]" />
        </button>
        {pages.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => onGo(item)}
            className={cn(
              'grid size-[26px] place-items-center rounded-[7px] text-[11.5px] tabular-nums transition-colors',
              item === page
                ? 'bg-[#18181a] text-white'
                : 'border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] hover:bg-[#f6f6f6] hover:text-[#18181a]',
            )}
          >
            {item}
          </button>
        ))}
        <button
          type="button"
          aria-label="下一页"
          disabled={totalPages > 0 ? page >= totalPages : !hasMore}
          onClick={next}
          className="grid size-[26px] place-items-center rounded-[7px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a] disabled:opacity-40"
        >
          <ChevronRight className="size-[13px]" />
        </button>
      </div>
    </div>
  );
}

export function SectionCard({ children, className }: { children: React.ReactNode; className?: string }) {
  // Agent 广场工作台外壳里内容直接坐在定高大白卡上，这里只负责分组间距（原来是自带边框的独立小卡）。
  return (
    <section
      className={cn(
        'flex flex-col gap-[14px]',
        className,
      )}
    >
      {children}
    </section>
  );
}

export function GroupTitle({
  icon,
  title,
  hint,
}: {
  icon: React.ReactNode;
  title: string;
  hint?: string;
}) {
  return (
    <div className="flex items-center gap-[6px] text-[#757f9c]">
      {icon}
      <span className="text-[14px] font-normal leading-none text-[#464c5e]">{title}</span>
      {hint && <span className="text-[12px] text-[#a3aaba]">{hint}</span>}
    </div>
  );
}

export function PlatformChip({ platform }: { platform: string }) {
  const meta = PLATFORM_META[platform] ?? { label: platform, tone: 'bg-[#f3f4f6] text-[#464c5e]' };
  return (
    <span className={cn('rounded-[6px] px-[7px] py-[2px] text-[11px] leading-[16px] font-medium', meta.tone)}>
      {meta.label}
    </span>
  );
}

function StatusChip({ status }: { status: AiReviewTaskSummary['status'] }) {
  const meta = TASK_STATUS_META[status] ?? TASK_STATUS_META.queued;
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-[5px] rounded-[6px] px-[7px] py-[2px] text-[11px] leading-[16px] font-medium',
        meta.tone,
      )}
    >
      <span className={cn('size-[6px] rounded-full', meta.dot)} />
      {meta.label}
    </span>
  );
}

function formatTime(value: string | null): string {
  // 后端时间戳是 naive UTC，统一走 lib 里的规范实现（裸 new Date 在东八区会早 8 小时）
  return formatClientMonthDayTime(value ?? undefined, '');
}

function WorkspaceRow({
  workspace,
  active,
  onOpen,
  onDelete,
}: {
  workspace: AiReviewWorkspace;
  active: boolean;
  onOpen: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <div
      className={cn(
        'group cursor-pointer rounded-[12px] border-[0.5px] px-[12px] py-[10px] transition-colors',
        active ? 'border-[#cfe0ff] bg-[#f4f8ff]' : 'border-transparent bg-[#fafbfd] hover:bg-[#f6f6f6]',
      )}
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpen();
        }
      }}
    >
      <div className="flex items-center gap-[8px]">
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#18181a]">{workspace.name}</span>
        <PlatformChip platform={workspace.platform} />
        <span
          className={cn('shrink-0', confirming ? 'inline-flex items-center gap-[4px]' : 'hidden group-hover:inline-flex group-hover:items-center group-hover:gap-[4px]')}
          onClick={(event) => event.stopPropagation()}
        >
          {confirming ? (
            <>
              <button
                type="button"
                onClick={onDelete}
                className="rounded-[6px] bg-[#fce7e7] px-[8px] py-[2px] text-[11px] text-[#c0392b] transition-colors hover:bg-[#f8d3d3]"
              >
                确认删除
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="rounded-[6px] px-[8px] py-[2px] text-[11px] text-[#757f9c] transition-colors hover:bg-[#f3f4f6]"
              >
                取消
              </button>
            </>
          ) : (
            <button
              type="button"
              aria-label="删除该 workspace"
              title="删除 workspace 及其全部评审任务"
              onClick={() => setConfirming(true)}
              className="grid size-[22px] place-items-center rounded-[6px] text-[#a3aaba] transition-colors hover:bg-[#fce7e7] hover:text-[#c0392b]"
            >
              <Trash2 className="size-[13px]" />
            </button>
          )}
        </span>
      </div>
      <div className="mt-[3px] truncate font-mono text-[11px] text-[#a3aaba]">{workspace.repo_path}</div>
    </div>
  );
}

function MergeRequestRow({
  mr,
  inFlightStatus,
  onCreate,
}: {
  mr: AiReviewMergeRequest;
  /** 该 PR/MR 是否已有排队 / 执行中的任务（'' 表示没有） */
  inFlightStatus?: string | null;
  onCreate: () => void;
}) {
  const blocked = isTaskInFlight(inFlightStatus);
  const meta = blocked ? TASK_STATUS_META[inFlightStatus as AiReviewTaskStatus] : null;
  const blockedText = duplicateReviewBlockedText(mr.number, inFlightStatus);
  return (
    <div
      className={cn(
        'flex items-start gap-[10px] rounded-[12px] border-[0.5px] px-[14px] py-[10px]',
        blocked ? 'border-[#dbe6ff] bg-[#f7faff]' : 'border-[#eef0f4] bg-[#fafbfd]',
      )}
    >
      <GitPullRequest className="mt-[3px] size-[14px] shrink-0 text-[#1a71ff]" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-[6px]">
          <span className="shrink-0 text-[12px] font-medium tabular-nums text-[#757f9c]">#{mr.number}</span>
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#18181a]" title={mr.title}>
            {mr.title || '（无标题）'}
          </span>
        </div>
        {mr.description?.trim() && (
          <p className="mt-[3px] line-clamp-2 text-[11.5px] leading-[17px] text-[#858b9c]">{mr.description}</p>
        )}
        <div className="mt-[3px] flex flex-wrap items-center gap-x-[10px] gap-y-[2px] text-[11px] text-[#757f9c]">
          <span className="truncate font-mono">
            {mr.source_branch} → {mr.target_branch}
          </span>
          {mr.author && <span className="shrink-0">@{mr.author}</span>}
          {blocked && meta && (
            <span
              title={blockedText}
              className={cn(
                'inline-flex shrink-0 items-center gap-[4px] rounded-[6px] px-[6px] py-[1px] text-[10.5px]',
                meta.tone,
              )}
            >
              <span className={cn('size-[5px] shrink-0 rounded-full', meta.dot)} />
              {meta.label}
            </span>
          )}
        </div>
      </div>
      <UIButton
        type="button"
        disabled={blocked}
        aria-label={blocked ? blockedText : `发起 #${mr.number} 的评审`}
        onClick={onCreate}
        className={cn(
          'h-[28px] shrink-0 gap-[4px] rounded-[8px] border-[0.5px] px-[10px] text-[12px] font-normal',
          blocked
            ? 'cursor-not-allowed border-[#e3e7f1] bg-[#f6f6f6] text-[#a3aaba]'
            : 'border-[#cfe0ff] bg-[#f4f8ff] text-[#1a71ff] hover:bg-[#e9f1ff]',
        )}
      >
        {blocked ? '已有评审任务' : '发起评审'}
      </UIButton>
    </div>
  );
}

/** 当前 workspace 的 open PR/MR 列表：服务端分页 + 搜索，失败给重试。 */
function MrList({
  workspace,
  onCreate,
  reloadSignal,
  inFlightByMr,
}: {
  workspace: AiReviewWorkspace;
  onCreate: (mr: AiReviewMergeRequest) => void;
  reloadSignal: number;
  /** 任务列表轮询到的最新「进行中」状态（编号 → 状态），优先于列表接口的快照 */
  inFlightByMr: Map<number, string>;
}) {
  const [items, setItems] = useState<AiReviewMergeRequest[] | null>(null);
  const [pageMeta, setPageMeta] = useState<{ total: number | null; page: number; has_more: boolean }>({
    total: null,
    page: 1,
    has_more: false,
  });
  const [search, setSearch] = useState('');
  const [searchDraft, setSearchDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
      const result = await fetchAiReviewMergeRequests(workspace.id, {
        page: 1,
        pageSize: PAGE_SIZE,
        search,
      });
      setItems(result.items);
      setPageMeta({ total: result.total, page: 1, has_more: result.has_more });
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : '拉取 PR/MR 列表失败');
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [workspace.id, search]);

  const goToPage = useCallback(
    async (page: number) => {
      setError(null);
      setLoading(true);
      try {
        const result = await fetchAiReviewMergeRequests(workspace.id, {
          page,
          pageSize: PAGE_SIZE,
          search,
        });
        setItems(result.items);
        setPageMeta({ total: result.total, page: result.page, has_more: result.has_more });
      } catch (cause) {
        setError(cause instanceof ApiError || cause instanceof Error ? cause.message : '拉取 PR/MR 列表失败');
      } finally {
        setLoading(false);
      }
    },
    [workspace.id, search],
  );

  useEffect(() => {
    setSearchDraft('');
    setSearch('');
  }, [workspace.id]);

  useEffect(() => {
    void reload();
  }, [reload, reloadSignal]);

  return (
    <>
      <div className="flex items-center gap-[8px]">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-[10px] top-1/2 size-[13px] -translate-y-1/2 text-[#a3aaba]" />
          <input
            value={searchDraft}
            placeholder="搜索标题 / 作者 / 编号（如 #42）"
            onChange={(event) => setSearchDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                setSearch(searchDraft);
              }
            }}
            className="h-[32px] w-full rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white pl-[28px] pr-[10px] text-[12px] text-[#18181a] outline-none transition-colors placeholder:text-[#a3aaba] focus:border-[#cfe0ff] focus:ring-2 focus:ring-[#e9f1ff]"
          />
        </div>
        {search !== searchDraft && (
          <UIButton
            type="button"
            onClick={() => setSearch(searchDraft)}
            className="h-[32px] shrink-0 rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
          >
            搜索
          </UIButton>
        )}
        {search && (
          <button
            type="button"
            aria-label="清除搜索"
            onClick={() => {
              setSearchDraft('');
              setSearch('');
            }}
            className="grid size-[30px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            <X className="size-[13px]" />
          </button>
        )}
      </div>

      {loading ? (
        <div className="flex items-center gap-[8px] py-[18px] text-[12px] text-[#a3aaba]">
          <LoaderCircle className="size-[13px] animate-spin" />
          正在拉取 open 状态的 PR / MR…
        </div>
      ) : error ? (
        <div className="flex flex-col items-start gap-[8px] rounded-[12px] border-[0.5px] border-[#f3d28b] bg-[#fff8e8] px-[14px] py-[12px] text-[12px] leading-[18px] text-[#6f4500]">
          <span className="font-medium">拉取 PR / MR 失败</span>
          <span>{error}</span>
          <button
            type="button"
            onClick={() => void goToPage(pageMeta.page || 1)}
            className="mt-[2px] inline-flex items-center gap-[4px] rounded-[8px] border-[0.5px] border-[#e3d0a6] bg-white px-[10px] py-[3px] text-[11.5px] text-[#6f4500] transition-colors hover:bg-[#fdf4df]"
          >
            <RotateCcw className="size-[12px]" />
            重试
          </button>
        </div>
      ) : (items ?? []).length === 0 ? (
        <div className="rounded-[12px] border-[0.5px] border-dashed border-[#e3e7f1] bg-[#fafbfd] px-[14px] py-[18px] text-[12px] leading-[18px] text-[#757f9c]">
          {search
            ? `没有匹配「${search}」的 open PR / MR。换个关键词，或清空搜索看全量列表。`
            : '该仓库当前没有 open 状态的 PR / MR。可以在「评审任务」下方手动指定一个已存在的编号。'}
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-[8px]">
            {(items ?? []).map((mr) => (
              <MergeRequestRow
                key={mr.number}
                mr={mr}
                inFlightStatus={inFlightByMr.get(mr.number) ?? mr.in_flight_status ?? ''}
                onCreate={() => onCreate(mr)}
              />
            ))}
          </div>
          <ListPager
            page={pageMeta.page}
            totalPages={pageMeta.total !== null ? Math.max(1, Math.ceil(pageMeta.total / PAGE_SIZE)) : 0}
            totalLabel={
              pageMeta.total !== null
                ? `共 ${pageMeta.total} 个`
                : `第 ${pageMeta.page} 页${pageMeta.has_more ? ' · 还有更多' : ''}`
            }
            hasMore={pageMeta.has_more}
            onGo={(page) => void goToPage(page)}
          />
        </>
      )}
    </>
  );
}

function TaskRow({
  task,
  onOpenResult,
  onRetry,
  onDelete,
  busy,
}: {
  task: AiReviewTaskSummary;
  onOpenResult: () => void;
  onRetry: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  return (
    <div className="flex items-start gap-[10px] rounded-[12px] border-[0.5px] border-[#eef0f4] bg-[#fafbfd] px-[14px] py-[10px]">
      <StatusChip status={task.status} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-[6px]">
          <span className="shrink-0 text-[12px] font-medium tabular-nums text-[#757f9c]">#{task.mr_number}</span>
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#18181a]" title={task.mr_title}>
            {task.mr_title || '（无标题）'}
          </span>
        </div>
        {task.error ? (
          <p className="mt-[3px] line-clamp-2 text-[11.5px] leading-[17px] text-[#c0392b]">{task.error}</p>
        ) : (
          <div className="mt-[3px] flex flex-wrap items-center gap-x-[10px] gap-y-[2px] text-[11px] text-[#757f9c]">
            <span className="truncate font-mono">
              {task.source_branch} → {task.target_branch}
            </span>
            <span className="shrink-0">{formatTime(task.created_at)}</span>
            {isOcrSkipped(task.ocr_status) && (
              <span
                title="ocr 没有找到可评审的文件：diff 为空，或变更文件类型不在默认白名单（.txt/.md/.lock 等）"
                className="inline-flex shrink-0 items-center gap-[3px] rounded-[6px] bg-[#fff7e8] px-[6px] py-[1px] text-[10.5px] text-[#8a4b00]"
              >
                无可评审文件
              </span>
            )}
            {task.platform_synced_at && (
              <a
                href={task.platform_sync_url || task.web_url}
                target="_blank"
                rel="noreferrer"
                onClick={(event) => event.stopPropagation()}
                title={`已回写平台评论区 · ${formatTime(task.platform_synced_at)}`}
                className="inline-flex shrink-0 items-center gap-[3px] rounded-[6px] bg-[#e9f7ef] px-[6px] py-[1px] text-[10.5px] text-[#1a7f4b] transition-colors hover:bg-[#ddf2e5]"
              >
                <SendToBack className="size-[10px]" />
                已回写
              </a>
            )}
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-[4px]">
        {task.status === 'succeeded' && (
          <UIButton
            type="button"
            disabled={busy}
            onClick={onOpenResult}
            className="h-[28px] shrink-0 gap-[4px] rounded-[8px] border-[0.5px] border-[#cfe0ff] bg-[#f4f8ff] px-[10px] text-[12px] font-normal text-[#1a71ff] hover:bg-[#e9f1ff] disabled:opacity-50"
          >
            查看结果
          </UIButton>
        )}
        {(task.status === 'failed' || task.status === 'queued') && (
          <button
            type="button"
            aria-label="重试该任务"
            title="重新入队"
            disabled={busy}
            onClick={onRetry}
            className="grid size-[28px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a] disabled:opacity-50"
          >
            {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : <RotateCcw className="size-[13px]" />}
          </button>
        )}
        <button
          type="button"
          aria-label="删除该任务"
          disabled={busy}
          onClick={onDelete}
          className="grid size-[28px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#fce7e7] hover:text-[#c0392b] disabled:opacity-50"
        >
          <Trash2 className="size-[13px]" />
        </button>
      </div>
    </div>
  );
}

export default function AiReviewerPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [workspaces, setWorkspaces] = useState<AiReviewWorkspace[]>([]);
  const [workspacesLoading, setWorkspacesLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const [tasks, setTasks] = useState<AiReviewTaskSummary[]>([]);
  const [taskMeta, setTaskMeta] = useState<{ total: number; page: number; total_pages: number }>({
    total: 0,
    page: 1,
    total_pages: 1,
  });
  const [taskSearch, setTaskSearch] = useState('');
  const [taskSearchDraft, setTaskSearchDraft] = useState('');
  const [taskPage, setTaskPage] = useState(1);
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);

  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [createTaskMr, setCreateTaskMr] = useState<AiReviewMergeRequest | null>(null);
  const [resultTaskId, setResultTaskId] = useState<string | null>(null);
  const [mrReloadSignal, setMrReloadSignal] = useState(0);

  const selected = useMemo(
    () => workspaces.find((workspace) => workspace.id === selectedId) ?? null,
    [workspaces, selectedId],
  );

  const reloadWorkspaces = useCallback(async () => {
    setWorkspacesLoading(true);
    try {
      const rows = await fetchAiReviewWorkspaces();
      setWorkspaces(rows);
      setSelectedId((prev) => (prev && rows.some((row) => row.id === prev) ? prev : rows[0]?.id ?? null));
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '加载 workspace 失败');
    } finally {
      setWorkspacesLoading(false);
    }
  }, []);

  const reloadTasks = useCallback(
    async (workspaceId: string | null, page = taskPage, search = taskSearch) => {
      if (!workspaceId) {
        setTasks([]);
        setTaskMeta({ total: 0, page: 1, total_pages: 1 });
        return;
      }
      try {
        const result = await fetchAiReviewTasks(workspaceId, {
          page,
          pageSize: PAGE_SIZE,
          search,
        });
        setTasks(result.items);
        setTaskMeta({ total: result.total, page: result.page, total_pages: result.total_pages });
      } catch {
        // 轮询失败静默：下一轮再试，避免抖动弹 toast
      }
    },
    [taskPage, taskSearch],
  );

  // 切 workspace：任务页码与搜索全部重置
  useEffect(() => {
    setTaskPage(1);
    setTaskSearch('');
    setTaskSearchDraft('');
  }, [selectedId]);

  useEffect(() => {
    void reloadWorkspaces();
  }, [reloadWorkspaces]);

  useEffect(() => {
    void reloadTasks(selectedId);
  }, [selectedId, taskSearch, taskPage, reloadTasks]);

  // 有活跃任务 3s 轮询；全闲 10s 保底（任务列表变化时重设定时器）
  const hasActiveTask = tasks.some((task) => task.status === 'queued' || task.status === 'running');
  useEffect(() => {
    if (!selectedId) return;
    const timer = window.setInterval(
      () => void reloadTasks(selectedId),
      hasActiveTask ? 3000 : 10000,
    );
    return () => window.clearInterval(timer);
  }, [selectedId, hasActiveTask, reloadTasks]);

  const handleDeleteWorkspace = useCallback(
    async (workspace: AiReviewWorkspace) => {
      try {
        await deleteAiReviewWorkspace(workspace.id);
        notify.success(`已删除 workspace「${workspace.name}」及其评审任务`);
        await reloadWorkspaces();
      } catch (cause) {
        notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '删除失败');
      }
    },
    [reloadWorkspaces],
  );

  const handleRetry = useCallback(
    async (task: AiReviewTaskSummary) => {
      setBusyTaskId(task.id);
      try {
        const result = await retryAiReviewTask(task.id);
        if (result.scheduled) notify.success('已重新入队');
        else notify.warning('调度通道暂不可用，任务保持排队，稍后会自动重试');
        await reloadTasks(selectedId);
      } catch (cause) {
        notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '重试失败');
      } finally {
        setBusyTaskId(null);
      }
    },
    [selectedId, reloadTasks],
  );

  const handleDeleteTask = useCallback(
    async (task: AiReviewTaskSummary) => {
      setBusyTaskId(task.id);
      try {
        await deleteAiReviewTask(task.id);
        notify.success('任务已删除');
        await reloadTasks(selectedId);
        setMrReloadSignal((value) => value + 1);
      } catch (cause) {
        notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '删除失败');
      } finally {
        setBusyTaskId(null);
      }
    },
    [selectedId, reloadTasks],
  );

  /** 当前任务页里的进行中任务 → 编号索引：用于即时禁用「发起评审」。 */
  const inFlightByMr = useMemo(() => {
    const map = new Map<number, string>();
    for (const task of tasks) {
      if (isTaskInFlight(task.status) && !map.has(task.mr_number)) {
        map.set(task.mr_number, task.status);
      }
    }
    return map;
  }, [tasks]);

  const inFlightCount = inFlightByMr.size;

  // 进行中集合变化（新任务入队 / 任务收尾）时刷一次 PR/MR 列表，
  // 让服务端返回的 in_flight_status 跟上，避免行状态与任务状态打架。
  const inFlightSignature = useMemo(
    () =>
      [...inFlightByMr.entries()]
        .sort((left, right) => left[0] - right[0])
        .map(([number, status]) => `${number}:${status}`)
        .join(','),
    [inFlightByMr],
  );
  const inFlightSignatureRef = useRef(inFlightSignature);
  useEffect(() => {
    if (inFlightSignatureRef.current === inFlightSignature) return;
    inFlightSignatureRef.current = inFlightSignature;
    setMrReloadSignal((value) => value + 1);
  }, [inFlightSignature]);

  return (
    <div className="min-h-full box-border px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="Agent 广场 · AI CodeReviewer"
      />

      {/* 与 PPT Studio / 决策助手统一的工作台外壳：一张定高大白卡，栏内各自滚动 */}
      <div className="flex h-[calc(100vh-190px)] min-h-[560px] flex-col gap-[14px] rounded-[20px] bg-white p-[16px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        {/* 标题信息交给页头，卡片内只留右上角返回入口 */}
        <div className="flex flex-wrap items-center justify-end gap-[10px]">
          <AgentAppBackButton />
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[14px] xl:flex-row">
          {/* 左栏：workspace 固定宽，栏内滚动 */}
          <div className="flex min-h-0 flex-col xl:w-[300px] xl:shrink-0 xl:border-r-[0.5px] xl:border-[#eef0f5] xl:pr-[16px]">
            <SectionCard className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap items-center justify-between gap-[10px]">
            <GroupTitle
              icon={<GitBranch className="size-[14px] shrink-0" />}
              title="Workspace"
              hint={`共 ${workspaces.length} 个`}
            />
            <div className="flex items-center gap-[6px]">
              <button
                type="button"
                aria-label="平台设置"
                title="平台 token 与全局评审要求"
                onClick={() => setSettingsOpen(true)}
                className="grid size-[30px] place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
              >
                <Settings2 className="size-[14px]" />
              </button>
              <UIButton
                type="button"
                onClick={() => setWorkspaceDialogOpen(true)}
                className="h-[30px] shrink-0 gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
              >
                <Plus className="size-[13px]" />
                新建
              </UIButton>
            </div>
          </div>

          {workspacesLoading ? (
            <div className="flex items-center gap-[8px] py-[24px] text-[12px] text-[#a3aaba]">
              <LoaderCircle className="size-[13px] animate-spin" />
              正在加载 workspace…
            </div>
          ) : workspaces.length === 0 ? (
            <div className="flex flex-col items-start gap-[8px] rounded-[12px] border-[0.5px] border-dashed border-[#e3e7f1] bg-[#fafbfd] px-[14px] py-[18px] text-[12px] leading-[18px] text-[#757f9c]">
              <span className="inline-flex items-center gap-[6px] font-medium text-[#464c5e]">
                <GitBranch className="size-[13px]" />
                还没有 workspace
              </span>
              一个 workspace 对应一个待评审仓库。先到右上角「平台设置」填 GitHub / GitLab access
              token，再点「新建」添加仓库。
            </div>
          ) : (
            <div className="flex flex-col gap-[6px]">
              {workspaces.map((workspace) => (
                <WorkspaceRow
                  key={workspace.id}
                  workspace={workspace}
                  active={workspace.id === selectedId}
                  onOpen={() => setSelectedId(workspace.id)}
                  onDelete={() => void handleDeleteWorkspace(workspace)}
                />
              ))}
            </div>
          )}

          {selected && (
            <div className="shrink-0 rounded-[12px] bg-[#fafbfd] px-[12px] py-[10px] text-[11px] leading-[16px] text-[#a3aaba]">
              <span className="font-mono">{selected.repo_url}</span>
              <div className="mt-[2px]">
                默认分支 <span className="font-mono text-[#757f9c]">{selected.default_branch}</span> ·{' '}
                {taskMeta.total > 0 ? `${taskMeta.total} 个评审任务` : '暂无评审任务'}
              </div>
            </div>
          )}
            </SectionCard>
          </div>

          {/* 右栏：待评审 PR/MR 与评审任务，栏内滚动 */}
          {!selected ? (
            <SectionCard className="min-h-0 flex-1 items-center justify-center py-[60px]">
              <div className="flex flex-col items-center gap-[10px] text-center">
                <span className="grid size-[44px] place-items-center rounded-[14px] bg-[#f3f4f6] text-[#a3aaba]">
                  <GitBranch className="size-[20px]" />
                </span>
                <p className="text-[13px] font-medium text-[#464c5e]">选择左侧 workspace 后开始评审</p>
                <p className="text-[12px] text-[#757f9c]">
                  右侧会展示该仓库 open 状态的 PR / MR，逐个发起异步评审。
                </p>
              </div>
            </SectionCard>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto">
            <SectionCard>
              <div className="flex flex-wrap items-center justify-between gap-[10px]">
                <GroupTitle
                  icon={<GitPullRequest className="size-[14px] shrink-0" />}
                  title="待评审 PR / MR"
                  hint="实时拉取 open 状态"
                />
              </div>
              <MrList
                workspace={selected}
                onCreate={(mr) => setCreateTaskMr(mr)}
                reloadSignal={mrReloadSignal}
                inFlightByMr={inFlightByMr}
              />
            </SectionCard>

            <SectionCard>
              <div className="flex flex-wrap items-center justify-between gap-[10px]">
                <GroupTitle
                  icon={<Pencil className="size-[14px] shrink-0" />}
                  title="评审任务"
                  hint={inFlightCount > 0 ? `${inFlightCount} 个进行中 · 自动刷新` : '全部已结束'}
                />
              </div>
              <div className="flex items-center gap-[8px]">
                <div className="relative flex-1">
                  <Search className="pointer-events-none absolute left-[10px] top-1/2 size-[13px] -translate-y-1/2 text-[#a3aaba]" />
                  <input
                    value={taskSearchDraft}
                    placeholder="搜索标题 / 作者 / 编号（如 #42）"
                    onChange={(event) => setTaskSearchDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault();
                        setTaskSearch(taskSearchDraft);
                      }
                    }}
                    className="h-[32px] w-full rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white pl-[28px] pr-[10px] text-[12px] text-[#18181a] outline-none transition-colors placeholder:text-[#a3aaba] focus:border-[#cfe0ff] focus:ring-2 focus:ring-[#e9f1ff]"
                  />
                </div>
                {taskSearch !== taskSearchDraft && (
                  <UIButton
                    type="button"
                    onClick={() => setTaskSearch(taskSearchDraft)}
                    className="h-[32px] shrink-0 rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
                  >
                    搜索
                  </UIButton>
                )}
                {taskSearch && (
                  <button
                    type="button"
                    aria-label="清除任务搜索"
                    onClick={() => {
                      setTaskSearchDraft('');
                      setTaskSearch('');
                    }}
                    className="grid size-[30px] shrink-0 place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] transition-colors hover:bg-[#f6f6f6] hover:text-[#18181a]"
                  >
                    <X className="size-[13px]" />
                  </button>
                )}
              </div>
              {tasks.length === 0 ? (
                <div className="rounded-[12px] border-[0.5px] border-dashed border-[#e3e7f1] bg-[#fafbfd] px-[14px] py-[18px] text-[12px] leading-[18px] text-[#757f9c]">
                  {taskSearch
                    ? `没有匹配「${taskSearch}」的评审任务。换个关键词，或清空搜索看全量列表。`
                    : '还没有评审任务。在上方挑一条 PR / MR 点「发起评审」，或手动指定编号；任务交给后台队列跑（通常几分钟）， 完成后状态会自动刷新为「已完成」。'}
                </div>
              ) : (
                <>
                  <div className="flex flex-col gap-[8px]">
                    {tasks.map((task) => (
                      <TaskRow
                        key={task.id}
                        task={task}
                        busy={busyTaskId === task.id}
                        onOpenResult={() => setResultTaskId(task.id)}
                        onRetry={() => void handleRetry(task)}
                        onDelete={() => void handleDeleteTask(task)}
                      />
                    ))}
                  </div>
                  <ListPager
                    page={taskMeta.page}
                    totalPages={taskMeta.total_pages}
                    totalLabel={`共 ${taskMeta.total} 条`}
                    hasMore={false}
                    onGo={(page) => setTaskPage(page)}
                  />
                </>
              )}
            </SectionCard>
            </div>
          )}
      </div>
      </div>

      <AiReviewWorkspaceDialog
        open={workspaceDialogOpen}
        onClose={() => setWorkspaceDialogOpen(false)}
        onCreated={() => void reloadWorkspaces()}
      />
      <AiReviewSettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <CreateReviewTaskDialog
        open={createTaskMr !== null}
        onClose={() => setCreateTaskMr(null)}
        workspace={selected}
        initialMr={createTaskMr}
        inFlightByMr={inFlightByMr}
        onCreated={() => {
          void reloadTasks(selectedId);
          setMrReloadSignal((value) => value + 1);
        }}
      />
      <ReviewResultDialog
        taskId={resultTaskId}
        onClose={() => setResultTaskId(null)}
        onOpenSettings={() => {
          // 先关结果弹窗再开设置：两个 Dialog 叠加会互相抢焦点与滚动锁
          setResultTaskId(null);
          setSettingsOpen(true);
        }}
      />
    </div>
  );
}
