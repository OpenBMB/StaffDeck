import { useMemo, useState, type ReactNode } from 'react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch } from '@/components/ui';
import { cn } from '@/lib/utils';
import type { HarnessAssembly, HarnessModule, HarnessTreeBig, HarnessTreeOption, HarnessTreeSub } from '../../api/harness';
import { KIND_META, engineLabel, hookLabel, operationLabel, placementSourceLabel, slotLabel } from '../../lib/harnessLabels';
import IconChevron from '../../assets/icons/chevron-down.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import IconClear from '../../assets/icons/field-clear.svg?react';

/**
 * 功能模块树：大模块 → 小模块 → 具体实现。
 *
 * 每一行的开关改的是「保存的装配」；运行中的状态来自后端注册表。两者不一致时行上会
 * 出现「重启后生效」，页面顶部会给出重启按钮。归类（放到哪个类目下）是纯展示，改完立即生效。
 */

const ENGINE_SLOT = 'runtime.engine';
const PEP_SLOT = 'security.pep';
const ENGINE_BY_MODULE: Record<string, 'harness_v3' | 'harness_v2'> = { 'engine.harness_v3': 'harness_v3', 'engine.harness_v2': 'harness_v2' };
const PROFILE_BY_MODULE: Record<string, string> = { 'security.oss_local': 'OSS_LOCAL', 'security.business_base': 'BUSINESS_BASE' };
const ENGINE_FOLLOWERS: Record<string, 'harness_v3' | 'harness_v2'> = { 'harness_v3.core': 'harness_v3' };

export type ModuleTreeProps = {
  tree: HarnessTreeBig[];
  loading?: boolean;
  tech: boolean;
  assembly: HarnessAssembly | null;
  options: HarnessTreeOption[];
  busy?: boolean;
  onToggleModule: (moduleId: string, enabled: boolean) => void;
  onChooseEngine: (engine: 'harness_v3' | 'harness_v2') => void;
  onChooseProfile: (profile: string) => void;
  onPlaceModule: (moduleId: string, subId: string | null) => void;
};

type Desired = { enabled: boolean; kind: 'switch' | 'choice' | 'fixed' | 'follows' | 'removed'; choiceLabel?: string; follows?: 'harness_v3' | 'harness_v2' };

/** 保存的装配里，这个模块应该是什么状态、能用什么方式改。 */
export function desiredState(m: HarnessModule, assembly: HarnessAssembly | null): Desired {
  if (m.slot === ENGINE_SLOT) {
    const engine = ENGINE_BY_MODULE[m.module_id];
    return { enabled: assembly ? assembly.engine === engine : m.enabled, kind: 'choice', choiceLabel: '使用此引擎' };
  }
  if (m.slot === PEP_SLOT) {
    const profile = PROFILE_BY_MODULE[m.module_id];
    return { enabled: assembly ? assembly.security_profile === profile : m.enabled, kind: 'choice', choiceLabel: '使用此权限模式' };
  }
  const follows = ENGINE_FOLLOWERS[m.module_id];
  if (follows) return { enabled: assembly ? assembly.engine === follows : m.enabled, kind: 'follows', follows };
  // An external module whose spec was removed from the saved assembly disappears at the next restart.
  if (assembly && m.spec && m.spec !== 'builtin' && !m.spec.startsWith('entry_point:') && !assembly.extra_modules.includes(m.spec)) {
    return { enabled: false, kind: 'removed' };
  }
  if (m.switchable === false || m.kind === 'K') return { enabled: true, kind: 'fixed' };
  // Modules the platform ships disabled (metadata.supported === false) stay off unless the saved assembly says otherwise.
  if (m.metadata?.supported === false) return { enabled: false, kind: 'fixed' };
  return { enabled: assembly ? !assembly.disabled_modules.includes(m.module_id) : m.enabled, kind: 'switch' };
}

function KindBadge({ kind, className }: { kind: string; className?: string }) {
  const meta = KIND_META[kind] ?? KIND_META.A;
  return (
    <span title={meta.hint} className={cn('inline-flex h-[18px] shrink-0 items-center rounded-[6px] px-[6px] text-[11px] leading-none', meta.tone, className)}>
      {meta.label}
    </span>
  );
}

