import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Clock,
  Download,
  FileSearch,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  Workflow,
  Wrench,
} from 'lucide-react';

import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { DetailField } from '@/components/DetailField';
import { Paginator } from '@/components/Paginator';
import { StatCard } from '@/components/StatCard';
import { Button as UIButton } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Checkbox,
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  UnderlineTabs,
  type UnderlineTabItem,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { SELECT_TRIGGER_CLASS, formatDateTime } from '@/lib/enterprise-ui';
import { MarkdownMessage } from '../chat/chatHelpers';

import { api, TENANT_ID } from '../../api/client';
import IconCalendar from '../../assets/icons/profile-calendar.svg?react';
import { employeeDisplayNameWithCreator } from '../../employee';
import { useClientPagination } from '../../hooks/useClientPagination';
import { StatusBadge } from '../scheduled-tasks/StatusBadge';
import type { BadgeTone } from '../scheduled-tasks/shared';
import type {
  AgentProfileRead,
  EnterpriseChatSessionRead,
  EnterpriseSessionDetailRead,
  FeedbackAnalysisRead,
  FeedbackMessageRead,
  FeedbackSessionRead,
  FeedbackSummaryRead,
  TraceLineRead,
  TurnTraceRead,
} from '../../types';
import {
  buildConversationUserOptions,
  matchesConversationLogFilter,
  type ConversationLogFilter,
  type ConversationLogRow,
} from './conversationLogFilters';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const FEEDBACK_PAGE_SIZE = 10;
const ALL_CONVERSATION_USERS = '__all_conversation_users__';

type ConversationDetail = {
  session: Record<string, unknown>;
  messages: FeedbackMessageRead[];
  feedback: Array<Record<string, unknown>>;
  events: EnterpriseSessionDetailRead['events'];
  traces: TurnTraceRead[];
  toolInvocations: NonNullable<EnterpriseSessionDetailRead['tool_invocations']>;
};

const FILTER_TABS: UnderlineTabItem<ConversationLogFilter>[] = [
  { label: '全部', value: 'all' },
  { label: '好评', value: 'up' },
  { label: '差评', value: 'down' },
  { label: '未评价', value: 'unrated' },
  { label: '能力不足', value: 'ability' },
  { label: '工具问题', value: 'tool' },
  { label: '知识缺失', value: 'knowledge' },
  { label: 'SOP 问题', value: 'sop' },
];

const MOBILE_CARD_CLASS =
  'min-w-0 rounded-[8px] border border-[#eceef1] bg-white p-[14px]';

