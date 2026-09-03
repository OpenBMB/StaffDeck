import { useState } from 'react';
import { Button as UIButton } from '@/components/ui';
import { cn } from '@/lib/utils';
import type { DshAssemblyState, DshInspectResult, DshTreeOption } from '../../api/dsh';
import { KIND_META, operationLabel, placementSourceLabel, slotLabel } from '../../lib/dshLabels';
import { PlacementSelect } from './ModuleTree';
import IconSuccess from '../../assets/icons/success-fill.svg?react';
import IconWarning from '../../assets/icons/warning-fill.svg?react';
import IconError from '../../assets/icons/error-fill.svg?react';

/**
 * 接入外部模块：填写注册入口 → 预检（在一个临时注册表里试装，看会装进什么、有没有冲突）→
 * 添加 → 归类 → 重启后加载。预检结果里就能选类目，因为归类只认模块 id，不用等模块真的装上。
 */

export type ExternalModulesPanelProps = {
  assembly: DshAssemblyState | null;
  options: DshTreeOption[];
  tech: boolean;
  busy?: boolean;
  onInspect: (spec: string) => Promise<DshInspectResult | null>;
  onAdd: (spec: string) => Promise<boolean>;
  onRemove: (spec: string) => Promise<boolean>;
  onPlace: (moduleId: string, subId: string | null) => Promise<void>;
};

