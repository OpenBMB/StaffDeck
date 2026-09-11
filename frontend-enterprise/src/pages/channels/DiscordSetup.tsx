import { useState } from 'react';
import { notify } from '@/components/ui/app-toast';

import { Input } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { api, TENANT_ID } from '../../api/client';
import type { ChannelBindingRead } from '../../types';
import { StatusBadge } from '../scheduled-tasks/StatusBadge';

const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const OUTLINE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[#e3e7f1] px-5 text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] hover:text-[#18181a]';

export default function DiscordSetup({
  binding,
  onChanged,
}: {
  binding: ChannelBindingRead;
  onChanged: (updated: ChannelBindingRead) => void;
}) {
  const configuredBotId = binding.bot_id || String(binding.config_json?.bot_id || '');
  const [editing, setEditing] = useState(!configuredBotId);
  const [botToken, setBotToken] = useState('');
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!botToken.trim()) {
      notify.error('请填写完整凭证');
      return;
    }
    setSaving(true);
    try {
      const updated = await api.post<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/discord/credentials`,
        { tenant_id: TENANT_ID, bot_token: botToken.trim() },
      );
      setBotToken('');
      setEditing(false);
      onChanged(updated);
      notify.success('已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存凭证失败');
    } finally {
      setSaving(false);
    }
  }

  if (configuredBotId && !editing) {
    return (
      <div className="flex flex-wrap items-center gap-[10px] rounded-[10px] bg-[#fafbfc] p-[16px]">
        <span className="text-[12px] text-[#464c5e]">凭证已配置</span>
        <span className="text-[12px] text-[#858b9c]">Bot ID：{configuredBotId}</span>
        <StatusBadge tone={binding.connected ? 'green' : 'gray'}>
          {binding.connected ? '已连接' : '未连接'}
        </StatusBadge>
        <UIButton
          variant="outline"
          onClick={() => { setBotToken(''); setEditing(true); }}
          className={OUTLINE_BUTTON_CLASS}
        >
          轮换 Token
        </UIButton>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-[12px] rounded-[10px] bg-[#fafbfc] p-[16px]">
      <span className="text-[12px] leading-[1.6] text-[#858b9c]">
        凭证获取路径：Discord Developer Portal → Applications → Bot → Token。需开启 Message Content Intent，否则群聊中未提及机器人的消息将无法读取。
      </span>
      <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
        Bot Token
        <Input type="password" value={botToken} autoComplete="off" onChange={(e) => setBotToken(e.target.value)} className="h-8 rounded-[10px] text-[12px]" />
      </label>
      <div className="flex justify-end gap-[8px]">
        {configuredBotId && <UIButton variant="outline" onClick={() => setEditing(false)} className={OUTLINE_BUTTON_CLASS}>取消</UIButton>}
        <UIButton onClick={() => void save()} disabled={saving} className={PRIMARY_BUTTON_CLASS}>保存</UIButton>
      </div>
    </div>
  );
}
