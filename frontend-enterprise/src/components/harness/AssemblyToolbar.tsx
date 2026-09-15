import { useEffect, useState } from 'react';
import { harnessApi, type AssemblyOptions, type AssemblyPreview, type HarnessAssemblyState, type HarnessAssemblyUpdate } from '@/api/harness';
import { Button, notify } from '@/components/ui';

export default function AssemblyToolbar({ tenantId, state, busy, onSave }: {
  tenantId: string; state: HarnessAssemblyState | null; busy: boolean;
  onSave: (patch: HarnessAssemblyUpdate) => Promise<boolean>;
}) {
  const [options, setOptions] = useState<AssemblyOptions | null>(null);
  const [error, setError] = useState('');
  const [checking, setChecking] = useState(false);
  const [preview, setPreview] = useState<AssemblyPreview | null>(null);
  const [presetId, setPresetId] = useState('');
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [source, setSource] = useState<'saved' | 'applied'>('saved');
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let active = true;
    harnessApi.assemblyOptions(tenantId).then((data) => { if (active) setOptions(data); })
      .catch((err: Error) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, [tenantId, state?.restart_count]);
  useEffect(() => { setPreview(null); }, [state?.saved]);

  async function savePreset() {
    if (!name.trim() || saving) return;
    setSaving(true);
    setError('');
    try {
      const created = await harnessApi.savePreset(tenantId, { name: name.trim(), description: description.trim(), source });
      setOptions((previous) => ({ slots: previous?.slots || [], presets: [...(previous?.presets || []), created] }));
      setPresetId(created.id);
      setEditing(false);
      setName('');
      setDescription('');
      notify.success('装配预设已保存，未改变当前运行配置');
    } catch (err) { setError(err instanceof Error ? err.message : '保存预设失败'); }
    finally { setSaving(false); }
  }

  async function check() {
    setChecking(true);
    try { setPreview(await harnessApi.assemblyPreview(tenantId)); }
    catch (err) { notify.error(err instanceof Error ? err.message : '装配检查失败'); }
    finally { setChecking(false); }
  }
  const preset = options?.presets.find((item) => item.id === presetId);
  return <div className="border-b border-[#edf0f5] pb-[20px]" aria-label="模块装配工具栏">
    <div className="flex flex-wrap items-start justify-between gap-[16px]">
      <div><h3 className="text-[15px] font-semibold">模块配置</h3>
        <p className="mt-[6px] text-[12px] leading-5 text-[#757f9c]">载入预设，或在下方逐项选择实现、启用与停用模块。所有更改统一检查和应用。</p></div>
      <Button variant="outline" disabled={busy || checking || !state} onClick={() => void check()}>{checking ? '检查中…' : '检查待应用装配'}</Button>
    </div>
    {error && <p role="alert" className="mt-[12px] text-[12px] text-red-700">{error}</p>}
    <div className="mt-[18px] flex flex-wrap items-center gap-[10px] rounded-[10px] bg-[#f7f9fc] p-[12px]">
      <label className="text-[12px]" htmlFor="assembly-preset">装配预设</label>
      <select id="assembly-preset" className="h-[34px] rounded border bg-white px-[10px] text-[12px]" value={presetId} onChange={(e) => setPresetId(e.target.value)}>
        <option value="">选择一份预设</option>{(options?.presets || []).map((item) => <option key={item.id} value={item.id} data-i18n-ignore={item.custom || undefined}>{item.name}</option>)}
      </select>
      <Button variant="outline" disabled={busy || !preset} onClick={() => { if (preset) void onSave(preset.assembly); }}>载入为待应用清单</Button>
      <Button variant="outline" disabled={busy || saving || !state || state.restarting} onClick={() => setEditing(!editing)}>保存为预设</Button>
      {preset && <p data-i18n-ignore={preset.custom || undefined} className="basis-full text-[12px] text-[#757f9c]">{preset.description}</p>}
      {editing && <form className="basis-full space-y-[12px] border-t border-[#e3e7f1] pt-[14px]" onSubmit={(event) => { event.preventDefault(); void savePreset(); }}>
        <div className="grid gap-[12px] sm:grid-cols-2">
          <label className="text-[12px]">预设名称<input required maxLength={80} autoFocus value={name} disabled={saving} onChange={(event) => setName(event.target.value)} className="mt-[6px] block h-[36px] w-full rounded border bg-white px-[10px]" /></label>
          <label className="text-[12px]">保存来源<select aria-label="保存来源" value={source} disabled={saving} onChange={(event) => setSource(event.target.value as 'saved' | 'applied')} className="mt-[6px] block h-[36px] w-full rounded border bg-white px-[10px]">
            <option value="saved">当前编辑配置（待应用）</option><option value="applied" disabled={!state?.applied}>当前运行配置（已生效）</option>
          </select></label>
        </div>
        <label className="block text-[12px]">预设说明（选填）<input maxLength={500} value={description} disabled={saving} onChange={(event) => setDescription(event.target.value)} className="mt-[6px] block h-[36px] w-full rounded border bg-white px-[10px]" /></label>
        <p className="text-[11px] leading-5 text-[#757f9c]">保存模块选择、启停状态及公开参数，不复制账号凭证或业务数据。载入后仍需检查并应用。</p>
        <div className="flex justify-end gap-[10px]"><Button type="button" variant="outline" disabled={saving} onClick={() => setEditing(false)}>取消</Button><Button type="submit" disabled={saving || busy || !name.trim()}>{saving ? '保存中…' : '保存预设'}</Button></div>
      </form>}
    </div>
    {preview && <div role="status" className={`mt-[14px] rounded-[10px] border p-[14px] text-[12px] leading-6 ${preview.ok ? 'border-green-200 bg-green-50 text-green-900' : 'border-red-200 bg-red-50 text-red-900'}`}>
      <strong>{preview.ok ? '检查通过，仍需应用后生效' : '不能应用此装配'}</strong>
      {preview.error && <p>{preview.error}</p>}
      {preview.diff && <><p>启用：{preview.diff.enable.join('、') || '无'}</p><p>停用：{preview.diff.disable.join('、') || '无'}</p><p>保持不变：{preview.diff.unchanged.length} 个模块</p></>}
    </div>}
    <p className="mt-[12px] text-[11px] leading-5 text-[#757f9c]">更改先保存为待应用配置，点击「重启运行时」后生效。应用时等待任务结束，失败保留原配置；更换数据接线不会迁移历史。</p>
  </div>;
}