function InspectCard({ result, options, placements, tech, busy, onPlace }: { result: DshInspectResult; options: DshTreeOption[]; placements: Record<string, string>; tech: boolean; busy?: boolean; onPlace: (id: string, sub: string | null) => Promise<void> }) {
  const optionLabel = (subId: string | undefined) => options.find((o) => o.sub_id === subId)?.label ?? subId ?? '';
  return (
    <div className={cn('rounded-[10px] border-[0.5px] p-[12px]', result.ok ? 'border-[#cfe9d8] bg-[#f4fbf6]' : 'border-[#f3c4c4] bg-[#fff6f6]')}>
      <div className="mb-[6px] flex flex-wrap items-center gap-[8px] text-[13px] text-[#18181a]">
        {result.ok ? <IconSuccess className="size-[14px] text-[#2cb360]" /> : <IconError className="size-[14px] text-[#d20b0b]" />}
        <code className="font-mono text-[12px]">{result.spec}</code>
        <span className="text-[12px] text-[#757f9c]">{result.ok ? `预检通过，会安装 ${result.modules.length} 个模块` : '预检未通过'}</span>
        {tech && result.file && <span className="truncate font-mono text-[11px] text-[#9aa0ad]">{result.file}</span>}
        <span className="ml-auto text-[11px] text-[#9aa0ad]">{result.elapsed_ms} ms</span>
      </div>
      {result.errors.map((e, i) => (
        <div key={i} className="mb-[4px] flex items-start gap-[6px] text-[12px] text-[#b00c0c]"><IconError className="mt-[2px] size-[12px] shrink-0" /><span><span className="font-mono">{e.code}</span>{e.phase ? `（${e.phase === 'import' ? '导入' : e.phase === 'register' ? '注册' : e.phase === 'seal' ? '装配校验' : e.phase}）` : ''}：{e.message}</span></div>
      ))}
      {result.warnings.map((w, i) => (
        <div key={i} className="mb-[4px] flex items-start gap-[6px] text-[12px] text-[#8a5a0b]"><IconWarning className="mt-[2px] size-[12px] shrink-0" /><span>{w.module_id ? <span className="font-mono">{w.module_id} </span> : null}{w.message}</span></div>
      ))}
      {result.modules.length > 0 && (
        <div className="mt-[8px] flex flex-col gap-[6px]">
          {result.modules.map((m) => {
            const override = placements[m.module_id];
            const meta = KIND_META[m.kind] ?? KIND_META.A;
            return (
              <div key={m.module_id} className="flex flex-wrap items-center gap-[10px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] py-[8px]">
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-[8px] text-[13px] text-[#18181a]">
                    {m.name}
                    <span className={cn('rounded-[6px] px-[6px] py-[2px] text-[11px]', meta.tone)}>{meta.label}</span>
                    {m.already_installed && <span className="rounded-[6px] bg-[#f3f4f6] px-[6px] py-[2px] text-[11px] text-[#757f9c]">已在运行</span>}
                  </span>
                  <span className="block text-[12px] text-[#757f9c]">{m.summary || '（模块未提供说明）'}</span>
                  <span className="mt-[2px] block text-[11px] text-[#9aa0ad]">
                    {tech ? `${m.module_id} · v${m.version} · ${slotLabel(m.slot)} · ` : ''}
                    能力：{m.provides.length ? m.provides.map(operationLabel).join('、') : '—'}
                    {' · 归类：'}{override ? `${optionLabel(override)}（管理员指定）` : m.placement && m.placement.source !== 'none' ? `${optionLabel(m.placement.sub_id)}（${placementSourceLabel(m.placement.source)}）` : '未归类'}
                  </span>
                </span>
                {m.movable !== false && <PlacementSelect value={override ?? null} options={options} onChange={(sub) => void onPlace(m.module_id, sub)} disabled={busy} compact />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function ExternalModulesPanel({ assembly, options, tech, busy, onInspect, onAdd, onRemove, onPlace }: ExternalModulesPanelProps) {
  const [spec, setSpec] = useState('');
  const [inspecting, setInspecting] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, DshInspectResult>>({});
  const [confirmSpec, setConfirmSpec] = useState<string | null>(null);

  const saved = assembly?.saved.extra_modules ?? [];
  const placements = assembly?.saved.placements ?? {};

  async function inspect(s: string) {
    setInspecting(s);
    try {
      const r = await onInspect(s);
      if (r) setResults((prev) => ({ ...prev, [s]: r }));
      return r;
    } finally {
      setInspecting(null);
    }
  }

  async function add(force = false) {
    const s = spec.trim();
    if (!s) return;
    if (saved.includes(s)) { setSpec(''); return; }
    const r = results[s] ?? (await inspect(s));
    if (r && !r.ok && !force) { setConfirmSpec(s); return; }
    setConfirmSpec(null);
    const ok = await onAdd(s);
    if (ok) setSpec('');
  }

  return (
    <section className="rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white p-[16px]">
      <div className="mb-[4px] text-[13px] text-[#18181a]">接入外部模块</div>
      <div className="mb-[10px] text-[12px] text-[#757f9c]">
        把插件包安装到后端环境（<code className="rounded-[4px] bg-[#f6f6f6] px-[4px]">pip install</code> 或加入 PYTHONPATH）后，填写它的注册入口（形如 <code className="rounded-[4px] bg-[#f6f6f6] px-[4px]">my_plugin.staffdeck:register</code>，省略冒号后部分即默认 <code className="rounded-[4px] bg-[#f6f6f6] px-[4px]">register</code>）。
        「预检」会在不影响运行的前提下试装一次，列出会安装的模块并检查冲突；预检结果里就可以选择它归到哪个类目。添加后重启运行时才会真正加载，加载失败会自动恢复原有装配。
      </div>
      <div className="flex flex-wrap items-center gap-[8px]">
        <input value={spec} onChange={(e) => { setSpec(e.target.value); setConfirmSpec(null); }} onKeyDown={(e) => { if (e.key === 'Enter') void add(); }} placeholder="package.module:register" className="h-[34px] w-[360px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] font-mono text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4] focus:border-[#18181a] max-[900px]:w-full" />
        <UIButton variant="outline" onClick={() => void inspect(spec.trim())} disabled={busy || !spec.trim() || inspecting !== null} className="h-[34px] rounded-[10px] border-[0.5px] border-[#e3e7f1] px-[14px] text-[12px] font-normal">{inspecting === spec.trim() ? '预检中…' : '预检'}</UIButton>
        <UIButton onClick={() => void add()} disabled={busy || !spec.trim() || inspecting !== null} className="h-[34px] rounded-[10px] bg-[#18181a] px-[14px] text-[12px] text-white hover:bg-[#333] disabled:opacity-50">添加</UIButton>
        {confirmSpec === spec.trim() && (
          <span className="inline-flex items-center gap-[8px] text-[12px] text-[#b00c0c]">
            预检未通过，重启时会加载失败并自动回滚。
            <button type="button" onClick={() => void add(true)} className="underline">仍然添加</button>
          </span>
        )}
      </div>
      {spec.trim() && results[spec.trim()] && !saved.includes(spec.trim()) && (
        <div className="mt-[10px]"><InspectCard result={results[spec.trim()]} options={options} placements={placements} tech={tech} busy={busy} onPlace={onPlace} /></div>
      )}
      {saved.length > 0 && (
        <div className="mt-[12px] flex flex-col gap-[8px]">
          {saved.map((s) => {
            const live = assembly?.applied?.extra_modules.includes(s);
            const r = results[s];
            return (
              <div key={s} className="flex flex-col gap-[8px]">
                <div className="flex flex-wrap items-center gap-[10px] rounded-[10px] bg-[#fafbfd] px-[12px] py-[8px] text-[12px]">
                  <code className="min-w-0 flex-1 truncate font-mono text-[#18181a]">{s}</code>
                  <span className={cn('rounded-[6px] px-[6px] py-[2px] text-[11px]', live ? 'bg-[#eaf7ef] text-[#1f9d55]' : 'bg-[#fff4e5] text-[#c2740c]')}>{live ? '已加载' : '重启后加载'}</span>
                  <button type="button" onClick={() => void inspect(s)} disabled={busy || inspecting !== null} className="text-[12px] text-[#2f6fdb] hover:underline disabled:opacity-50">{inspecting === s ? '预检中…' : '预检'}</button>
                  <button type="button" onClick={() => void onRemove(s)} disabled={busy} className="text-[12px] text-[#757f9c] hover:text-[#d20b0b]">移除</button>
                </div>
                {r && <InspectCard result={r} options={options} placements={placements} tech={tech} busy={busy} onPlace={onPlace} />}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
