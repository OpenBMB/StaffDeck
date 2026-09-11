import { useEffect, useRef, useState } from 'react';
import { notify } from '@/components/ui/app-toast';

import { Input, RadioGroup, RadioGroupItem, Switch, Textarea } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import IconActionToggle from '../../assets/icons/action-toggle.svg?react';
import { api, TENANT_ID } from '../../api/client';
import type {
  ChannelAllowlistConfig,
  ChannelBatchJobRead,
  ChannelBindingRead,
  ChannelFeatureFlags,
  ChannelMetaRead,
} from '../../types';

const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const OUTLINE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[#e3e7f1] px-5 text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] hover:text-[#18181a]';

const DISCORD_FEATURE_FLAGS: Array<{
  key: keyof ChannelFeatureFlags;
  label: string;
  defaultOn: boolean;
}> = [
  { key: 'slash_commands', label: '原生斜杠命令', defaultOn: true },
  { key: 'threads', label: '线程', defaultOn: true },
  // 自动创建线程默认关闭,保存存量行为(未开启时不在新会话自动建线程)
  { key: 'auto_thread', label: '自动创建线程', defaultOn: false },
  { key: 'batch_send', label: '批量投递', defaultOn: true },
  { key: 'backfill', label: '历史回填', defaultOn: true },
  { key: 'typing', label: '输入状态提示', defaultOn: true },
  // voice 需要服务端安装 ffmpeg，默认关闭
  { key: 'voice', label: '语音（需 ffmpeg）', defaultOn: false },
  { key: 'rich_media', label: '富媒体（嵌入与附件）', defaultOn: true },
];

function defaultFeatureFlags(): ChannelFeatureFlags {
  return Object.fromEntries(
    DISCORD_FEATURE_FLAGS.map((flag) => [flag.key, flag.defaultOn]),
  ) as ChannelFeatureFlags;
}

// meta.capabilities 为空（旧后端未声明）时回退到默认全集，保证 UI 完整可用
function visibleFeatureKeys(meta: ChannelMetaRead | undefined): Set<string> {
  const capabilities = meta?.capabilities || [];
  if (capabilities.length === 0) {
    return new Set(DISCORD_FEATURE_FLAGS.map((flag) => flag.key));
  }
  return new Set(capabilities);
}

function parseIdLines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

function IdTextarea({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  hint?: string;
}) {
  return (
    <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
      {label}
      <Textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={2}
        aria-label={label}
        className="h-auto min-h-[48px] rounded-[10px] text-[12px]"
      />
      {hint && <span className="text-[11px] text-[#a0a6b8]">{hint}</span>}
    </label>
  );
}

export default function DiscordFeatureConfig({
  binding,
  meta,
  onChanged,
}: {
  binding: ChannelBindingRead;
  meta?: ChannelMetaRead;
  onChanged: (updated: ChannelBindingRead) => void;
}) {
  const [features, setFeatures] = useState<ChannelFeatureFlags>(() => {
    const saved = binding.config_json?.features || {};
    const defaults = defaultFeatureFlags();
    return Object.fromEntries(
      DISCORD_FEATURE_FLAGS.map((flag) => [flag.key, saved[flag.key] ?? defaults[flag.key]]),
    ) as ChannelFeatureFlags;
  });
  const [allowMode, setAllowMode] = useState<'allow_all' | 'deny_all'>(
    binding.config_json?.allowlist?.mode || 'allow_all',
  );
  const [guildIds, setGuildIds] = useState(
    (binding.config_json?.allowlist?.guild_ids || []).join('\n'),
  );
  const [channelIds, setChannelIds] = useState(
    (binding.config_json?.allowlist?.channel_ids || []).join('\n'),
  );
  const [userIds, setUserIds] = useState(
    (binding.config_json?.allowlist?.user_ids || []).join('\n'),
  );
  const [denyIds, setDenyIds] = useState((binding.config_json?.allowlist?.deny || []).join('\n'));
  const [saving, setSaving] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [targetChannelId, setTargetChannelId] = useState('');
  const [batchText, setBatchText] = useState('');
  const [batchSending, setBatchSending] = useState(false);
  const [batchJob, setBatchJob] = useState<ChannelBatchJobRead | null>(null);
  const [batchPolling, setBatchPolling] = useState(false);
  const mountedRef = useRef(true);

  // 保存成功（onChanged 更新 binding）后回显服务端配置
  useEffect(() => {
    const saved = binding.config_json?.features || {};
    const defaults = defaultFeatureFlags();
    setFeatures(
      Object.fromEntries(
        DISCORD_FEATURE_FLAGS.map((flag) => [flag.key, saved[flag.key] ?? defaults[flag.key]]),
      ) as ChannelFeatureFlags,
    );
    const allowlist = binding.config_json?.allowlist || {};
    setAllowMode(allowlist.mode || 'allow_all');
    setGuildIds((allowlist.guild_ids || []).join('\n'));
    setChannelIds((allowlist.channel_ids || []).join('\n'));
    setUserIds((allowlist.user_ids || []).join('\n'));
    setDenyIds((allowlist.deny || []).join('\n'));
  }, [binding]);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  function buildAllowlist(): ChannelAllowlistConfig {
    return {
      mode: allowMode,
      guild_ids: parseIdLines(guildIds),
      channel_ids: parseIdLines(channelIds),
      user_ids: parseIdLines(userIds),
      deny: parseIdLines(denyIds),
    };
  }

  function buildFeatures(): ChannelFeatureFlags {
    // batch_send 为前端保留开关：后端 ChannelFeaturesConfig 暂不含该键，
    // 保存时剔除避免回显丢失；批量投递门禁为后续演进
    return Object.fromEntries(
      DISCORD_FEATURE_FLAGS.filter((flag) => flag.key !== 'batch_send').map((flag) => [
        flag.key,
        Boolean(features[flag.key]),
      ]),
    ) as ChannelFeatureFlags;
  }

  async function save() {
    if (saving) return;
    setSaving(true);
    try {
      // 后端契约：PUT /{binding_id} 请求体支持 features/allowlist 平级字段
      //（设计文档 §3.1 + §4.5 D5-4），allowlist/features 变更会 config_revision+=1
      const updated = await api.put<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}?tenant_id=${TENANT_ID}`,
        {
          tenant_id: TENANT_ID,
          features: buildFeatures(),
          allowlist: buildAllowlist(),
        },
      );
      onChanged(updated);
      notify.success('已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存功能配置失败');
    } finally {
      setSaving(false);
    }
  }

  async function triggerBackfill() {
    if (backfilling) return;
    setBackfilling(true);
    try {
      // 后端契约：POST /{binding_id}/backfill（设计文档 §4.4 D4-3）；
      // 目标频道为空时服务端回退 config_json.backfill.channel_id
      const body: Record<string, unknown> = { tenant_id: TENANT_ID };
      const target = targetChannelId.trim();
      if (target) body.channel_id = target;
      await api.post(`/api/enterprise/channels/${binding.id}/backfill`, body);
      notify.success('回填已触发');
    } catch (error) {
      console.error('discord backfill trigger failed:', error);
      notify.error(error instanceof Error ? error.message : '服务暂不可用');
    } finally {
      setBackfilling(false);
    }
  }

  async function sendBatch() {
    const items = parseIdLines(batchText);
    if (items.length === 0) {
      notify.error('请先输入至少一条消息');
      return;
    }
    if (batchSending) return;
    setBatchSending(true);
    setBatchJob(null);
    try {
      // 后端契约：POST /{binding_id}/batch?channel_id=...（设计文档 §4.3 D3-4），
      // 响应 {job_id, status}；目标频道为空时服务端回退 config_json.batch.channel_id
      const target = targetChannelId.trim();
      const query = target ? `?channel_id=${encodeURIComponent(target)}` : '';
      const job = await api.post<ChannelBatchJobRead>(
        `/api/enterprise/channels/${binding.id}/batch${query}`,
        { tenant_id: TENANT_ID, items },
      );
      setBatchJob(job);
      void pollBatchJob(job.job_id);
    } catch (error) {
      console.error('discord batch send failed:', error);
      notify.error(error instanceof Error ? error.message : '服务暂不可用');
    } finally {
      setBatchSending(false);
    }
  }

  async function pollBatchJob(jobId: string | undefined) {
    if (!jobId) return;
    setBatchPolling(true);
    try {
      // 后端契约：GET /{binding_id}/batch/{job_id} 响应 {job_id,status,progress,total,...}
      const job = await api.get<ChannelBatchJobRead>(
        `/api/enterprise/channels/${binding.id}/batch/${jobId}?tenant_id=${TENANT_ID}`,
      );
      if (!mountedRef.current) return;
      setBatchJob(job);
      const status = job.status || '';
      if (status === 'pending' || status === 'running') {
        window.setTimeout(() => void pollBatchJob(jobId), 2000);
      } else if (status === 'done') {
        notify.success('批处理完成');
      } else if (status === 'failed') {
        notify.error('批处理失败');
      }
    } catch (error) {
      // 作业不存在或查询失败：停止轮询，降级提示
      console.error('discord batch poll failed:', error);
    } finally {
      if (mountedRef.current) setBatchPolling(false);
    }
  }

  const visibleKeys = visibleFeatureKeys(meta);
  const batchStatusLabel = batchJob?.total
    ? `批处理进度：${batchJob.progress ?? 0} / ${batchJob.total}`
    : `状态：${batchJob?.status || '排队中'}`;

  return (
    <section aria-label="功能配置">
      <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
        <IconActionToggle className="size-[14px] shrink-0" />
        <span className="text-[14px] font-normal leading-none">功能配置</span>
      </div>
      <div className="flex flex-col gap-[16px] rounded-[14px] border border-[#eef0f4] p-[16px]">
        <div className="flex flex-col gap-[10px]">
          <span className="text-[13px] font-semibold text-[#18181a]">功能开关</span>
          <span className="text-[12px] leading-[1.6] text-[#858b9c]">
            开启或关闭该渠道的 Discord 扩展能力；语音能力需要服务器安装 ffmpeg。
          </span>
          <div className="grid gap-[10px]">
            {DISCORD_FEATURE_FLAGS.filter((flag) => visibleKeys.has(flag.key)).map((flag) => (
              <div key={flag.key} className="flex items-center justify-between gap-[12px]">
                <span className="text-[13px] text-[#18181a]">{flag.label}</span>
                <Switch
                  checked={Boolean(features[flag.key])}
                  onCheckedChange={(next) =>
                    setFeatures((current) => ({ ...current, [flag.key]: next }))
                  }
                />
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col gap-[10px] border-t border-[#eef0f4] pt-[16px]">
          <span className="text-[13px] font-semibold text-[#18181a]">权限白名单</span>
          <RadioGroup
            value={allowMode}
            onValueChange={(value) => setAllowMode(value as 'allow_all' | 'deny_all')}
            className="flex flex-wrap gap-[16px]"
          >
            <label className="flex items-center gap-[6px] text-[13px] text-[#18181a]">
              <RadioGroupItem value="allow_all" />
              放行所有消息
            </label>
            <label className="flex items-center gap-[6px] text-[13px] text-[#18181a]">
              <RadioGroupItem value="deny_all" />
              仅允许列表内的消息
            </label>
          </RadioGroup>
          <IdTextarea label="允许的服务器 ID" value={guildIds} onChange={setGuildIds} />
          <IdTextarea label="允许的频道 ID" value={channelIds} onChange={setChannelIds} />
          <IdTextarea label="允许的用户 ID" value={userIds} onChange={setUserIds} />
          <IdTextarea
            label="拒绝列表（每行一个 ID）"
            value={denyIds}
            onChange={setDenyIds}
            hint="拒绝列表中的条目优先于允许列表。"
          />
          <span className="text-[11px] text-[#a0a6b8]">每行一个 ID，留空表示不限制</span>
        </div>

        <div className="flex flex-col gap-[10px] border-t border-[#eef0f4] pt-[16px]">
          <span className="text-[13px] font-semibold text-[#18181a]">批量发送与回填</span>
          <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
            目标频道 ID（可选）
            <Input
              value={targetChannelId}
              onChange={(event) => setTargetChannelId(event.target.value)}
              aria-label="目标频道 ID（可选）"
              placeholder="目标频道 ID（可选）"
              className="h-8 rounded-[10px] text-[12px]"
            />
            <span className="text-[11px] text-[#a0a6b8]">留空时使用已保存的批处理/回填目标频道。</span>
          </label>
          <Textarea
            value={batchText}
            onChange={(event) => setBatchText(event.target.value)}
            rows={4}
            aria-label="批量发送消息（每行一条）"
            placeholder="批量发送消息（每行一条）"
            className="rounded-[10px] text-[12px]"
          />
          <div className="flex flex-wrap items-center justify-between gap-[12px]">
            <span className="min-w-0 truncate text-[12px] text-[#858b9c]">
              {batchJob ? batchStatusLabel : '每行一条消息，逐条投递到目标频道。'}
            </span>
            <div className="flex gap-[8px]">
              <UIButton
                variant="outline"
                onClick={() => void triggerBackfill()}
                disabled={backfilling}
                className={OUTLINE_BUTTON_CLASS}
              >
                手动回填
              </UIButton>
              <UIButton
                onClick={() => void sendBatch()}
                disabled={batchSending || batchPolling}
                className={PRIMARY_BUTTON_CLASS}
              >
                发送
              </UIButton>
            </div>
          </div>
        </div>

        <div className="flex justify-end gap-[8px] border-t border-[#eef0f4] pt-[16px]">
          <UIButton onClick={() => void save()} disabled={saving} className={PRIMARY_BUTTON_CLASS}>
            保存
          </UIButton>
        </div>
      </div>
    </section>
  );
}
