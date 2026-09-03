import { useEffect, useMemo, useRef, useState } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch } from '@/components/ui';
import { cn } from '@/lib/utils';
import type { DshLogEntry, DshSessionSummary } from '../../api/dsh';
import { engineLabel } from '../../lib/dshLabels';
import { formatLogEntry, logDate, logTime, type LogLevel } from '../../lib/dshLog';
import { parseBackendDateTime } from '../../lib/timezone';
import IconSearch from '../../assets/icons/search.svg?react';
import IconClear from '../../assets/icons/field-clear.svg?react';
import IconChevron from '../../assets/icons/chevron-down.svg?react';

/**
 * 会话执行日志：一行一件事，按时间排列，像看控制台一样从上往下读。
 * 每行 = 时间 · 类型标签 · 一句人话 · 右侧附注；点开可以看原始数据。
 */

const LEVEL_STYLE: Record<LogLevel, { bar: string; text: string }> = {
  info: { bar: 'bg-[#2f6fdb]', text: 'text-[#18181a]' },
  success: { bar: 'bg-[#2cb360]', text: 'text-[#18181a]' },
  warn: { bar: 'bg-[#e0a03b]', text: 'text-[#8a5a0b]' },
  error: { bar: 'bg-[#d20b0b]', text: 'text-[#b00c0c]' },
  muted: { bar: 'bg-[#d5d9e3]', text: 'text-[#757f9c]' },
};

const TYPE_TONE: Record<string, string> = {
  'user/message': 'bg-[#e8f1ff] text-[#2f6fdb]',
  'assistant/message': 'bg-[#e9f7ef] text-[#1f9d55]',
  'tool/call': 'bg-[#f0eef9] text-[#6a4fc7]',
  'tool/result': 'bg-[#f0eef9] text-[#6a4fc7]',
  'tool/route': 'bg-[#f3f4f6] text-[#757f9c]',
  'tool/denied': 'bg-[#fff4e5] text-[#c2740c]',
  'engine/start': 'bg-[#e9f7ef] text-[#1f9d55]',
  'engine/finish': 'bg-[#e9f7ef] text-[#1f9d55]',
  'turn/failed': 'bg-[#fce7e7] text-[#d20b0b]',
  error: 'bg-[#fce7e7] text-[#d20b0b]',
  'llm/call': 'bg-[#f3f4f6] text-[#757f9c]',
  'llm/failed': 'bg-[#fce7e7] text-[#d20b0b]',
  'handoff/created': 'bg-[#fff4e5] text-[#c2740c]',
};

const FILTERS: Array<{ key: string; label: string; match: (t: string) => boolean }> = [
  { key: 'all', label: '全部', match: () => true },
  { key: 'tool', label: '能力调用', match: (t) => t.startsWith('tool/') || t.startsWith('knowledge/') },
  { key: 'engine', label: '引擎', match: (t) => t.startsWith('engine/') || t.startsWith('turn/') || t.startsWith('task/') || t.startsWith('snapshot/') },
  { key: 'message', label: '对话', match: (t) => t.startsWith('user/') || t.startsWith('assistant/') || t.startsWith('handoff/') },
  { key: 'model', label: '模型调用', match: (t) => t.startsWith('llm/') },
  { key: 'problem', label: '仅问题', match: () => true },
];

function sessionTitle(s: DshSessionSummary): string {
  const who = s.agent_name || '未指定员工';
  const what = s.title || s.session_id;
  return `${who} · ${what}`;
}

