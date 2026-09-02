import { useMemo, useState } from 'react';
import { cn } from '@/lib/utils';
import type { DshModule, DshTreeBig, DshTreeSub } from '../../api/dsh';
import IconChevron from '../../assets/icons/chevron-down.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import IconClear from '../../assets/icons/field-clear.svg?react';

/** 大模块 → 子模块 → 插件 的树，对应参考架构图的八个业务模块。 */

const KIND_META: Record<string, { label: string; tone: string; hint: string }> = {
  A: { label: 'A', tone: 'bg-[#e8f1ff] text-[#2f6fdb]', hint: '代码插件：实现稳定 SPI，可独立开发、测试、发版' },
  C: { label: 'C', tone: 'bg-[#fff4e5] text-[#c2740c]', hint: '内容包：Staff / SOP / Skill 等声明式内容，下一 Turn 生效' },
  T: { label: 'T', tone: 'bg-[#eaf7ef] text-[#1f9d55]', hint: '可信服务：可独立部署，但平台语义唯一，不可由租户替换' },
  K: { label: 'K', tone: 'bg-[#f0eef9] text-[#6a4fc7]', hint: '平台内核：统一契约、编排、幂等与安全边界，不可插拔' },
};

function KindBadge({ kind, className }: { kind: string; className?: string }) {
  const meta = KIND_META[kind] ?? KIND_META.A;
  return (
    <span
      title={meta.hint}
      className={cn('inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[6px] px-[5px] text-[11px] font-semibold leading-none', meta.tone, className)}
    >
      {meta.label}
    </span>
  );
}

function CountPill({ enabled, total }: { enabled: number; total: number }) {
  const all = enabled === total && total > 0;
  return (
    <span
      className={cn(
        'inline-flex h-[20px] items-center rounded-full px-[8px] text-[11px] leading-none tabular-nums',
        all ? 'bg-[#e9f7ef] text-[#2cb360]' : total === 0 ? 'bg-[#f3f4f6] text-[#9aa0ad]' : 'bg-[#fff4e5] text-[#c2740c]',
      )}
    >
      {enabled}/{total}
    </span>
  );
}

