import { useState } from 'react';
import { notify } from '@/components/ui/app-toast';

import { Input } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';

import { api, TENANT_ID } from '../../api/client';
import { InfoCircleOutlined } from '../../icons';
import type { ChannelBindingRead } from '../../types';
import { StatusBadge } from '../scheduled-tasks/StatusBadge';

const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const OUTLINE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[#e3e7f1] px-5 text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] hover:text-[#18181a]';

const REQUIRED_PERMISSIONS = [
  '获取通讯录基本信息（contact:contact.base:readonly）',
  '获取用户基本信息（contact:user.base:readonly）',
  '通过手机号或邮箱获取用户 ID（contact:user.id:readonly）',
  '获取群组中其他机器人和用户@当前机器人的消息（im:message.group_at_msg.include_bot:readonly）',
  '获取群组中用户@机器人消息（im:message.group_at_msg:readonly）',
  '获取群组中所有消息（敏感权限）（im:message.group_msg）',
  '获取群组中用户和机器人发送的消息（im:message.group_msg.include_bot:read）',
  '读取用户发给机器人的单聊消息（im:message.p2p_msg:readonly）',
  '查看消息表情回复（im:message.reactions:read）',
  '发送、删除消息表情回复（im:message.reactions:write_only）',
  '获取单聊、群组消息（im:message:readonly）',
  '以应用的身份发消息（im:message:send_as_bot）',
];

const REMOVABLE_PERMISSIONS = [
  '任务-创建、更新任务或清单时可指定的人员范围 数据权限范围',
  '邮箱-用户邮箱管理 数据权限范围',
  '邮箱-邮件数据 数据权限范围',
  '飞书人事（企业版）-员工 数据权限范围',
  '飞书人事（企业版）-待入职人员 数据权限范围',
  '妙记-妙记基本信息 数据权限范围',
];

function FeishuPermissionHint() {
  return (
    <div className="rounded-[10px] border border-[#e8edf5] bg-white p-[12px] text-[12px] text-[#464c5e]">
      <div className="flex items-start gap-[8px]">
        <div className="mt-[1px] flex h-4 w-4 items-center justify-center text-[#8b93a7]">
          <TooltipProvider delayDuration={120}>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-label="查看飞书权限说明"
                  className="flex h-4 w-4 items-center justify-center rounded-full text-[#8b93a7] transition-colors hover:text-[#18181a]"
                >
                  <InfoCircleOutlined className="h-4 w-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="top" align="start" className="max-w-[340px]">
                这个飞书接入需要通讯录基础信息、机器人消息收发和 reaction 能力；其余权限一般可以不加，尤其是任务、邮箱、人事和妙记相关权限。
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
        <div className="min-w-0 flex-1 leading-[1.6] text-[#596174]">
          <span className="text-[#18181a]">建议仅保留最小权限集。</span> 下面这些是首版需要的；其余大多可删。
        </div>
      </div>
      <div className="mt-[10px] grid gap-[8px] md:grid-cols-2">
        <div>
          <div className="mb-[4px] text-[11px] font-medium text-[#8b93a7]">必需权限</div>
          <div className="flex flex-wrap gap-[6px]">
            {REQUIRED_PERMISSIONS.map((item) => (
              <span key={item} className="rounded-full bg-[#eef4ff] px-[8px] py-[2px] text-[#3f5fb8]">
                {item}
              </span>
            ))}
          </div>
        </div>
        <div>
          <div className="mb-[4px] text-[11px] font-medium text-[#8b93a7]">通常可删除</div>
          <div className="flex flex-wrap gap-[6px]">
            {REMOVABLE_PERMISSIONS.map((item) => (
              <span key={item} className="rounded-full bg-[#f4f6fa] px-[8px] py-[2px] text-[#667085]">
                {item}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function FeishuSetup({
  binding,
  onChanged,
}: {
  binding: ChannelBindingRead;
  onChanged: (updated: ChannelBindingRead) => void;
}) {
  const configuredAppId = binding.app_id || '';
  const [editing, setEditing] = useState(!configuredAppId);
  const [appId, setAppId] = useState(configuredAppId);
  const [appSecret, setAppSecret] = useState('');
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!appId.trim() || !appSecret.trim()) {
      notify.error('请填写完整凭证');
      return;
    }
    setSaving(true);
    try {
      const updated = await api.post<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/feishu/credentials`,
        {
          tenant_id: TENANT_ID,
          app_id: appId.trim(),
          app_secret: appSecret.trim(),
        },
      );
      setAppSecret('');
      setEditing(false);
      onChanged(updated);
      notify.success('已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存凭证失败');
    } finally {
      setSaving(false);
    }
  }

  if (configuredAppId && !editing) {
    return (
      <div className="flex flex-col gap-[10px] rounded-[10px] bg-[#fafbfc] p-[16px]">
        <div className="flex flex-wrap items-center gap-[10px]">
          <span className="text-[12px] text-[#464c5e]">凭证已配置</span>
          <span className="text-[12px] text-[#858b9c]">App ID：{configuredAppId}</span>
          {binding.bot_name && (
            <span className="text-[12px] text-[#858b9c]">机器人：{binding.bot_name}</span>
          )}
          {binding.provider_tenant_key && (
            <span className="text-[12px] text-[#858b9c]">
              Tenant：{binding.provider_tenant_key}
            </span>
          )}
          <StatusBadge tone={binding.connected ? 'green' : 'gray'}>
            {binding.connected ? '已连接' : '未连接'}
          </StatusBadge>
          <UIButton
            variant="outline"
            onClick={() => {
              setAppId(configuredAppId);
              setAppSecret('');
              setEditing(true);
            }}
            className={OUTLINE_BUTTON_CLASS}
          >
            轮换 Secret
          </UIButton>
        </div>
        <div className="rounded-[8px] bg-[#f4f6fa] px-[12px] py-[8px] text-[11px] leading-[1.6] text-[#667085]">
          对话过程将逐步推送实时执行步骤卡片（SOP 匹配 / 步骤 / 工具 / 知识检索），结束后定格。
          可通过环境变量 <code className="rounded bg-[#e8edf5] px-[3px] py-[1px] text-[#3f5fb8]">channel_feishu_trace_enabled</code> 关闭。
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-[12px] rounded-[10px] bg-[#fafbfc] p-[16px]">
      <FeishuPermissionHint />
      <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
        App ID
        <Input
          type="text"
          value={appId}
          disabled={Boolean(configuredAppId)}
          autoComplete="off"
          data-1p-ignore="true"
          data-lpignore="true"
          onChange={(event) => setAppId(event.target.value)}
          className="h-8 rounded-[10px] text-[12px]"
        />
      </label>
      <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
        App Secret
        <Input
          type="password"
          value={appSecret}
          autoComplete="off"
          name="feishu-app-secret-no-password-manager"
          data-1p-ignore="true"
          data-lpignore="true"
          onChange={(event) => setAppSecret(event.target.value)}
          className="h-8 rounded-[10px] text-[12px]"
        />
      </label>
      <div className="flex justify-end gap-[8px]">
        {configuredAppId && (
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
