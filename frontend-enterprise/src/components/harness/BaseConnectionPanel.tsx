import { useEffect, useState } from 'react';
import { Button as UIButton } from '@/components/ui';
import { cn } from '@/lib/utils';
import type { HarnessBaseConnection, HarnessBaseConnectionUpdate, HarnessBaseTestResult } from '../../api/harness';
import { formatClientDateTime } from '../../lib/timezone';
import IconSuccess from '../../assets/icons/success-fill.svg?react';
import IconWarning from '../../assets/icons/warning-fill.svg?react';
import IconError from '../../assets/icons/error-fill.svg?react';
import IconChevron from '../../assets/icons/chevron-down.svg?react';

/**
 * 企业权限中心（Base）连接设置：地址、令牌、可选的身份中心凭证，以及「测试连接」。
 * 密钥只写不读：后端只返回是否已设置；输入框留着掩码就表示不改。
 */

const MASK = '••••••••';

type FormState = {
  authz_url: string;
  decision_token: string;
  control_token: string;
  identity_internal_url: string;
  runtime_client_id: string;
  runtime_client_secret: string;
  workload_audience: string;
  timeout_seconds: string;
  pending_timeout_seconds: string;
};

function fromBase(b: HarnessBaseConnection | null | undefined): FormState {
  return {
    authz_url: String(b?.authz_url ?? ''),
    decision_token: b?.has_decision_token ? MASK : '',
    control_token: b?.has_control_token ? MASK : '',
    identity_internal_url: String(b?.identity_internal_url ?? ''),
    runtime_client_id: String(b?.runtime_client_id ?? ''),
    runtime_client_secret: b?.has_runtime_client_secret ? MASK : '',
    workload_audience: String(b?.workload_audience ?? ''),
    timeout_seconds: b?.timeout_seconds === '' || b?.timeout_seconds === undefined || b?.timeout_seconds === null ? '' : String(b.timeout_seconds),
    pending_timeout_seconds: b?.pending_timeout_seconds === '' || b?.pending_timeout_seconds === undefined || b?.pending_timeout_seconds === null ? '' : String(b.pending_timeout_seconds),
  };
}

function toUpdate(f: FormState): HarnessBaseConnectionUpdate {
  const num = (v: string) => (v.trim() === '' ? null : Number(v));
  return {
    authz_url: f.authz_url.trim(),
    decision_token: f.decision_token,
    control_token: f.control_token,
    identity_internal_url: f.identity_internal_url.trim(),
    runtime_client_id: f.runtime_client_id.trim(),
    runtime_client_secret: f.runtime_client_secret,
    workload_audience: f.workload_audience.trim(),
    timeout_seconds: num(f.timeout_seconds),
    pending_timeout_seconds: num(f.pending_timeout_seconds),
  };
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-[4px]">
      <span className="text-[12px] text-[#757f9c]">{label}{hint && <span className="ml-[6px] text-[11px] text-[#c0c6d4]">{hint}</span>}</span>
      {children}
    </label>
  );
}

const INPUT = 'h-[34px] w-full rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4] focus:border-[#18181a]';

export type BaseConnectionPanelProps = {
  base: HarnessBaseConnection | null;
  selected: boolean;           // 企业版 is the saved choice
  busy?: boolean;
  onSave: (patch: HarnessBaseConnectionUpdate) => Promise<boolean>;
  onTest: (patch?: HarnessBaseConnectionUpdate) => Promise<HarnessBaseTestResult | null>;
};