function ModuleRow({ m }: { m: DshModule }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={cn('rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white transition-colors', !m.enabled && 'opacity-60')}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-[10px] px-[12px] py-[9px] text-left hover:bg-[#fafbfd]"
      >
        <span className={cn('size-[7px] shrink-0 rounded-full', m.enabled ? 'bg-[#2cb360]' : 'bg-[#c0c6d4]')} />
        <KindBadge kind={m.kind} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] text-[#18181a]">{m.name}</span>
          <span className="block truncate font-mono text-[11px] text-[#9aa0ad]">{m.module_id} · v{m.version}</span>
        </span>
        {m.guarded && <span className="shrink-0 rounded-[6px] bg-[#eaf7ef] px-[6px] py-[2px] text-[11px] leading-none text-[#1f9d55]">PEP</span>}
        {!m.enabled && <span className="shrink-0 rounded-[6px] bg-[#f3f4f6] px-[6px] py-[2px] text-[11px] leading-none text-[#757f9c]">已禁用</span>}
        <IconChevron className={cn('size-[14px] shrink-0 text-[#c0c6d4] transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="grid gap-[8px] border-t-[0.5px] border-[#eef1f6] px-[12px] py-[10px] text-[12px] md:grid-cols-2">
          <Field label="插槽"><code className="rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] text-[11px]">{m.slot}</code></Field>
          <Field label="契约">{m.contract_version} · 来源 {m.source}</Field>
          <Field label="提供能力">{m.provides.length ? m.provides.map((p) => <code key={p} className="mr-[6px] rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] text-[11px]">{p}</code>) : <span className="text-[#c0c6d4]">—</span>}</Field>
          <Field label="依赖能力">{m.requires.length ? m.requires.map((p) => <code key={p} className="mr-[6px] rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] text-[11px]">{p}</code>) : <span className="text-[#c0c6d4]">—</span>}</Field>
          <Field label="策略动作">{m.policy_actions.length ? m.policy_actions.map((p) => <code key={p} className="mr-[6px] rounded-[6px] bg-[#eaf7ef] px-[6px] py-[2px] text-[11px] text-[#1f9d55]">{p}</code>) : <span className="text-[#c0c6d4]">无（不经过 PEP）</span>}</Field>
          <Field label="Hook">{m.hooks.length ? m.hooks.map((h) => <code key={h} className="mr-[6px] rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] text-[11px]">{h}</code>) : <span className="text-[#c0c6d4]">—</span>}</Field>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 gap-[8px]">
      <span className="w-[64px] shrink-0 text-[#9aa0ad]">{label}</span>
      <span className="min-w-0 flex-1 break-words leading-[20px] text-[#464c5e]">{children}</span>
    </div>
  );
}

function SubModuleNode({ sub, defaultOpen }: { sub: DshTreeSub; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="relative pl-[18px]">
      <span className="absolute left-[7px] top-[6px] bottom-[6px] w-[1px] bg-[#e9ecf3]" aria-hidden />
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-[8px] rounded-[8px] px-[6px] py-[7px] text-left hover:bg-[#fafbfd]">
        <IconChevron className={cn('size-[12px] shrink-0 text-[#c0c6d4] transition-transform', open ? 'rotate-0' : '-rotate-90')} />
        <KindBadge kind={sub.kind} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] text-[#18181a]">{sub.name}</span>
          <span className="block truncate text-[11px] text-[#9aa0ad]">{sub.description}</span>
        </span>
        <CountPill enabled={sub.enabled} total={sub.total} />
      </button>
      {open && (
        <div className="flex flex-col gap-[6px] py-[4px] pl-[26px] pr-[4px]">
          {sub.modules.map((m) => <ModuleRow key={m.module_id} m={m} />)}
          {sub.modules.length === 0 && <div className="rounded-[10px] border-[0.5px] border-dashed border-[#e3e7f1] px-[12px] py-[10px] text-[12px] text-[#9aa0ad]">尚无插件注册到此子模块（插槽：{sub.slots.join('、') || '—'}）</div>}
          {sub.legacy.length > 0 && (
            <div className="px-[4px] pt-[2px] text-[11px] text-[#c0c6d4]">
              复用现有实现：{sub.legacy.join(' · ')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function BigModuleCard({ big, byId, expanded, onToggle, defaultSubOpen }: { big: DshTreeBig; byId: Map<string, DshTreeBig>; expanded: boolean; onToggle: () => void; defaultSubOpen: boolean }) {
  return (
    <section className="overflow-hidden rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white">
      <button type="button" onClick={onToggle} className="flex w-full items-center gap-[12px] px-[16px] py-[12px] text-left hover:bg-[#fafbfd]">
        <span className="grid size-[28px] shrink-0 place-items-center rounded-[8px] bg-[#18181a] text-[12px] font-semibold text-white tabular-nums">{big.order}</span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-[8px]">
            <span className="truncate text-[14px] font-medium text-[#18181a]">{big.name}</span>
            {big.pep ? (
              <span className="shrink-0 rounded-[6px] bg-[#eaf7ef] px-[6px] py-[2px] text-[11px] leading-none text-[#1f9d55]">PEP 贯穿</span>
            ) : (
              <span className="shrink-0 rounded-[6px] bg-[#f0eef9] px-[6px] py-[2px] text-[11px] leading-none text-[#6a4fc7]">部署级二选一</span>
            )}
          </span>
          <span className="mt-[2px] block truncate text-[12px] text-[#757f9c]">{big.root} · {big.description}</span>
        </span>
        <CountPill enabled={big.enabled} total={big.total} />
        <IconChevron className={cn('size-[14px] shrink-0 text-[#c0c6d4] transition-transform', expanded && 'rotate-180')} />
      </button>
      {expanded && (
        <div className="border-t-[0.5px] border-[#eef1f6] px-[12px] py-[8px]">
          {big.subs.map((sub) => <SubModuleNode key={sub.id} sub={sub} defaultOpen={defaultSubOpen} />)}
          {big.edges.length > 0 && (
            <div className="mt-[6px] flex flex-wrap items-center gap-[6px] px-[6px] pb-[4px] pt-[8px] text-[11px] text-[#9aa0ad]">
              <span>调用关系：</span>
              {big.edges.map((e) => (
                <span key={e.to} className="inline-flex items-center gap-[4px] rounded-full border-[0.5px] border-[#e3e7f1] bg-white px-[8px] py-[3px] text-[#464c5e]">
                  <span className="text-[#c0c6d4]">→</span>
                  {byId.get(e.to)?.name ?? e.to}
                  <span className="text-[#9aa0ad]">（{e.label}）</span>
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export type ModuleTreeProps = { tree: DshTreeBig[]; loading?: boolean };

export default function ModuleTree({ tree, loading }: ModuleTreeProps) {
  const [q, setQ] = useState('');
  const [expandAll, setExpandAll] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const byId = useMemo(() => new Map(tree.map((b) => [b.id, b])), [tree]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return tree;
    return tree
      .map((big) => ({
        ...big,
        subs: big.subs
          .map((sub) => ({
            ...sub,
            modules: sub.modules.filter((m) => [m.module_id, m.name, m.slot, ...m.provides, ...m.policy_actions].some((s) => s.toLowerCase().includes(needle))),
          }))
          .filter((sub) => sub.modules.length > 0 || sub.name.toLowerCase().includes(needle)),
      }))
      .filter((big) => big.subs.length > 0 || big.name.toLowerCase().includes(needle));
  }, [tree, q]);

  const isOpen = (id: string) => (q.trim() ? true : expandAll || expanded[id] === true);

  return (
    <div className="flex flex-col gap-[14px]">
      <div className="flex flex-wrap items-center gap-[10px]">
        <label className="flex h-[34px] w-[320px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
          <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索模块、插槽或能力操作" className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]" />
          {q && (
            <button type="button" aria-label="清除" onClick={() => setQ('')} className="grid size-[16px] place-items-center text-[#c0c6d4] hover:text-[#858b9c]">
              <IconClear className="size-[14px]" />
            </button>
          )}
        </label>
        <button type="button" onClick={() => setExpandAll((v) => !v)} className="h-[34px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] text-[12px] text-[#757f9c] hover:border-[#cbd3e6] hover:text-[#18181a]">
          {expandAll ? '全部收起' : '全部展开'}
        </button>
        <div className="ml-auto flex items-center gap-[10px] text-[11px] text-[#9aa0ad]">
          {(['A', 'C', 'T', 'K'] as const).map((k) => (
            <span key={k} className="inline-flex items-center gap-[4px]"><KindBadge kind={k} />{KIND_META[k].hint.split('：')[0]}</span>
          ))}
        </div>
      </div>
      {loading && tree.length === 0 ? (
        <div className="py-[40px] text-center text-[13px] text-[#858b9c]">加载中…</div>
      ) : (
        <div className="flex flex-col gap-[10px]">
          {filtered.map((big) => (
            <BigModuleCard key={big.id} big={big} byId={byId} expanded={isOpen(big.id)} onToggle={() => setExpanded((s) => ({ ...s, [big.id]: !isOpen(big.id) }))} defaultSubOpen={Boolean(q.trim()) || expandAll} />
          ))}
          {filtered.length === 0 && <div className="py-[40px] text-center text-[13px] text-[#858b9c]">没有匹配的模块</div>}
        </div>
      )}
    </div>
  );
}