function Tag({ children, tone = 'gray', title }: { children: ReactNode; tone?: 'gray' | 'green' | 'amber' | 'blue' | 'purple'; title?: string }) {
  const tones = {
    gray: 'bg-[#f3f4f6] text-[#757f9c]',
    green: 'bg-[#eaf7ef] text-[#1f9d55]',
    amber: 'bg-[#fff4e5] text-[#c2740c]',
    blue: 'bg-[#e8f1ff] text-[#2f6fdb]',
    purple: 'bg-[#f0eef9] text-[#6a4fc7]',
  };
  return <span title={title} className={cn('inline-flex h-[18px] shrink-0 items-center rounded-[6px] px-[6px] text-[11px] leading-none', tones[tone])}>{children}</span>;
}

function CountPill({ enabled, total, desired }: { enabled: number; total: number; desired?: number }) {
  const all = enabled === total && total > 0;
  const changed = desired !== undefined && desired !== enabled;
  return (
    <span className="inline-flex shrink-0 items-center gap-[4px]">
      <span className={cn('inline-flex h-[20px] items-center rounded-full px-[8px] text-[11px] leading-none tabular-nums', all ? 'bg-[#e9f7ef] text-[#2cb360]' : total === 0 ? 'bg-[#f3f4f6] text-[#9aa0ad]' : 'bg-[#fff4e5] text-[#c2740c]')}>
        已启用 {enabled}/{total}
      </span>
      {changed && <span className="text-[11px] tabular-nums text-[#c2740c]" title="重启运行时后的数量">→ {desired}/{total}</span>}
    </span>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 gap-[8px]">
      <span className="w-[76px] shrink-0 text-[#9aa0ad]">{label}</span>
      <span className="min-w-0 flex-1 break-words leading-[20px] text-[#464c5e]">{children}</span>
    </div>
  );
}

function Chips({ items, tone = 'gray', empty = '—' }: { items: string[]; tone?: 'gray' | 'green'; empty?: string }) {
  if (!items.length) return <span className="text-[#c0c6d4]">{empty}</span>;
  return (
    <span className="flex flex-wrap gap-[6px]">
      {items.map((x) => (
        <span key={x} className={cn('rounded-[6px] px-[6px] py-[2px] text-[11px]', tone === 'green' ? 'bg-[#eaf7ef] text-[#1f9d55]' : 'bg-[#f6f6f6] text-[#464c5e]')}>{x}</span>
      ))}
    </span>
  );
}

export function PlacementSelect({ value, options, onChange, disabled, compact }: { value: string | null; options: HarnessTreeOption[]; onChange: (subId: string | null) => void; disabled?: boolean; compact?: boolean }) {
  return (
    <Select value={value ?? '__none__'} onValueChange={(v) => onChange(v === '__none__' ? null : v)} disabled={disabled}>
      <SelectTrigger className={cn('h-[26px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[12px]', compact ? 'w-[200px]' : 'w-[260px]')} aria-label="归入类目">
        <SelectValue placeholder="归入…" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="__none__">按默认位置</SelectItem>
        {options.map((o) => <SelectItem key={o.sub_id} value={o.sub_id}>{o.label}</SelectItem>)}
      </SelectContent>
    </Select>
  );
}