export default function BaseConnectionPanel({ base, selected, busy, onSave, onTest }: BaseConnectionPanelProps) {
  const [open, setOpen] = useState(selected);
  const [advanced, setAdvanced] = useState(false);
  const [form, setForm] = useState<FormState>(() => fromBase(base));
  const [dirty, setDirty] = useState(false);
  const [result, setResult] = useState<HarnessBaseTestResult | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => { if (!dirty) setForm(fromBase(base)); }, [base, dirty]);
  useEffect(() => { if (selected) setOpen(true); }, [selected]);

  const set = (k: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement>) => { setForm((f) => ({ ...f, [k]: e.target.value })); setDirty(true); };

  const [formError, setFormError] = useState<string | null>(null);

  function validate(): string | null {
    for (const [label, v] of [['判定超时', form.timeout_seconds], ['等待同步超时', form.pending_timeout_seconds]] as const) {
      if (v.trim() && !Number.isFinite(Number(v))) return `${label}必须是数字`;
    }
    if (form.authz_url.trim() && !/^https?:\/\/\S+/.test(form.authz_url.trim())) return '权限中心地址必须以 http:// 或 https:// 开头';
    if (form.identity_internal_url.trim() && !/^https?:\/\/\S+/.test(form.identity_internal_url.trim())) return '身份中心地址必须以 http:// 或 https:// 开头';
    return null;
  }

  async function save() {
    const err = validate();
    setFormError(err);
    if (err) return;
    const ok = await onSave(toUpdate(form));
    if (ok) { setDirty(false); setResult(null); }
  }

  async function test() {
    const err = validate();
    setFormError(err);
    if (err) return;
    setTesting(true);
    try {
      const r = await onTest(dirty ? toUpdate(form) : undefined);
      setResult(r);
    } finally {
      setTesting(false);
    }
  }

  const configured = Boolean(base?.configured);
  const lastOk = base?.last_test_ok;
  const verdict = !configured ? { icon: <IconWarning className="size-[14px] text-[#c2740c]" />, text: '尚未配置权限中心' }
    : lastOk === true ? { icon: <IconSuccess className="size-[14px] text-[#2cb360]" />, text: `连接测试通过${base?.last_test_at ? ` · ${formatClientDateTime(base.last_test_at)}` : ''}` }
    : lastOk === false ? { icon: <IconError className="size-[14px] text-[#d20b0b]" />, text: `上次测试失败${base?.last_test_at ? ` · ${formatClientDateTime(base.last_test_at)}` : ''}` }
    : { icon: <IconWarning className="size-[14px] text-[#c2740c]" />, text: '已保存，尚未测试连接' };

  return (
    <section className={cn('rounded-[14px] border-[0.5px] bg-white', selected ? 'border-[#cbd3e6]' : 'border-[#e3e7f1]')}>
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-[10px] px-[16px] py-[12px] text-left">
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-[8px] text-[13px] text-[#18181a]">企业权限中心连接{selected && <span className="rounded-[6px] bg-[#e8f1ff] px-[6px] py-[2px] text-[11px] text-[#2f6fdb]">已选择企业版</span>}</span>
          <span className="mt-[2px] flex items-center gap-[6px] text-[12px] text-[#757f9c]">{verdict.icon}{verdict.text}</span>
        </span>
        <IconChevron className={cn('size-[14px] shrink-0 text-[#c0c6d4] transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="border-t-[0.5px] border-[#eef1f6] px-[16px] py-[14px]">
          <div className="mb-[12px] text-[12px] text-[#757f9c]">切换到「企业版 · 统一权限中心」需要权限中心（Base Authz）的地址和决策令牌；本租户的用户、员工和资源必须已同步到权限中心，否则切换后会全部拒绝。保存后请点「测试连接」，通过后才能重启到企业版。</div>
          <div className="grid gap-[12px] md:grid-cols-2">
            <Row label="权限中心地址" hint="BASE_AUTHZ_URL"><input className={INPUT} value={form.authz_url} onChange={set('authz_url')} placeholder="http://127.0.0.1:9200" /></Row>
            <Row label="决策令牌" hint="BASE_AUTHZ_DECISION_TOKEN"><input className={INPUT} type="password" autoComplete="new-password" value={form.decision_token} onChange={set('decision_token')} placeholder={base?.has_decision_token ? '已保存，留空即清除' : '必填'} /></Row>
            <Row label="控制令牌（可选）" hint="BASE_AUTHZ_CONTROL_TOKEN"><input className={INPUT} type="password" autoComplete="new-password" value={form.control_token} onChange={set('control_token')} placeholder="同步资源事实时使用" /></Row>
            <Row label="判定超时 / 等待同步超时（秒）"><div className="flex gap-[8px]"><input className={INPUT} value={form.timeout_seconds} onChange={set('timeout_seconds')} placeholder="3" /><input className={INPUT} value={form.pending_timeout_seconds} onChange={set('pending_timeout_seconds')} placeholder="3" /></div></Row>
          </div>
          <button type="button" onClick={() => setAdvanced((v) => !v)} className="mt-[10px] inline-flex items-center gap-[4px] text-[12px] text-[#757f9c] hover:text-[#18181a]">
            <IconChevron className={cn('size-[12px] transition-transform', advanced ? 'rotate-0' : '-rotate-90')} />工作负载凭证（身份中心，可选）
          </button>
          {advanced && (
            <div className="mt-[8px] grid gap-[12px] md:grid-cols-2">
              <Row label="身份中心内部地址" hint="BASE_IDENTITY_INTERNAL_URL"><input className={INPUT} value={form.identity_internal_url} onChange={set('identity_internal_url')} placeholder="http://127.0.0.1:19100" /></Row>
              <Row label="运行时客户端 ID" hint="BASE_IDENTITY_RUNTIME_CLIENT_ID"><input className={INPUT} value={form.runtime_client_id} onChange={set('runtime_client_id')} placeholder="agent-platform-runtime" /></Row>
              <Row label="运行时客户端密钥" hint="BASE_IDENTITY_RUNTIME_CLIENT_SECRET"><input className={INPUT} type="password" autoComplete="new-password" value={form.runtime_client_secret} onChange={set('runtime_client_secret')} /></Row>
              <Row label="工作负载受众" hint="BASE_WORKLOAD_IDENTITY_AUDIENCE"><input className={INPUT} value={form.workload_audience} onChange={set('workload_audience')} placeholder="staffdeck-gateway" /></Row>
            </div>
          )}
          <div className="mt-[14px] flex flex-wrap items-center gap-[8px]">
            <UIButton onClick={() => void save()} disabled={busy || !dirty} className="h-[32px] rounded-[8px] bg-[#18181a] px-[14px] text-[12px] text-white hover:bg-[#333] disabled:opacity-50">保存</UIButton>
            <UIButton variant="outline" onClick={() => void test()} disabled={busy || testing || (!form.authz_url.trim())} className="h-[32px] rounded-[8px] border-[0.5px] border-[#e3e7f1] px-[14px] text-[12px] font-normal">{testing ? '正在测试…' : dirty ? '测试当前填写的值' : '测试连接'}</UIButton>
            {dirty && <span className="text-[12px] text-[#c2740c]">有未保存的修改</span>}
            {formError && <span className="text-[12px] text-[#b00c0c]">{formError}</span>}
          </div>
          {result && (
            <div className={cn('mt-[12px] rounded-[10px] border-[0.5px] p-[12px]', result.ok ? 'border-[#cfe9d8] bg-[#f4fbf6]' : 'border-[#f5d9a8] bg-[#fffaf0]')}>
              <div className="mb-[6px] flex items-center gap-[6px] text-[13px] text-[#18181a]">
                {result.ok ? <IconSuccess className="size-[14px] text-[#2cb360]" /> : <IconError className="size-[14px] text-[#d20b0b]" />}
                {result.ok ? '连接测试通过' : '连接测试未通过'}
                {!result.saved && <span className="text-[12px] text-[#9aa0ad]">（测试的是未保存的值，保存后需再测一次）</span>}
              </div>
              <ul className="flex flex-col gap-[4px]">
                {result.checks.map((c) => (
                  <li key={c.name} className="flex items-start gap-[8px] text-[12px]">
                    <span className="mt-[2px] shrink-0">{c.ok === true ? <IconSuccess className="size-[13px] text-[#2cb360]" /> : c.ok === false ? (c.fatal ? <IconError className="size-[13px] text-[#d20b0b]" /> : <IconWarning className="size-[13px] text-[#c2740c]" />) : <span className="inline-block size-[13px] rounded-full border-[0.5px] border-[#c0c6d4]" />}</span>
                    <span className="w-[160px] shrink-0 text-[#757f9c]">{c.name}</span>
                    <span className="min-w-0 flex-1 text-[#18181a]">{c.message}</span>
                    {c.latency_ms !== null && c.latency_ms !== undefined && <span className="shrink-0 text-[11px] text-[#9aa0ad]">{c.latency_ms} ms</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
