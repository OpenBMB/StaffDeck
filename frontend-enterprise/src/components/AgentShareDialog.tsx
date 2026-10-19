import { useEffect, useMemo, useState } from 'react';
import { Check, Copy, Link2, LoaderCircle, Share2 } from 'lucide-react';

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  notify,
} from '@/components/ui';
import { ModelConfigDropdown } from '@/components/ModelConfigDropdown';
import { api, TENANT_ID } from '@/api/client';
import { employeeDisplayName } from '@/employee';
import { copyTextToClipboard } from '@/lib/clipboard';
import { shareHostFromEnv, shareUrlForToken } from '@/lib/share-host';
import { formatClientMonthDayTime } from '@/lib/timezone';
import type { AgentProfileRead, ModelConfigRead } from '@/types';

type ShareTtl = '30m' | '2h' | 'forever';

const TTL_OPTIONS: { value: ShareTtl; label: string; hint: string }[] = [
  { value: '30m', label: '30 分钟', hint: '临时演示' },
  { value: '2h', label: '2 小时', hint: '一次沟通' },
  { value: 'forever', label: '永久', hint: '长期投放' },
];

type CreatedShare = {
  id: string;
  token: string;
  model_config_id: string;
  expires_at?: string | null;
};

/**
 * 数字员工分享弹窗：选模型 + 选有效期 → 生成免登录链接。
 *
 * 链接一旦生成，访客端不能再选择模型 —— 后端会把请求体里的 model_config_id
 * 强制覆盖成这里锁定的值。
 */