function ModuleRow({ m, tech, assembly, options, busy, onToggleModule, onChooseEngine, onChooseProfile, onPlaceModule }: { m: HarnessModule } & Omit<ModuleTreeProps, 'tree' | 'loading'>) {
  const [open, setOpen] = useState(false);
  const desired = desiredState(m, assembly);
  const pendingRestart = desired.enabled !== m.enabled;
  const running = m.enabled;
  const external = m.source && m.source !== 'builtin';
  const override = m.placement?.source === 'override';
  const showPlacement = Boolean(m.movable) && (m.placement?.source === 'none' || external || override || tech);
  const placementValue = override ? assembly?.placements?.[m.module_id] ?? m.placement?.sub_id ?? null : null;

  function choose() {
    if (m.slot === ENGINE_SLOT) onChooseEngine(ENGINE_BY_MODULE[m.module_id]);
    else if (m.slot === PEP_SLOT) onChooseProfile(PROFILE_BY_MODULE[m.module_id]);
  }

  return (
    <div className={cn('rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white transition-colors', !running && !pendingRestart && 'opacity-70')}>
      <div className="flex items-center gap-[10px] px-[12px] py-[9px]">
        <button type="button" onClick={() => setOpen((v) => !v)} className="flex min-w-0 flex-1 items-center gap-[10px] text-left">
          <span className={cn('size-[7px] shrink-0 rounded-full', running ? 'bg-[#2cb360]' : 'bg-[#c0c6d4]')} title={running ? '运行中' : '未启用'} />
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-[8px]">
              <span className="truncate text-[13px] text-[#18181a]">{m.name}</span>
              {desired.kind !== 'choice' && <KindBadge kind={m.kind} />}
              {desired.kind === 'choice' && <Tag tone="purple" title="同一时间只能启用一个">二选一</Tag>}
              {m.guarded && <Tag tone="green" title="调用前会检查使用者是否有权限">需授权</Tag>}
              {external && <Tag tone="blue" title={`来源：${m.source}`}>外部</Tag>}
              {override && <Tag title="管理员指定了展示位置">管理员指定</Tag>}
              {desired.kind === 'removed' && <Tag tone="amber" title="已从外部模块列表移除，重启运行时后卸载">重启后移除</Tag>}
              {desired.kind === 'follows' && pendingRestart && <Tag tone="amber" title="已保存，重启运行时后生效">重启后随引擎{desired.enabled ? '启用' : '停用'}</Tag>}
              {desired.kind !== 'follows' && desired.kind !== 'removed' && pendingRestart && <Tag tone="amber" title="已保存，重启运行时后生效">{desired.enabled ? '重启后启用' : '重启后停用'}</Tag>}
              {!running && !pendingRestart && desired.kind !== 'follows' && <Tag>已停用</Tag>}
            </span>
            <span className="mt-[2px] block truncate text-[12px] text-[#757f9c]">{m.summary || (tech ? `${m.module_id} · v${m.version}` : '')}</span>
          </span>
        </button>
        {showPlacement && (
          <PlacementSelect value={placementValue} options={options} onChange={(sub) => onPlaceModule(m.module_id, sub)} disabled={busy} compact />
        )}
        <span className="flex min-w-[128px] shrink-0 justify-end">
          {desired.kind === 'switch' && (
            <Switch checked={desired.enabled} onCheckedChange={(next) => onToggleModule(m.module_id, next)} aria-label={`${desired.enabled ? '停用' : '启用'} ${m.name}`} />
          )}
          {desired.kind === 'choice' && (
            desired.enabled ? (
              <Tag tone="blue">当前选择</Tag>
            ) : (
              <button type="button" disabled={busy} onClick={choose} className="h-[26px] shrink-0 whitespace-nowrap rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#464c5e] hover:border-[#18181a] hover:text-[#18181a] disabled:opacity-50">
                {desired.choiceLabel}
              </button>
            )
          )}
          {desired.kind === 'follows' && <Tag title={`随 ${engineLabel(desired.follows)} 引擎一起启用或停用`}>{running ? `随 ${engineLabel(desired.follows)} 启用` : `随 ${engineLabel(desired.follows)} 停用中`}</Tag>}
          {desired.kind === 'fixed' && (m.metadata?.supported === false ? <Tag title="该渠道暂不支持此功能，接入后自动可用">暂不支持</Tag> : <Tag title="平台核心组成部分，随系统一起启用">始终启用</Tag>)}
          {desired.kind === 'removed' && <Tag>待卸载</Tag>}
        </span>
        <button type="button" onClick={() => setOpen((v) => !v)} aria-label="展开详情" className="grid size-[20px] shrink-0 place-items-center text-[#c0c6d4] hover:text-[#757f9c]">
          <IconChevron className={cn('size-[14px] transition-transform', open && 'rotate-180')} />
        </button>
      </div>
      {open && (
        <div className="grid gap-[8px] border-t-[0.5px] border-[#eef1f6] px-[12px] py-[10px] text-[12px] md:grid-cols-2">
          {m.summary && <Field label="作用"><span className="text-[#18181a]">{m.summary}</span></Field>}
          <Field label="能做什么"><Chips items={m.provides.map(operationLabel)} /></Field>
          <Field label="需要什么"><Chips items={m.requires.map(operationLabel)} /></Field>
          <Field label="受权限控制"><Chips items={m.policy_actions.map(operationLabel)} tone="green" empty="无需单独授权" /></Field>
          {m.hooks.length > 0 && <Field label="介入时机"><Chips items={m.hooks.map(hookLabel)} /></Field>}
          {m.placement && <Field label="所在类目">{placementSourceLabel(m.placement.source)}{m.category ? <span className="ml-[6px] text-[#9aa0ad]">模块声明：{m.category}</span> : null}</Field>}
          {m.metadata && Object.keys(m.metadata).length > 0 && <Field label="附加信息">{Object.entries(m.metadata).map(([k, v]) => `${k}: ${String(v)}`).join(' · ')}</Field>}
          {tech && (
            <>
              <Field label="模块 ID"><code className="rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] text-[11px]">{m.module_id}</code> <span className="text-[#9aa0ad]">v{m.version} · 接口 {m.contract_version} · 来源 {m.source}</span></Field>
              <Field label="接入点"><code className="rounded-[6px] bg-[#f6f6f6] px-[6px] py-[2px] text-[11px]">{m.slot}</code> <span className="text-[#9aa0ad]">{slotLabel(m.slot)}</span></Field>
              <Field label="操作标识"><Chips items={[...m.provides, ...m.policy_actions.filter((p) => !m.provides.includes(p))]} /></Field>
              {m.hooks.length > 0 && <Field label="Hook"><Chips items={m.hooks} /></Field>}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function desiredCount(modules: HarnessModule[], assembly: HarnessAssembly | null): number {
  return modules.filter((m) => desiredState(m, assembly).enabled).length;
}

function SubModuleNode({ sub, defaultOpen, ...rest }: { sub: HarnessTreeSub; defaultOpen: boolean } & Omit<ModuleTreeProps, 'tree' | 'loading'>) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="relative pl-[18px]">
      <span className="absolute left-[7px] top-[6px] bottom-[6px] w-[1px] bg-[#e9ecf3]" aria-hidden />
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-[8px] rounded-[8px] px-[6px] py-[7px] text-left hover:bg-[#fafbfd]">
        <IconChevron className={cn('size-[12px] shrink-0 text-[#c0c6d4] transition-transform', open ? 'rotate-0' : '-rotate-90')} />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-[8px]">
            <span className="truncate text-[13px] text-[#18181a]">{sub.name}</span>
            <KindBadge kind={sub.kind} />
          </span>
          <span className="block truncate text-[11px] text-[#9aa0ad]">{sub.description}</span>
        </span>
        <CountPill enabled={sub.enabled} total={sub.total} desired={desiredCount(sub.modules, rest.assembly)} />
      </button>
      {open && (
        <div className="flex flex-col gap-[6px] py-[4px] pl-[26px] pr-[4px]">
          {sub.modules.map((m) => <ModuleRow key={m.module_id} m={m} {...rest} />)}
          {sub.modules.length === 0 && <div className="rounded-[10px] border-[0.5px] border-dashed border-[#e3e7f1] px-[12px] py-[10px] text-[12px] text-[#9aa0ad]">此类目下暂无模块</div>}
          {rest.tech && sub.legacy.length > 0 && <div className="px-[4px] pt-[2px] text-[11px] text-[#c0c6d4]">对应代码：{sub.legacy.join(' · ')}</div>}
        </div>
      )}
    </div>
  );
}

function BigModuleCard({ big, byId, expanded, onToggle, defaultSubOpen, ...rest }: { big: HarnessTreeBig; byId: Map<string, HarnessTreeBig>; expanded: boolean; onToggle: () => void; defaultSubOpen: boolean } & Omit<ModuleTreeProps, 'tree' | 'loading'>) {
  const unplaced = big.id === 'unplaced';
  const desired = desiredCount(big.subs.flatMap((s) => s.modules), rest.assembly);
  return (
    <section className={cn('overflow-hidden rounded-[14px] border-[0.5px] bg-white', unplaced ? 'border-dashed border-[#cbd3e6]' : 'border-[#e3e7f1]')}>
      <button type="button" onClick={onToggle} className="flex w-full items-center gap-[12px] px-[16px] py-[12px] text-left hover:bg-[#fafbfd]">
        <span className={cn('grid size-[28px] shrink-0 place-items-center rounded-[8px] text-[12px] font-semibold tabular-nums', unplaced ? 'bg-[#f3f4f6] text-[#757f9c]' : 'bg-[#18181a] text-white')}>{unplaced ? '?' : big.order}</span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-[8px]">
            <span className="truncate text-[14px] font-medium text-[#18181a]">{big.name}</span>
            {unplaced ? <Tag tone="amber">待归类</Tag> : big.pep ? <Tag tone="green" title="这一类模块的操作都会经过权限检查">受权限控制</Tag> : <Tag tone="blue" title="开源版和企业版各用一种，部署时二选一">按版本选用</Tag>}
          </span>
          <span className="mt-[2px] block truncate text-[12px] text-[#757f9c]">{big.description}</span>
        </span>
        <CountPill enabled={big.enabled} total={big.total} desired={desired} />
        <IconChevron className={cn('size-[14px] shrink-0 text-[#c0c6d4] transition-transform', expanded && 'rotate-180')} />
      </button>
      {expanded && (
        <div className="border-t-[0.5px] border-[#eef1f6] px-[12px] py-[8px]">
          {big.subs.map((sub) => <SubModuleNode key={sub.id} sub={sub} defaultOpen={defaultSubOpen || unplaced} {...rest} />)}
          {big.edges.length > 0 && (
            <div className="mt-[6px] flex flex-wrap items-center gap-[6px] px-[6px] pb-[4px] pt-[8px] text-[11px] text-[#9aa0ad]">
              <span>会用到：</span>
              {big.edges.map((e) => (
                <span key={e.to} className="inline-flex items-center gap-[4px] rounded-full border-[0.5px] border-[#e3e7f1] bg-white px-[8px] py-[3px] text-[#464c5e]">
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

export default function ModuleTree({ tree, loading, ...rest }: ModuleTreeProps) {
  const [q, setQ] = useState('');
  const [expandAll, setExpandAll] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const byId = useMemo(() => new Map(tree.map((b) => [b.id, b])), [tree]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return tree;
    const hit = (m: HarnessModule) => [m.module_id, m.name, m.summary ?? '', m.slot, m.source ?? '', m.category ?? '', ...m.provides, ...m.provides.map(operationLabel), ...m.policy_actions].some((s) => s.toLowerCase().includes(needle));
    return tree
      .map((big) => ({
        ...big,
        subs: big.subs
          .map((sub) => ({ ...sub, modules: sub.modules.filter(hit) }))
          .filter((sub) => sub.modules.length > 0 || sub.name.toLowerCase().includes(needle)),
      }))
      .filter((big) => big.subs.length > 0 || big.name.toLowerCase().includes(needle));
  }, [tree, q]);

  // 全部展开只是基线；每张卡片自己的开合覆盖它（否则「全部展开」后单张卡片收不起来）。
  const isOpen = (id: string) => (q.trim() ? true : expanded[id] ?? (expandAll || id === 'unplaced'));
  const toggleAll = () => { setExpandAll((v) => !v); setExpanded({}); };

  return (
    <div className="flex flex-col gap-[14px]">
      <div className="flex flex-wrap items-center gap-[10px]">
        <label className="flex h-[34px] w-[320px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
          <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索模块或能力，例如“知识”“飞书”" className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]" />
          {q && (
            <button type="button" aria-label="清除" onClick={() => setQ('')} className="grid size-[16px] place-items-center text-[#c0c6d4] hover:text-[#858b9c]">
              <IconClear className="size-[14px]" />
            </button>
          )}
        </label>
        <button type="button" onClick={toggleAll} className="h-[34px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] text-[12px] text-[#757f9c] hover:border-[#cbd3e6] hover:text-[#18181a]">
          {expandAll ? '全部收起' : '全部展开'}
        </button>
        <div className="ml-auto flex flex-wrap items-center gap-[10px] text-[11px] text-[#9aa0ad]">
          {(['A', 'C', 'T', 'K'] as const).map((k) => (
            <span key={k} className="inline-flex items-center gap-[4px]" title={KIND_META[k].hint}><KindBadge kind={k} /></span>
          ))}
        </div>
      </div>
      {loading && tree.length === 0 ? (
        <div className="py-[40px] text-center text-[13px] text-[#858b9c]">加载中…</div>
      ) : (
        <div className="flex flex-col gap-[10px]">
          {filtered.map((big) => (
            <BigModuleCard key={big.id} big={big} byId={byId} expanded={isOpen(big.id)} onToggle={() => setExpanded((s) => ({ ...s, [big.id]: !isOpen(big.id) }))} defaultSubOpen={Boolean(q.trim()) || expandAll} {...rest} />
          ))}
          {filtered.length === 0 && <div className="py-[40px] text-center text-[13px] text-[#858b9c]">没有匹配的模块</div>}
        </div>
      )}
    </div>
  );
}