function LogLine({ e, tech }: { e: DshLogEntry; tech: boolean }) {
  const [open, setOpen] = useState(false);
  const f = formatLogEntry(e);
  const style = LEVEL_STYLE[f.level];
  return (
    <div className={cn('group relative rounded-[8px] transition-colors hover:bg-white', open && 'bg-white')}>
      <span className={cn('absolute left-0 top-[8px] bottom-[8px] w-[3px] rounded-full', style.bar)} aria-hidden />
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-start gap-[10px] py-[6px] pl-[12px] pr-[8px] text-left">
        <span className="w-[92px] shrink-0 pt-[2px] font-mono text-[11px] tabular-nums text-[#9aa0ad]">{logTime(e.ts)}</span>
        <span className={cn('mt-[1px] inline-flex h-[18px] w-[118px] shrink-0 items-center justify-center rounded-[5px] font-mono text-[10.5px] leading-none', TYPE_TONE[e.type] ?? 'bg-[#f3f4f6] text-[#757f9c]')}>{e.type}</span>
        <span className="min-w-0 flex-1">
          <span className={cn('block text-[13px] leading-[20px]', style.text)}>{f.title}</span>
          {f.detail && <span className="block truncate text-[12px] leading-[18px] text-[#757f9c]">{f.detail}</span>}
        </span>
        {f.meta && <span className="shrink-0 pt-[2px] text-[11px] text-[#9aa0ad]">{f.meta}</span>}
        <IconChevron className={cn('mt-[4px] size-[12px] shrink-0 text-[#c0c6d4] opacity-0 transition-transform group-hover:opacity-100', open && 'rotate-180 opacity-100')} />
      </button>
      {open && (
        <div className="mx-[12px] mb-[8px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-[#fafbfd] px-[12px] py-[8px] text-[12px]">
          {f.detail && <div className="mb-[6px] whitespace-pre-wrap break-words text-[#464c5e]">{f.detail}</div>}
          <div className="flex flex-wrap gap-x-[14px] gap-y-[2px] text-[11px] text-[#9aa0ad]">
            <span>{logDate(e.ts)} {logTime(e.ts)}</span>
            {e.engine && <span>引擎：{engineLabel(e.engine)}</span>}
            {tech && e.event_type && <span>事件：{e.event_type}</span>}
            {tech && e.invocation_id && <span>调用 ID：{e.invocation_id}</span>}
            {tech && e.turn_id && <span>轮次：{e.turn_id}</span>}
          </div>
          <pre className="mt-[6px] max-h-[260px] overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-[16px] text-[#464c5e]">{JSON.stringify(e.data, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

export type SessionLogProps = {
  sessions: DshSessionSummary[];
  sessionId: string;
  onSelectSession: (id: string) => void;
  entries: DshLogEntry[];
  loading?: boolean;
  tech: boolean;
  onRefresh: () => void;
};

export default function SessionLog({ sessions, sessionId, onSelectSession, entries, loading, tech, onRefresh }: SessionLogProps) {
  const [q, setQ] = useState('');
  const [filter, setFilter] = useState('all');
  const [auto, setAuto] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!auto) return undefined;
    const t = window.setInterval(onRefresh, 4000);
    return () => window.clearInterval(t);
  }, [auto, onRefresh]);

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const pick = FILTERS.find((f) => f.key === filter) ?? FILTERS[0];
    return entries.filter((e) => {
      if (!pick.match(e.type)) return false;
      if (filter === 'problem') {
        const lvl = formatLogEntry(e).level;
        if (lvl !== 'warn' && lvl !== 'error') return false;
      }
      if (!needle) return true;
      const f = formatLogEntry(e);
      return [f.title, f.detail ?? '', e.type, JSON.stringify(e.data)].some((s) => s.toLowerCase().includes(needle));
    });
  }, [entries, q, filter]);

  useEffect(() => {
    if (auto) bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [visible.length, auto]);

  const problems = useMemo(() => entries.filter((e) => { const l = formatLogEntry(e).level; return l === 'warn' || l === 'error'; }).length, [entries]);
  const current = sessions.find((s) => s.session_id === sessionId);

  return (
    <div className="flex flex-col gap-[12px]">
      <div className="flex flex-wrap items-center gap-[10px]">
        <Select value={sessionId || undefined} onValueChange={onSelectSession}>
          <SelectTrigger className="h-[34px] w-[360px] rounded-[10px] border-[0.5px] border-[#e3e7f1] text-[12px] max-[900px]:w-full"><SelectValue placeholder="选择一个会话" /></SelectTrigger>
          <SelectContent>
            {sessions.map((s) => (
              <SelectItem key={s.session_id} value={s.session_id}>
                <span className="inline-flex items-center gap-[8px]"><span>{sessionTitle(s)}</span><span className="text-[11px] text-[#9aa0ad]">{parseBackendDateTime(s.updated_at).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span></span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <label className="flex h-[34px] w-[240px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
          <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="在日志里查找" className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]" />
          {q && <button type="button" aria-label="清除" onClick={() => setQ('')} className="grid size-[16px] place-items-center text-[#c0c6d4] hover:text-[#858b9c]"><IconClear className="size-[14px]" /></button>}
        </label>
        <div className="flex items-center gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[3px]">
          {FILTERS.map((f) => (
            <button key={f.key} type="button" onClick={() => setFilter(f.key)} className={cn('h-[26px] rounded-[8px] px-[10px] text-[12px] transition-colors', filter === f.key ? 'bg-[#18181a] text-white' : 'text-[#757f9c] hover:text-[#18181a]')}>
              {f.label}{f.key === 'problem' && problems > 0 ? ` ${problems}` : ''}
            </button>
          ))}
        </div>
        <label className="ml-auto inline-flex items-center gap-[8px] text-[12px] text-[#757f9c]">
          <Switch checked={auto} onCheckedChange={setAuto} aria-label="自动刷新" />
          自动刷新
        </label>
      </div>

      {current && (
        <div className="flex flex-wrap items-center gap-x-[16px] gap-y-[4px] px-[2px] text-[12px] text-[#757f9c]">
          <span>员工：<span className="text-[#18181a]">{current.agent_name || '未指定'}</span></span>
          {current.channel && <span>来源：<span className="text-[#18181a]">{current.channel}</span></span>}
          <span>共 {entries.length} 条记录</span>
          {tech && <span className="font-mono text-[11px]">{current.session_id}</span>}
        </div>
      )}

      <div className="max-h-[calc(100vh-380px)] min-h-[320px] overflow-auto rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-[#f7f8fa] p-[8px]">
        {!sessionId ? (
          <div className="py-[60px] text-center text-[13px] text-[#858b9c]">选择一个会话，查看它的执行过程</div>
        ) : loading && entries.length === 0 ? (
          <div className="py-[60px] text-center text-[13px] text-[#858b9c]">加载中…</div>
        ) : visible.length === 0 ? (
          <div className="py-[60px] text-center text-[13px] text-[#858b9c]">{entries.length === 0 ? '这个会话还没有执行记录' : '没有符合条件的记录'}</div>
        ) : (
          <div className="flex flex-col gap-[1px]">
            {visible.map((e) => <LogLine key={e.id} e={e} tech={tech} />)}
            <div ref={bottomRef} />
          </div>
        )}
      </div>
    </div>
  );
}