export default function AgentShareDialog({
  agent,
  open,
  onClose,
}: {
  agent: AgentProfileRead | null;
  open: boolean;
  onClose: () => void;
}) {
  const [models, setModels] = useState<ModelConfigRead[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelId, setModelId] = useState('');
  const [ttl, setTtl] = useState<ShareTtl>('2h');
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<CreatedShare | null>(null);
  const [copied, setCopied] = useState(false);

  const displayName = useMemo(() => (agent ? employeeDisplayName(agent) : '数字员工'), [agent]);
  // 优先用 VITE_SHARE_HOST 配置的对外域名，未配置回落当前访问 origin。
  const shareUrl = created ? shareUrlForToken(created.token, shareHostFromEnv(import.meta.env, window.location.origin)) : '';

  useEffect(() => {
    if (!open) return;
    setCreated(null);
    setCopied(false);
    setTtl('2h');
    setModelsLoading(true);
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        const enabled = rows.filter((item) => item.enabled);
        setModels(enabled);
        setModelId((current) => (
          enabled.some((item) => item.id === current)
            ? current
            : enabled.find((item) => item.is_default)?.id || enabled[0]?.id || ''
        ));
      })
      .catch((error) => {
        notify.error(error instanceof Error ? error.message : '模型列表加载失败');
      })
      .finally(() => setModelsLoading(false));
  }, [open]);

  async function createShare() {
    if (!agent || !modelId) return;
    setCreating(true);
    try {
      const row = await api.post<CreatedShare>('/api/agent-shares', {
        tenant_id: TENANT_ID,
        agent_id: agent.id,
        model_config_id: modelId,
        ttl,
      });
      setCreated(row);
      setCopied(false);
      notify.success('分享链接已生成');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '生成分享链接失败');
    } finally {
      setCreating(false);
    }
  }

  async function copyLink() {
    if (!shareUrl) return;
    try {
      await copyTextToClipboard(shareUrl);
      setCopied(true);
      notify.success('链接已复制');
    } catch {
      setCopied(false);
      notify.error('自动复制受浏览器限制，请手动选中链接复制');
    }
  }

  const selectedModel = models.find((item) => item.id === modelId);

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !creating) onClose(); }}>
      <DialogContent
        aria-describedby="agent-share-description"
        data-i18n-ignore
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[560px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[26px] py-[22px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Share2 className="size-[18px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">分享 · {displayName}</DialogTitle>
              <DialogDescription id="agent-share-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                生成的链接无需登录即可打开对话窗，访客不能更换模型。
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[18px] overflow-y-auto px-[26px] py-[22px]">
          <section className="grid gap-[10px]">
            <div className="flex items-center justify-between">
              <h3 className="text-[13px] font-semibold text-[#18181a]">使用模型</h3>
              <span className="text-[11px] text-[#8a92a3]">访客端不可更换</span>
            </div>
            <div className="flex items-center gap-[10px] rounded-[14px] border border-[#e2e6ee] bg-white px-[16px] py-[14px]">
              <ModelConfigDropdown
                models={models}
                value={modelId}
                onChange={setModelId}
                disabled={modelsLoading || creating || Boolean(created)}
                align="start"
                placeholder={modelsLoading ? '加载中…' : '选择模型'}
              />
              <span className="min-w-0 flex-1 truncate text-[11px] text-[#8a92a3]">
                {selectedModel?.model || '未选择模型'}
              </span>
            </div>
          </section>

          <section className="grid gap-[10px]">
            <h3 className="text-[13px] font-semibold text-[#18181a]">链接有效期</h3>
            <div className="grid grid-cols-3 gap-[10px]">
              {TTL_OPTIONS.map((option) => {
                const active = ttl === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    disabled={creating || Boolean(created)}
                    onClick={() => setTtl(option.value)}
                    className={active
                      ? 'rounded-[14px] border border-[#18181a] bg-[#18181a] px-[12px] py-[12px] text-left text-white transition-colors disabled:opacity-60'
                      : 'rounded-[14px] border border-[#e2e6ee] bg-white px-[12px] py-[12px] text-left text-[#464c5e] transition-colors hover:border-[#cbd3e6] disabled:opacity-60'}
                  >
                    <strong className="block text-[13px] font-semibold">{option.label}</strong>
                    <span className={active ? 'mt-[3px] block text-[10px] text-white/70' : 'mt-[3px] block text-[10px] text-[#8a92a3]'}>
                      {option.hint}
                    </span>
                  </button>
                );
              })}
            </div>
          </section>

          {!created ? (
            <Button
              type="button"
              disabled={creating || !modelId || !agent}
              onClick={() => void createShare()}
              className="h-[42px] w-full rounded-[12px] bg-[#18181a] text-[13px] text-white hover:bg-[#303033]"
            >
              {creating && <LoaderCircle className="size-[15px] animate-spin" />}
              生成分享链接
            </Button>
          ) : (
            <section className="rounded-[16px] border border-[#f0d28e] bg-[#fff9e9] p-[18px]" aria-live="polite">
              <div className="flex items-start justify-between gap-[14px]">
                <div>
                  <strong className="text-[13px] text-[#6b4d12]">链接已生成，复制后发给对方即可</strong>
                  <p className="mt-[4px] text-[11px] text-[#96732f]">
                    {created.expires_at ? `${formatDate(created.expires_at)} 失效` : '永久有效'}
                    {selectedModel ? ` · ${selectedModel.name || selectedModel.model}` : ''}
                  </p>
                </div>
                <span className="rounded-full bg-[#f7e8bc] px-[8px] py-[3px] text-[10px] text-[#7b5c19]">免登录</span>
              </div>
              <div className="mt-[12px] flex items-center gap-[8px] rounded-[12px] bg-[#1d2027] p-[8px] pl-[12px]">
                <Link2 className="size-[14px] shrink-0 text-[#8f98ad]" />
                <input
                  readOnly
                  value={shareUrl}
                  onFocus={(event) => event.currentTarget.select()}
                  className="min-w-0 flex-1 bg-transparent font-mono text-[12px] text-[#e7ebf3] outline-none"
                />
                <Button
                  type="button"
                  onClick={() => void copyLink()}
                  className="h-[30px] shrink-0 rounded-[8px] bg-white px-[10px] text-[11px] text-[#18181a] hover:bg-[#edf0f5]"
                >
                  {copied ? <Check className="size-[13px]" /> : <Copy className="size-[13px]" />}
                  {copied ? '已复制' : '复制'}
                </Button>
              </div>
            </section>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function formatDate(value: string): string {
  // 后端 created/expires 是 naive UTC，直接 new Date 会按本地时间解析（东八区早 8 小时）
  return formatClientMonthDayTime(value, value);
}
