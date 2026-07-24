import { useEffect, useMemo, useState } from 'react';

import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { DetailField } from '@/components/DetailField';
import { Paginator } from '@/components/Paginator';
import { Button as UIButton } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { MOBILE_CARD_CLASS, formatDateTime } from '@/lib/enterprise-ui';

import { api, TENANT_ID } from '../../api/client';
import IconListBulleted from '../../assets/icons/list-bulleted.svg?react';
import IconHistory from '../../assets/icons/profile-history.svg?react';
import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import type { EnterpriseAuthUser } from '../../auth';
import { canManageEmployeeAgent } from '../../employee';
import { useClientPagination } from '../../hooks/useClientPagination';
import type { AgentProfileRead, MemoryRead } from '../../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
const MEMORY_PAGE_SIZE = 10;
const ALL_USERS_VALUE = '__all__';

type MemoryFilter = {
  username: string;
  user_id: string;
  q: string;
};

type MemoryUserGroup = {
  key: string;
  username?: string;
  user_id: string;
  memories: MemoryRead[];
  kinds: string[];
  latest_at: string;
  preview: string;
};

const EMPTY_FILTER: MemoryFilter = { username: '', user_id: '', q: '' };

export default function MemoriesTab({
  currentUser,
  agent,
}: {
  currentUser?: EnterpriseAuthUser;
  agent?: AgentProfileRead | null;
} = {}) {
  const [rows, setRows] = useState<MemoryRead[]>([]);
  const [detail, setDetail] = useState<MemoryUserGroup | null>(null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [agentId, setAgentId] = useState(
    () => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '',
  );
  const [filter, setFilter] = useState<MemoryFilter>(EMPTY_FILTER);

  async function load(next: MemoryFilter = filter) {
    setLoading(true);
    try {
      const params = new URLSearchParams({ tenant_id: TENANT_ID });
      if (agentId) params.set('agent_id', agentId);
      if (next.username.trim()) params.set('username', next.username.trim());
      if (next.user_id.trim()) params.set('user_id', next.user_id.trim());
      if (next.q.trim()) params.set('q', next.q.trim());
      params.set('limit', '500');
      const result = await api.get<MemoryRead[]>(`/api/enterprise/memories?${params.toString()}`);
      setRows(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '查询失败');
    } finally {
      setLoading(false);
    }
  }

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
    void load(filter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  const groups = useMemo(() => groupMemories(rows), [rows]);
  const pagination = useClientPagination(groups, MEMORY_PAGE_SIZE, groups);
  const canFilterUsers = agent ? canManageEmployeeAgent(agent, currentUser) : false;
  const userOptions = useMemo(() => {
    const map = new Map<string, string>();
    rows.forEach((row) => {
      if (row.user_id && !map.has(row.user_id)) {
        map.set(row.user_id, row.username || row.user_id);
      }
    });
    return Array.from(map.entries()).map(([user_id, label]) => ({ user_id, label }));
  }, [rows]);
  const emptyText = agentId
    ? '当前员工暂无用户记忆；新的对话记忆会按员工和用户隔离沉淀。'
    : '暂无记忆';

  function resetFilter() {
    setFilter(EMPTY_FILTER);
    void load(EMPTY_FILTER);
  }

  async function clearOwnMemories() {
    const scopeText = agentId ? '当前员工下你的长期记忆' : '当前租户下你的长期记忆';
    if (!window.confirm(`将清空${scopeText}，不会影响其他用户。确定继续？`)) {
      return;
    }
    setClearing(true);
    try {
      const params = new URLSearchParams({ tenant_id: TENANT_ID });
      if (agentId) params.set('agent_id', agentId);
      const result = await api.delete<{ deleted: number }>(`/api/enterprise/memories/me?${params.toString()}`);
      notify.success(result.deleted > 0 ? `已清空 ${result.deleted} 条记忆` : '没有可清空的记忆');
      setDetail(null);
      await load(filter);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '清空失败');
    } finally {
      setClearing(false);
    }
  }

  const columns: DataTableColumn<MemoryUserGroup>[] = [
    {
      key: 'username',
      title: '用户名',
      width: 200,
      className: 'align-top whitespace-normal text-[#18181a]',
      render: (row) => (
        <span className="block max-w-full break-all leading-[1.55]" title={row.username || undefined}>
          {row.username || '-'}
        </span>
      ),
    },
    {
      key: 'user_id',
      title: '用户ID',
      width: 180,
      className: 'align-top whitespace-normal',
      render: (row) => (
        <span className="block max-w-full break-all leading-[1.55]" title={row.user_id}>
          {row.user_id}
        </span>
      ),
    },
    {
      key: 'kinds',
      title: '类型',
      width: 120,
      render: (row) => (
        <div className="flex flex-wrap gap-[4px]">
          {row.kinds.map((kind) => (
            <MemoryKindBadge key={kind} kind={kind} />
          ))}
        </div>
      ),
    },
    {
      key: 'count',
      title: '记忆数',
      width: 100,
      render: (row) => `${row.memories.length} 次`,
    },
    {
      key: 'latest',
      title: '最近更新',
      width: 170,
      render: (row) => formatDateTime(row.latest_at),
    },
    {
      key: 'preview',
      title: '摘要',
      className: 'whitespace-normal',
      render: (row) => <span className="wrap-break-word">{row.preview || '-'}</span>,
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      render: (row) => (
        <UIButton
          variant="link"
          onClick={() => setDetail(row)}
          className="h-auto p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline"
        >
          查看
        </UIButton>
      ),
    },
  ];

  const renderMobileCard = (row: MemoryUserGroup) => (
    <article className={MOBILE_CARD_CLASS} key={row.key}>
      <div className="flex min-w-0 items-start justify-between gap-[10px]">
        <strong className="min-w-0 truncate text-[14px] font-semibold text-[#18181a]">
          {row.username || row.user_id}
        </strong>
        <UIButton
          variant="link"
          onClick={() => setDetail(row)}
          className="h-auto shrink-0 p-0 text-[12px] font-normal text-[#1a71ff] hover:text-[#4a8dff] hover:no-underline"
        >
          查看
        </UIButton>
      </div>
      <div className="mt-[8px] flex flex-wrap gap-[4px]">
        {row.kinds.map((kind) => (
          <MemoryKindBadge key={kind} kind={kind} />
        ))}
      </div>
      <p className="mt-[8px] line-clamp-2 text-[12px] leading-[1.55] text-[#858b9c]">{row.preview || '-'}</p>
      <div className="mt-[10px] flex items-center justify-between text-[12px] text-[#858b9c]">
        <span>{row.memories.length} 条记忆</span>
        <span>{formatDateTime(row.latest_at)}</span>
      </div>
    </article>
  );

  return (
    <>
      <section
        aria-busy={loading}
        className="relative mt-[-2px] flex w-full min-w-0 max-w-full flex-col gap-[24px] overflow-hidden rounded-[18px] bg-white p-[14px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] min-[521px]:p-[18px]"
      >
        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconHistory className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">记忆查询</span>
          </div>

          <form
            className="flex flex-wrap items-center gap-[16px]"
            onSubmit={(event) => {
              event.preventDefault();
              void load(filter);
            }}
          >
            {canFilterUsers && (
              <label className="flex h-[34px] w-[260px] items-center overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
                <span className="flex h-full w-[58px] shrink-0 items-center justify-center border-r-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] text-[12px] text-[#858b9c]">
                  用户
                </span>
                <UISelect
                  value={filter.user_id || ALL_USERS_VALUE}
                  onValueChange={(value) => {
                    const next = {
                      ...filter,
                      user_id: value === ALL_USERS_VALUE ? '' : value,
                    };
                    setFilter(next);
                    void load(next);
                  }}
                >
                  <SelectTrigger className="h-full min-w-0 flex-1 rounded-none border-0 px-[12px] text-[12px] shadow-none focus:ring-0">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL_USERS_VALUE}>全部用户</SelectItem>
                    {userOptions.map((option) => (
                      <SelectItem key={option.user_id} value={option.user_id}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </UISelect>
              </label>
            )}
            <PrefixInput
              label="用户名"
              placeholder="如 user_demo"
              value={filter.username}
              onChange={(value) => setFilter((prev) => ({ ...prev, username: value }))}
            />
            <PrefixInput
              label="用户ID"
              placeholder="如 user_demo"
              value={filter.user_id}
              onChange={(value) => setFilter((prev) => ({ ...prev, user_id: value }))}
            />
            <PrefixInput
              label="搜索"
              placeholder="用户名、用户 ID、记忆内容"
              value={filter.q}
              onChange={(value) => setFilter((prev) => ({ ...prev, q: value }))}
            />
            <UIButton
              type="submit"
              disabled={loading}
              className="h-[34px] w-[80px] gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white hover:bg-[#303030]"
            >
              <IconSearch className="size-[14px]" />
              查询
            </UIButton>
            <UIButton
              type="button"
              variant="outline"
              onClick={resetFilter}
              disabled={loading}
              className="h-[34px] w-[80px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
            >
              <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
              重置
            </UIButton>
            <UIButton
              type="button"
              variant="outline"
              onClick={clearOwnMemories}
              disabled={loading || clearing}
              className="h-[34px] w-[112px] rounded-[10px] border-[0.5px] border-[#f0d3d3] bg-white px-[16px] text-[12px] font-normal text-[#c43d3d] hover:border-[#e1a8a8] hover:bg-[#fff7f7] hover:text-[#a92d2d]"
            >
              {clearing ? '清空中' : '清空我的记忆'}
            </UIButton>
          </form>

          <div className="grid gap-[10px] md:hidden">
            {groups.length ? (
              pagination.pagedItems.map(renderMobileCard)
            ) : (
              <div className="py-[40px] text-center text-[13px] text-[#858b9c]">{emptyText}</div>
            )}
          </div>

          <div className="hidden md:block">
            <DataTable
              aria-label="员工记忆"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.key}
              loading={loading}
              emptyText={emptyText}
            />
          </div>

          {groups.length > 0 && (
            <Paginator
              aria-label="员工记忆分页"
              className="mt-0 mb-[6px]"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </div>
      </section>

      <MemoryDetailDialog detail={detail} onClose={() => setDetail(null)} />
    </>
  );
}

function PrefixInput({
  label,
  placeholder,
  value,
  onChange,
}: {
  label: string;
  placeholder?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex h-[34px] w-[260px] items-center overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
      <span className="flex h-full w-[58px] shrink-0 items-center justify-center border-r-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] text-[12px] text-[#858b9c]">
        {label}
      </span>
      <input
        autoComplete="off"
        data-1p-ignore="true"
        data-lpignore="true"
        data-bwignore="true"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-full min-w-0 flex-1 bg-transparent px-[12px] text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
      />
    </label>
  );
}

function MemoryKindBadge({ kind }: { kind: string }) {
  const tone = MEMORY_KIND_TONE[kind] ?? 'gray';
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-[12px] py-[4px] text-[12px] leading-none capitalize whitespace-nowrap',
        MEMORY_KIND_TONE_CLASS[tone],
      )}
    >
      {kind}
    </span>
  );
}

function MemoryDetailDialog({
  detail,
  onClose,
}: {
  detail: MemoryUserGroup | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={Boolean(detail)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[720px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconListBulleted className="size-[14px] shrink-0" />
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            员工记忆详情
          </DialogTitle>
        </div>

        {detail && (
          <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[12px]">
            <div className="grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1">
              <DetailField label="用户名">{detail.username || '-'}</DetailField>
              <DetailField label="用户ID">{detail.user_id}</DetailField>
              <DetailField label="记忆数">{detail.memories.length} 条</DetailField>
              <DetailField label="类型">
                <div className="flex flex-wrap gap-[4px]">
                  {detail.kinds.map((kind) => (
                    <MemoryKindBadge key={kind} kind={kind} />
                  ))}
                </div>
              </DetailField>
            </div>

            <div className="flex flex-col gap-[12px]">
              {detail.memories.map((item) => (
                <article
                  key={item.id}
                  className="rounded-[12px] border border-[#eef0f4] bg-white p-[14px]"
                >
                  <div className="flex items-center justify-between gap-[10px]">
                    <MemoryKindBadge kind={item.kind} />
                    <span className="text-[12px] text-[#858b9c]">{formatDateTime(item.updated_at)}</span>
                  </div>
                  <div className="mt-[10px] flex flex-wrap gap-x-[16px] gap-y-[4px] text-[12px] text-[#858b9c]">
                    <span>importance: {item.importance}</span>
                    <span>session: {item.session_id || '-'}</span>
                  </div>
                  <p className="mt-[8px] text-[13px] leading-[1.6] text-[#18181a] wrap-break-word">
                    {item.content}
                  </p>
                  {Object.keys(item.metadata || {}).length > 0 && (
                    <details className="mt-[10px] text-[12px] text-[#858b9c]">
                      <summary className="cursor-pointer select-none">metadata</summary>
                      <pre className="mt-[6px] overflow-x-auto rounded-[8px] bg-[#f6f6f6] p-[10px] text-[11px] leading-normal text-[#464c5e]">
                        {JSON.stringify(item.metadata, null, 2)}
                      </pre>
                    </details>
                  )}
                </article>
              ))}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

type MemoryTone = 'blue' | 'green' | 'gray';

const MEMORY_KIND_TONE: Record<string, MemoryTone> = {
  profile: 'blue',
  summary: 'green',
};

const MEMORY_KIND_TONE_CLASS: Record<MemoryTone, string> = {
  blue: 'bg-[#e8f0ff] text-[#1a71ff]',
  green: 'bg-[#e9f7ef] text-[#2cb360]',
  gray: 'bg-[#f2f3f7] text-[#858b9c]',
};

function groupMemories(rows: MemoryRead[]): MemoryUserGroup[] {
  const map = new Map<string, MemoryRead[]>();
  rows.forEach((row) => {
    const key = row.username || row.user_id;
    const existing = map.get(key) || [];
    existing.push(row);
    map.set(key, existing);
  });
  return Array.from(map.entries())
    .map(([key, memories]) => {
      const sorted = [...memories].sort(
        (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      );
      const kinds = Array.from(new Set(sorted.map((item) => item.kind))).sort();
      return {
        key,
        username: sorted[0]?.username,
        user_id: sorted[0]?.user_id || key,
        memories: sorted,
        kinds,
        latest_at: sorted[0]?.updated_at,
        preview: sorted
          .map((item) => item.content.replace(/\s+/g, ' ').trim())
          .filter(Boolean)
          .join(' / '),
      };
    })
    .sort((a, b) => new Date(b.latest_at).getTime() - new Date(a.latest_at).getTime());
}
