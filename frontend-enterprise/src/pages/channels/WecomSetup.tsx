import { useState } from 'react';
import { notify } from '@/components/ui/app-toast';

import { Input } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';

import { api, TENANT_ID } from '../../api/client';
import type { ChannelBindingRead, ChannelCredentialFieldRead, ChannelMetaRead } from '../../types';
import { StatusBadge } from '../scheduled-tasks/StatusBadge';

const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const OUTLINE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[#e3e7f1] px-5 text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] hover:text-[#18181a]';

const DEFAULT_FIELDS: ChannelCredentialFieldRead[] = [
  { key: 'bot_id', label: '机器人 ID' },
  { key: 'secret', label: '机器人 Secret', secret: true },
  { key: 'corp_id', label: '企业 ID' },
];

export default function WecomSetup({
  binding,
  meta,
  onChanged,
}: {
  binding: ChannelBindingRead;
  meta?: ChannelMetaRead;
  onChanged: (updated: ChannelBindingRead) => void;
}) {
  const fields = meta?.credential_fields?.length ? meta.credential_fields : DEFAULT_FIELDS;
  // bot_id 是 ChannelBindingRead 的顶层字段(后端 DTO 不回传 config_json)
  const configuredBotId =
    (typeof binding.bot_id === 'string' && binding.bot_id) ||
    (typeof binding.config_json?.bot_id === 'string' ? binding.config_json.bot_id : '');
  const configuredCorpId =
    (typeof binding.corp_id === 'string' && binding.corp_id) ||
    (typeof binding.config_json?.corp_id === 'string' ? binding.config_json.corp_id : '');
  const [editing, setEditing] = useState(!configuredBotId);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  async function save() {
    const incomplete = fields.some(
      (field) => !field.optional && !String(values[field.key] || '').trim(),
    );
    if (incomplete) {
      notify.error('请填写完整凭证');
      return;
    }
    setSaving(true);
    try {
      const payload: Record<string, string> = { tenant_id: TENANT_ID };
      fields.forEach((field) => {
        const value = String(values[field.key] || '').trim();
        if (value) payload[field.key] = value;
      });
      const updated = await api.post<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/wecom/credentials`,
        payload,
      );
      notify.success('已保存');
      setValues({});
      setEditing(false);
      onChanged(updated);
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
        <span className="text-[12px] text-[#858b9c]">bot_id：{configuredBotId}</span>
        {configuredCorpId && (
          <span className="text-[12px] text-[#858b9c]">{`企业 ID：${configuredCorpId}`}</span>
        )}
        <StatusBadge tone={binding.connected ? 'green' : 'gray'}>
          {binding.connected ? '已连接' : '未连接'}
        </StatusBadge>
        <UIButton
          variant="outline"
          onClick={() => {
            setValues({
              bot_id: configuredBotId,
              ...(configuredCorpId ? { corp_id: configuredCorpId } : {}),
            });
            setEditing(true);
          }}
          className={OUTLINE_BUTTON_CLASS}
        >
          重新配置
        </UIButton>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-[12px] rounded-[10px] bg-[#fafbfc] p-[16px]">
      <span className="text-[12px] leading-[1.6] text-[#858b9c]">
        凭证获取路径：企业微信管理后台 → 智能机器人。
      </span>
      {fields.map((field) => (
        <label key={field.key} className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
          {field.label}
          <Input
            type={field.secret ? 'password' : 'text'}
            value={values[field.key] || ''}
            placeholder={field.placeholder || ''}
            autoComplete="off"
            onChange={(event) =>
              setValues((prev) => ({ ...prev, [field.key]: event.target.value }))
            }
            className="h-8 rounded-[10px] text-[12px]"
          />
          {field.key === 'corp_id' && (
            <span className="text-[11px] leading-[1.5] text-[#a0a6b8]">
              企业身份隔离的必要字段，激活后不可在原绑定上修改
            </span>
          )}
        </label>
      ))}
      <div className="flex justify-end gap-[8px]">
        {configuredBotId && (
          <UIButton
            variant="outline"
            onClick={() => setEditing(false)}
            className={OUTLINE_BUTTON_CLASS}
          >
            取消
          </UIButton>
        )}
        <UIButton onClick={() => void save()} disabled={saving} className={PRIMARY_BUTTON_CLASS}>
          保存
        </UIButton>
      </div>
    </div>
  );
}