export default function ConversationLogsTab() {
  const [searchParams] = useSearchParams();
  const [scopedAgentId, setScopedAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '',
  );
  const agentId = searchParams.get('agent_id') || scopedAgentId;
  const [sessions, setSessions] = useState<EnterpriseChatSessionRead[]>([]);
  const [downRows, setDownRows] = useState<FeedbackSessionRead[]>([]);
  const [upRows, setUpRows] = useState<FeedbackSessionRead[]>([]);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [summary, setSummary] = useState<FeedbackSummaryRead | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [filter, setFilter] = useState<ConversationLogFilter>('all');
  const [conversationUserId, setConversationUserId] = useState(ALL_CONVERSATION_USERS);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reanalyzingId, setReanalyzingId] = useState<string | null>(null);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(() => new Set());
  const [exportingKey, setExportingKey] = useState('');

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      setScopedAgentId(
        (event as CustomEvent<{ agentId?: string }>).detail?.agentId ||
          window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) ||
          '',
      );
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  const load = async () => {
    setLoading(true);
    const agentQuery = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    // Load each source independently so one failing endpoint doesn't blank the whole tab.
    const [sessionResult, downResult, upResult, summaryResult, agentResult] = await Promise.allSettled([
      api.get<EnterpriseChatSessionRead[]>(`/api/enterprise/sessions?tenant_id=${TENANT_ID}${agentQuery}`),
      api.get<FeedbackSessionRead[]>(`/api/enterprise/feedback/sessions?tenant_id=${TENANT_ID}&rating=down${agentQuery}`),
      api.get<FeedbackSessionRead[]>(`/api/enterprise/feedback/sessions?tenant_id=${TENANT_ID}&rating=up${agentQuery}`),
      api.get<FeedbackSummaryRead>(`/api/enterprise/feedback/summary?tenant_id=${TENANT_ID}${agentQuery}`),
      api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`),
    ]);
    if (sessionResult.status === 'fulfilled') setSessions(sessionResult.value);
    if (downResult.status === 'fulfilled') setDownRows(downResult.value);
    if (upResult.status === 'fulfilled') setUpRows(upResult.value);
    if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value);
    if (agentResult.status === 'fulfilled') setAgents(agentResult.value);
    const failure = [sessionResult, downResult, upResult, summaryResult, agentResult].find(
      (item): item is PromiseRejectedResult => item.status === 'rejected',
    );
    if (failure) {
      notify.error(failure.reason instanceof Error ? failure.reason.message : '部分对话日志数据加载失败');
    }
    setLoading(false);
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  useEffect(() => {
    setConversationUserId(ALL_CONVERSATION_USERS);
  }, [agentId]);

  const rows = useMemo<ConversationLogRow[]>(() => {
    const downBySession = new Map(downRows.map((item) => [item.session_id, item]));
    const upBySession = new Map(upRows.map((item) => [item.session_id, item]));
    return sessions
      .filter((session) => !agentId || session.agent_id === agentId)
      .map((session) => ({
        ...session,
        downFeedback: downBySession.get(session.id),
        upFeedback: upBySession.get(session.id),
      }));
  }, [agentId, downRows, sessions, upRows]);

  const agentsById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);

  const agentLabelFromId = (rowAgentId?: string | null): string => {
    if (!rowAgentId) return '-';
    const agent = agentsById.get(rowAgentId);
    return agent ? employeeDisplayNameWithCreator(agent) : rowAgentId;
  };

  const agentLabel = (row: ConversationLogRow): string => agentLabelFromId(row.agent_id);

  const logFilterRows = useMemo(
    () => rows.filter((row) => matchesConversationLogFilter(row, filter)),
    [filter, rows],
  );

  const conversationUserOptions = useMemo(
    () => buildConversationUserOptions(logFilterRows),
    [logFilterRows],
  );

  const filteredRows = useMemo(
    () => logFilterRows.filter((row) => (
      conversationUserId === ALL_CONVERSATION_USERS || row.user_id === conversationUserId
    )),
    [conversationUserId, logFilterRows],
  );

  useEffect(() => {
    if (loading || conversationUserId === ALL_CONVERSATION_USERS) return;
    if (!conversationUserOptions.some((option) => option.userId === conversationUserId)) {
      setConversationUserId(ALL_CONVERSATION_USERS);
    }
  }, [conversationUserId, conversationUserOptions, loading]);

  const pagination = useClientPagination(
    filteredRows,
    FEEDBACK_PAGE_SIZE,
    `${filter}:${conversationUserId}`,
  );

  useEffect(() => {
    const visibleIds = new Set(filteredRows.map((row) => row.id));
    setSelectedSessionIds((current) => {
      const next = new Set([...current].filter((sessionId) => visibleIds.has(sessionId)));
      if (next.size === current.size && [...next].every((sessionId) => current.has(sessionId))) {
        return current;
      }
      return next;
    });
  }, [filteredRows]);

  const pageSessionIds = pagination.pagedItems.map((row) => row.id);
  const allPageRowsSelected =
    pageSessionIds.length > 0 && pageSessionIds.every((sessionId) => selectedSessionIds.has(sessionId));
  const somePageRowsSelected = pageSessionIds.some((sessionId) => selectedSessionIds.has(sessionId));
  const batchRows = selectedSessionIds.size
    ? filteredRows.filter((row) => selectedSessionIds.has(row.id))
    : filteredRows;

  const toggleSessionSelection = (sessionId: string, selected: boolean) => {
    setSelectedSessionIds((current) => {
      const next = new Set(current);
      if (selected) next.add(sessionId);
      else next.delete(sessionId);
      return next;
    });
  };

  const togglePageSelection = (selected: boolean) => {
    setSelectedSessionIds((current) => {
      const next = new Set(current);
      pageSessionIds.forEach((sessionId) => {
        if (selected) next.add(sessionId);
        else next.delete(sessionId);
      });
      return next;
    });
  };

  const exportSingleSession = async (row: ConversationLogRow) => {
    setExportingKey(row.id);
    try {
      const blob = await api.blob(
        `/api/enterprise/sessions/${encodeURIComponent(row.id)}/export?tenant_id=${TENANT_ID}`,
      );
      downloadBlob(blob, `staffdeck-conversation-log-${safeFilenamePart(row.id)}.json`);
      notify.success('对话日志 JSON 已导出');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '导出对话日志失败');
    } finally {
      setExportingKey('');
    }
  };

  const exportBatch = async () => {
    const sessionIds = batchRows.map((row) => row.id);
    if (sessionIds.length === 0) return;
    if (sessionIds.length > 500) {
      notify.error('单次最多导出 500 条对话日志，请缩小筛选范围后重试');
      return;
    }
    setExportingKey('batch');
    try {
      const blob = await api.postBlob(
        `/api/enterprise/sessions/export?tenant_id=${TENANT_ID}`,
        { session_ids: sessionIds },
      );
      downloadBlob(blob, `staffdeck-conversation-logs-${filenameTimestamp()}.json`);
      notify.success(`已导出 ${sessionIds.length} 条对话日志`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '批量导出对话日志失败');
    } finally {
      setExportingKey('');
    }
  };

  const openDetail = async (row: ConversationLogRow) => {
    setDetailLoading(true);
    try {
      const sessionDetail = await api.get<EnterpriseSessionDetailRead>(
        `/api/enterprise/sessions/${row.id}?tenant_id=${TENANT_ID}`,
      );
      setDetail({
        session: sessionDetail.session,
        messages: sessionDetail.messages,
        feedback: sessionDetail.feedback || [],
        events: sessionDetail.events || [],
        traces: sessionDetail.traces || [],
        toolInvocations: sessionDetail.tool_invocations || [],
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载对话详情失败');
    } finally {
      setDetailLoading(false);
    }
  };

  const reloadCurrentDetail = async () => {
    const sessionId = String(detail?.session?.id || detail?.session?.session_id || '');
    if (!sessionId) return;
    const row = rows.find((item) => item.id === sessionId);
    if (row) await openDetail(row);
  };

  const reanalyzeFeedback = async (feedbackId: string) => {
    setReanalyzingId(feedbackId);
    try {
      await api.post(`/api/enterprise/feedback/${feedbackId}/reanalyze?tenant_id=${TENANT_ID}`);
      notify.success('已重新提交后台分析');
      await reloadCurrentDetail();
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重新分析失败');
    } finally {
      setReanalyzingId(null);
    }
  };

  const columns: DataTableColumn<ConversationLogRow>[] = [
    {
      key: 'selection',
      title: (
        <Checkbox
          aria-label="选择当前页对话日志"
          checked={allPageRowsSelected ? true : somePageRowsSelected ? 'indeterminate' : false}
          onCheckedChange={(checked) => togglePageSelection(checked === true)}
        />
      ),
      width: 46,
      align: 'center',
      render: (row) => (
        <Checkbox
          aria-label={`选择对话日志 ${row.title || row.id}`}
          checked={selectedSessionIds.has(row.id)}
          onCheckedChange={(checked) => toggleSessionSelection(row.id, checked === true)}
        />
      ),
    },
    {
      key: 'title',
      title: '对话任务',
      width: 200,
      className: 'whitespace-normal text-[#18181a]',
      render: (row) => (
        <span className="line-clamp-1 wrap-break-word">
          {row.title || row.summary || row.last_agent_question || row.id}
        </span>
      ),
    },
    {
      key: 'agent',
      title: '数字员工',
      width: 180,
      render: (row) => <span className="block truncate" title={agentLabel(row)}>{agentLabel(row)}</span>,
    },
    {
      key: 'source',
      title: '来源/用户',
      width: 140,
      render: (row) => (
        <div className="flex min-w-0 flex-col items-start gap-[4px]">
          <ChannelBadge channel={row.channel} />
          <span className="max-w-full truncate text-[11px] text-[#a0a6b8]">
            {row.session_display_name || row.session_username || '-'}
          </span>
        </div>
      ),
    },
    {
      key: 'status',
      title: '状态',
      width: 120,
      render: (row) => (
        <div className="flex flex-wrap gap-[4px]">
          {row.downFeedback && <StatusBadge tone="red">差评</StatusBadge>}
          {row.upFeedback && <StatusBadge tone="green">好评</StatusBadge>}
          {!row.upFeedback && !row.downFeedback && <StatusBadge tone="blue">未评价</StatusBadge>}
        </div>
      ),
    },
    {
      key: 'attribution',
      title: '问题归因',
      width: 130,
      render: (row) => (
        <span>
          {row.downFeedback
            ? row.downFeedback.primary_bucket_label || row.downFeedback.primary_bucket || '待分析'
            : '暂无缺口'}
        </span>
      ),
    },
    {
      key: 'latest',
      title: '最近内容',
      className: 'whitespace-normal',
      render: (row) => (
        <span className="line-clamp-1 wrap-break-word">
          {row.downFeedback?.latest_message ||
            row.upFeedback?.latest_message ||
            row.summary ||
            row.last_agent_question ||
            '-'}
        </span>
      ),
    },
    {
      key: 'updated',
      title: '时间',
      width: 170,
      render: (row) => formatDateTime(row.updated_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 150,
      render: (row) => (
        <div className="flex items-center gap-[12px]">
          <UIButton
            variant="link"
            disabled={Boolean(exportingKey)}
            onClick={() => void exportSingleSession(row)}
            className="h-auto gap-[4px] p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
          >
            {exportingKey === row.id ? (
              <LoaderCircle className="size-[12px] animate-spin" />
            ) : (
              <Download className="size-[12px]" />
            )}
            JSON
          </UIButton>
          <UIButton
            variant="link"
            disabled={detailLoading}
            onClick={() => void openDetail(row)}
            className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
          >
            查看
          </UIButton>
        </div>
      ),
    },
  ];

  const renderMobileCard = (row: ConversationLogRow) => (
    <article className={MOBILE_CARD_CLASS} key={row.id}>
      <div className="flex min-w-0 items-start justify-between gap-[10px]">
        <div className="flex min-w-0 items-start gap-[8px]">
          <Checkbox
            aria-label={`选择对话日志 ${row.title || row.id}`}
            checked={selectedSessionIds.has(row.id)}
            onCheckedChange={(checked) => toggleSessionSelection(row.id, checked === true)}
          />
          <strong className="min-w-0 wrap-break-word text-[14px] font-semibold text-[#18181a]">
            {row.title || row.summary || row.last_agent_question || row.id}
          </strong>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-[4px]">
          {row.downFeedback && <StatusBadge tone="red">差评</StatusBadge>}
          {row.upFeedback && <StatusBadge tone="green">好评</StatusBadge>}
          {!row.upFeedback && !row.downFeedback && <StatusBadge tone="blue">未评价</StatusBadge>}
        </div>
      </div>
      <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">
        {row.downFeedback?.latest_message ||
          row.upFeedback?.latest_message ||
          row.summary ||
          row.last_agent_question ||
          '-'}
      </p>
      <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
        <span className="truncate" title={agentLabel(row)}>{agentLabel(row)}</span>
        <span className="shrink-0">{formatDateTime(row.updated_at)}</span>
      </div>
      <div className="mt-[8px] flex items-center gap-[8px] text-[12px] text-[#858b9c]">
        <ChannelBadge channel={row.channel} />
        <span className="truncate">{row.session_display_name || row.session_username || '-'}</span>
      </div>
      <div className="mt-[10px] flex justify-end gap-[12px]">
        <UIButton
          variant="link"
          disabled={Boolean(exportingKey)}
          onClick={() => void exportSingleSession(row)}
          className="h-auto gap-[4px] p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
        >
          {exportingKey === row.id ? (
            <LoaderCircle className="size-[12px] animate-spin" />
          ) : (
            <Download className="size-[12px]" />
          )}
          JSON
        </UIButton>
        <UIButton
          variant="link"
          disabled={detailLoading}
          onClick={() => void openDetail(row)}
          className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline disabled:text-[#c0c6d4]"
        >
          查看
        </UIButton>
      </div>
    </article>
  );

  return (
    <>
      <section
        aria-busy={loading}
        className="relative mt-[-2px] flex w-full min-w-0 max-w-full flex-col gap-[24px] overflow-hidden rounded-[18px] bg-white p-[14px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] min-[521px]:p-[18px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconCalendar className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">对话记录</span>
        </div>

        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="对话反馈统计">
          <StatCard value={rows.length} label="对话" />
          <StatCard value={summary?.total_feedback ?? 0} label="反馈" />
          <StatCard value={summary?.up_count ?? 0} label="好评" tone="green" />
          <StatCard value={summary?.down_count ?? 0} label="差评" tone="red" />
        </div>

        {summary && (summary.summary || summary.bucket_counts.length > 0) && (
          <div className="flex flex-col gap-[12px] rounded-[14px] border border-[#eef0f4] bg-[#fafbfc] px-[20px] py-[16px]">
            {summary.summary && (
              <p className="wrap-break-word text-[13px] leading-[1.7] text-[#464c5e]">
                {summary.summary}
              </p>
            )}
            {summary.bucket_counts.length > 0 && (
              <div className="flex flex-wrap gap-[6px]">
                {summary.bucket_counts.map((item) => (
                  <StatusBadge key={item.bucket} tone={bucketTone(item.bucket)}>
                    {item.label} {item.count}
                  </StatusBadge>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="flex flex-col gap-[12px] min-[1100px]:flex-row min-[1100px]:items-center min-[1100px]:justify-between">
          <div className="min-w-0 overflow-x-auto">
            <UnderlineTabs
              aria-label="对话日志筛选"
              variant="line"
              value={filter}
              onChange={setFilter}
              items={FILTER_TABS}
            />
          </div>
          <div className="flex shrink-0 items-center gap-[8px] max-[1099px]:w-full max-[520px]:flex-col">
            <label className="flex h-[34px] w-[280px] shrink-0 items-center overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white transition-colors focus-within:border-[#18181a] max-[1099px]:flex-1 max-[520px]:w-full">
              <span className="flex h-full w-[72px] shrink-0 items-center justify-center border-r-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] text-[12px] text-[#858b9c]">
                对话用户
              </span>
              <UISelect value={conversationUserId} onValueChange={setConversationUserId}>
                <SelectTrigger
                  aria-label="筛选对话用户"
                  className={cn(
                    SELECT_TRIGGER_CLASS,
                    'h-full min-w-0 flex-1 rounded-none border-0 px-[12px] shadow-none focus-visible:border-0',
                  )}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL_CONVERSATION_USERS}>
                    全部用户（{logFilterRows.length}）
                  </SelectItem>
                  {conversationUserOptions.map((option) => (
                    <SelectItem key={option.userId} value={option.userId}>
                      {option.label}（{option.count}）
                    </SelectItem>
                  ))}
                </SelectContent>
              </UISelect>
            </label>
            <UIButton
              variant="outline"
              disabled={batchRows.length === 0 || Boolean(exportingKey)}
              onClick={() => void exportBatch()}
              className="h-[34px] shrink-0 gap-[6px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] text-[12px] font-normal text-[#464c5e] hover:border-[#cbd3e6] hover:bg-[#fafbfc] disabled:text-[#c0c6d4] max-[520px]:w-full"
            >
              {exportingKey === 'batch' ? (
                <LoaderCircle className="size-[14px] animate-spin" />
              ) : (
                <Download className="size-[14px]" />
              )}
              {selectedSessionIds.size
                ? `导出已选（${batchRows.length}）`
                : `导出筛选结果（${batchRows.length}）`}
            </UIButton>
          </div>
        </div>

        <div className="grid gap-[10px] md:hidden">
          {filteredRows.length ? (
            pagination.pagedItems.map(renderMobileCard)
          ) : (
            <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无对话日志</div>
          )}
        </div>

        <div className="hidden md:block">
          <DataTable
            aria-label="对话日志"
            columns={columns}
            data={pagination.pagedItems}
            rowKey={(row) => row.id}
            loading={loading}
            emptyText="暂无对话日志"
          />
        </div>

        {filteredRows.length > 0 && (
          <Paginator
            aria-label="对话日志分页"
            className="mt-0 mb-[6px]"
            page={pagination.page}
            pageCount={pagination.pageCount}
            onChange={pagination.setPage}
          />
        )}
      </section>

      <FeedbackDetailDialog
        detail={detail}
        agentLabelFromId={agentLabelFromId}
        onClose={() => setDetail(null)}
        onReanalyze={reanalyzeFeedback}
        reanalyzingId={reanalyzingId}
      />
    </>
  );
}

function FeedbackDetailDialog({
  detail,
  agentLabelFromId,
  onClose,
  onReanalyze,
  reanalyzingId,
}: {
  detail: ConversationDetail | null;
  agentLabelFromId: (agentId?: string | null) => string;
  onClose: () => void;
  onReanalyze: (feedbackId: string) => void;
  reanalyzingId: string | null;
}) {
  return (
    <Dialog open={Boolean(detail)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[1180px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <Clock className="size-[14px] shrink-0" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            对话日志详情
          </DialogTitle>
        </div>

        {detail && (
          <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[12px]">
            <div className="grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1">
              <DetailField label="任务 ID">
                {String(detail.session.session_id || detail.session.id || '-')}
              </DetailField>
              <DetailField label="数字员工">{agentLabelFromId(String(detail.session.agent_id || ''))}</DetailField>
              <DetailField label="用户">{displayUser(detail.session)}</DetailField>
              <DetailField label="状态">{String(detail.session.status || '-')}</DetailField>
              <DetailField label="反馈" className="col-span-2 max-[520px]:col-span-1">
                <div className="flex flex-wrap gap-[6px]">
                  <StatusBadge tone="green">
                    好评 {detail.feedback.filter((item) => item.rating === 'up').length}
                  </StatusBadge>
                  <StatusBadge tone="red">
                    差评 {detail.feedback.filter((item) => item.rating === 'down').length}
                  </StatusBadge>
                  {detail.feedback
                    .filter((item) => item.rating === 'down')
                    .map((item) => item.analysis as FeedbackAnalysisRead | undefined)
                    .filter(Boolean)
                    .map((analysis, index) => (
                      <StatusBadge
                        key={`${analysis?.bucket || 'unknown'}_${index}`}
                        tone={bucketTone(analysis?.bucket)}
                      >
                        {analysis?.bucket_label || analysis?.bucket || '待分析'}
                      </StatusBadge>
                    ))}
                </div>
              </DetailField>
            </div>

            <div className="feedback-conversation">
              {conversationItems(detail).map(({ message: item, trace }) => (
                <FeedbackMessage
                  key={item.id}
                  item={item}
                  trace={trace}
                  userLabel={displayUser(detail.session)}
                  onReanalyze={onReanalyze}
                  reanalyzing={Boolean(item.feedback_id && item.feedback_id === reanalyzingId)}
                />
              ))}
              {detail.messages.length === 0 && detail.traces.length > 0
                ? detail.traces.map((trace) => (
                    <div key={trace.turn_id} className="feedback-message-row assistant">
                      <div className="feedback-message-bubble trace-only">
                        <FeedbackTraceBlock trace={trace} />
                      </div>
                    </div>
                  ))
                : null}
            </div>

            <section className="rounded-[14px] border border-[#e3e7f1] bg-[#fafbfc] p-[14px]">
              <div className="flex flex-wrap items-center justify-between gap-[8px]">
                <div>
                  <strong className="text-[13px] font-semibold text-[#18181a]">工具调用与原始日志 JSON</strong>
                  <p className="m-0 mt-[3px] text-[11px] text-[#858b9c]">
                    包含工具参数、返回结果、Trace、事件和消息，敏感字段使用服务端审计副本。
                  </p>
                </div>
                <StatusBadge tone="blue">{detail.toolInvocations.length} 次工具调用</StatusBadge>
              </div>

              {detail.toolInvocations.length > 0 && (
                <div className="mt-[12px] grid gap-[8px]">
                  {detail.toolInvocations.map((invocation) => (
                    <details key={invocation.id} className="rounded-[10px] border border-[#e6e9ef] bg-white px-[12px] py-[9px]">
                      <summary className="cursor-pointer text-[12px] font-medium text-[#464c5e]">
                        {invocation.tool_name} · {invocation.status} · {formatDateTime(invocation.started_at)}
                      </summary>
                      <pre className="mt-[10px] max-h-[360px] overflow-auto rounded-[8px] bg-[#18181a] p-[12px] text-[11px] leading-[1.55] text-[#d8e2f0]">
                        {JSON.stringify(invocation, null, 2)}
                      </pre>
                    </details>
                  ))}
                </div>
              )}

              <details className="mt-[10px] rounded-[10px] border border-[#e6e9ef] bg-white px-[12px] py-[9px]">
                <summary className="cursor-pointer text-[12px] font-medium text-[#464c5e]">查看完整会话日志 JSON</summary>
                <pre className="mt-[10px] max-h-[520px] overflow-auto rounded-[8px] bg-[#18181a] p-[12px] text-[11px] leading-[1.55] text-[#d8e2f0]">
                  {JSON.stringify({
                    session: detail.session,
                    messages: detail.messages,
                    feedback: detail.feedback,
                    traces: detail.traces,
                    events: detail.events,
                    tool_invocations: detail.toolInvocations,
                  }, null, 2)}
                </pre>
              </details>
            </section>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function FeedbackMessage({
  item,
  trace,
  userLabel,
  onReanalyze,
  reanalyzing,
}: {
  item: FeedbackMessageRead;
  trace?: TurnTraceRead;
  userLabel: string;
  onReanalyze: (feedbackId: string) => void;
  reanalyzing: boolean;
}) {
  const isUser = item.role === 'user';
  const isAssistant = item.role === 'assistant';
  const analysisFailed = item.feedback_analysis?.status === 'failed';
  return (
    <div className={`feedback-message-row ${isUser ? 'user' : 'assistant'}`}>
      <div className="feedback-message-bubble">
        <div className="feedback-message-meta">
          <span>{isUser ? userLabel : isAssistant ? '员工' : item.role}</span>
          <span>{formatDateTime(item.created_at)}</span>
          {item.feedback_rating === 'down' && <StatusBadge tone="red">差评</StatusBadge>}
          {item.feedback_rating === 'up' && <StatusBadge tone="green">好评</StatusBadge>}
          {item.feedback_analysis &&
            (analysisFailed ? (
              <StatusBadge tone="red">分析失败</StatusBadge>
            ) : (
              <StatusBadge tone={bucketTone(item.feedback_analysis.bucket)}>
                {item.feedback_analysis.bucket_label || item.feedback_analysis.bucket || '待分析'}
              </StatusBadge>
            ))}
        </div>
        {trace && <FeedbackTraceBlock trace={trace} />}
        <div className="feedback-message-content">
          <MarkdownMessage content={item.content} />
        </div>
        {item.feedback_analysis && item.feedback_rating === 'down' && (
          <div className="feedback-analysis-box">
            <div>
              <strong>状态：</strong>
              {analysisStatusLabel(item.feedback_analysis.status)}
              {item.feedback_analysis.status !== 'failed' &&
                typeof item.feedback_analysis.confidence === 'number' && (
                  <span> · 置信度 {(item.feedback_analysis.confidence * 100).toFixed(0)}%</span>
                )}
            </div>
            {item.feedback_analysis.summary && (
              <div>
                <strong>改进项：</strong>
                {item.feedback_analysis.summary}
              </div>
            )}
            {item.feedback_analysis.reason && (
              <div>
                <strong>原因：</strong>
                {item.feedback_analysis.reason}
              </div>
            )}
            {item.feedback_analysis.status === 'failed' && item.feedback_id && (
              <UIButton
                variant="outline"
                disabled={reanalyzing}
                onClick={() => onReanalyze(item.feedback_id as string)}
                className="mt-[8px] h-[30px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:text-[#18181a]"
              >
                <RefreshCw className={cn('size-3.5', reanalyzing && 'animate-spin')} />
                重新分析
              </UIButton>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function conversationItems(
  detail: ConversationDetail,
): Array<{ message: FeedbackMessageRead; trace?: TurnTraceRead }> {
  const tracesByUserMessage = new Map<string, TurnTraceRead>();
  const tracesByTurn = new Map<string, TurnTraceRead>();
  detail.traces.forEach((trace) => {
    if (trace.user_message_id) tracesByUserMessage.set(trace.user_message_id, trace);
    tracesByTurn.set(trace.turn_id, trace);
  });

  let currentUserMessageId = '';
  return detail.messages.map((messageItem) => {
    if (messageItem.role === 'user') {
      currentUserMessageId = messageItem.id;
      return { message: messageItem };
    }
    const trace =
      messageItem.role === 'assistant'
        ? tracesByUserMessage.get(currentUserMessageId) || tracesByTurn.get(currentUserMessageId)
        : undefined;
    return { message: messageItem, trace };
  });
}

function FeedbackTraceBlock({ trace }: { trace: TurnTraceRead }) {
  const lines = traceDetails(trace.lines);
  if (lines.length === 0) return null;
  return (
    <div className="feedback-trace-block">
      <div className="feedback-trace-header">
        <Workflow className="size-[14px]" />
        <span>执行记录</span>
        <span className="feedback-trace-overall-timing">
          {timingText(trace.duration_ms, trace.model_duration_ms, trace.model_call_count)}
        </span>
        <span className="feedback-trace-status">{trace.completed_at ? '已完成' : '执行中'}</span>
      </div>
      <div className="feedback-trace-lines">
        {lines.map((line) => (
          <div key={line.id} className={`feedback-trace-line ${line.kind} ${line.state}`}>
            <span className="feedback-trace-icon">{traceLineIcon(line.kind)}</span>
            <span className="feedback-trace-content">
              <span className="feedback-trace-title-row">
                <span className="feedback-trace-text">{line.text}</span>
                {(typeof line.duration_ms === 'number' || typeof line.model_duration_ms === 'number') && (
                  <span className="feedback-trace-timing">
                    {timingText(line.duration_ms, line.model_duration_ms)}
                  </span>
                )}
              </span>
              {line.detail && <span className="feedback-trace-detail">{line.detail}</span>}
              {line.code && (
                <details className="feedback-trace-code">
                  <summary>查看代码</summary>
                  <pre>{line.code}</pre>
                </details>
              )}
              {line.output && (
                <details className="feedback-trace-code">
                  <summary>{line.outputTitle || '查看输出'}</summary>
                  <pre>{line.output}</pre>
                </details>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function traceDetails(lines: TraceLineRead[]): TraceLineRead[] {
  const hiddenPlaceholders = new Set(['正在思考', '已完成思考', '正在执行', '执行记录']);
  return lines.filter((line) => {
    if (line.kind === 'thinking' && line.state !== 'failed') return false;
    if (hiddenPlaceholders.has(line.text) && !line.detail && !line.code && !line.output) return false;
    return true;
  });
}

function traceLineIcon(kind: TraceLineRead['kind']) {
  if (kind === 'skill') return <GitBranch className="size-[13px]" />;
  if (kind === 'tool') return <Wrench className="size-[13px]" />;
  if (kind === 'knowledge') return <FileSearch className="size-[13px]" />;
  return <Workflow className="size-[13px]" />;
}

function displayUser(session: Record<string, unknown>): string {
  return String(
    session.session_display_name ||
      session.display_name ||
      session.session_username ||
      session.username ||
      '未知用户',
  );
}

function timingText(
  durationMs?: number | null,
  modelDurationMs?: number | null,
  modelCallCount?: number | null,
): string {
  const parts: string[] = [];
  if (typeof durationMs === 'number') parts.push(`总 ${formatDuration(durationMs)}`);
  if (typeof modelDurationMs === 'number') parts.push(`模型 ${formatDuration(modelDurationMs)}`);
  if (typeof modelCallCount === 'number') parts.push(`${modelCallCount} 次调用`);
  return parts.join(' · ');
}

function formatDuration(durationMs: number): string {
  if (durationMs < 1) return '<1ms';
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  if (durationMs < 10_000) return `${(durationMs / 1000).toFixed(2)}s`;
  return `${(durationMs / 1000).toFixed(1)}s`;
}

function ChannelBadge({ channel }: { channel?: string | null }) {
  if (channel === 'wechat') return <StatusBadge tone="green">微信</StatusBadge>;
  if (channel) return <StatusBadge tone="blue">{channel}</StatusBadge>;
  return <StatusBadge tone="gray">网页</StatusBadge>;
}

function bucketTone(bucket?: string): BadgeTone {
  if (bucket === 'model_issue') return 'red';
  if (bucket === 'skill_issue') return 'orange';
  if (bucket === 'tool_or_system_issue') return 'blue';
  if (bucket === 'positive_or_resolved') return 'green';
  if (bucket === 'needs_model_analysis') return 'blue';
  return 'gray';
}

function analysisStatusLabel(status?: string): string {
  if (status === 'pending') return '等待分析';
  if (status === 'analyzed') return '已完成';
  if (status === 'failed') return '分析失败';
  if (status === 'needs_model') return '未配置模型';
  return status || '未知';
}

function downloadBlob(blob: Blob, filename: string): void {
  const objectUrl = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(objectUrl);
}

function safeFilenamePart(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'session';
}

function filenameTimestamp(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
    '-',
    pad(now.getHours()),
    pad(now.getMinutes()),
    pad(now.getSeconds()),
  ].join('');
}
