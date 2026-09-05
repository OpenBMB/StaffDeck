import { useEffect, useState } from 'react';
import QRCode from 'qrcode';
import { notify } from '@/components/ui/app-toast';

import { Input } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { copyTextToClipboard } from '@/lib/clipboard';

import { api, TENANT_ID } from '../../api/client';
import type { ChannelBindingRead, ChannelCredentialFieldRead, ChannelMetaRead } from '../../types';

const PRIMARY_BUTTON_CLASS =
  'h-8 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const OUTLINE_BUTTON_CLASS =
  'h-8 rounded-[10px] border-[#e3e7f1] px-5 text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]';

const DEFAULT_FIELDS: ChannelCredentialFieldRead[] = [
  { key: 'secret', label: '微信客服应用 Secret', secret: true },
];

export default function WechatKfSetup({
  binding,
  meta,
  onChanged,
}: {
  binding: ChannelBindingRead;
  meta?: ChannelMetaRead;
  onChanged: (updated: ChannelBindingRead) => void;
}) {
  const fields = (meta?.credential_fields || DEFAULT_FIELDS).filter(
    (field) => !['corp_id', 'callback_token', 'encoding_aes_key'].includes(field.key),
  );
  const configured = Boolean(binding.wechat_kf_accounts?.length);
  const callbackReady = Boolean(binding.callback_ready);
  const credentialsSaved = callbackReady && binding.status === 'active';
  const [editing, setEditing] = useState(!configured);
  const [values, setValues] = useState<Record<string, string>>({});
  const [callbackConfig, setCallbackConfig] = useState<{
    token: string;
    aesKey: string;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [contactUrl, setContactUrl] = useState('');
  const [generating, setGenerating] = useState(false);
  const [accounts, setAccounts] = useState<Array<{
    open_kfid: string;
    name: string;
    bound?: boolean;
    bound_binding_id?: string | null;
    bound_agent_id?: string | null;
    bound_team_id?: string | null;
  }>>([]);
  const [accountName, setAccountName] = useState('');
  const [avatarMediaId, setAvatarMediaId] = useState('');
  const [avatarName, setAvatarName] = useState('');
  const [avatarUploading, setAvatarUploading] = useState(false);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [selectedAccount, setSelectedAccount] = useState<{ open_kfid: string; name: string } | null>(null);
  const [accountDetail, setAccountDetail] = useState<string | null>(null);
  const [accountLinks, setAccountLinks] = useState<Record<string, { url: string; qr: string }>>({});
  const [editingAccount, setEditingAccount] = useState<string | null>(null);
  const [editingAccountName, setEditingAccountName] = useState('');
  const callbackUrl = `${window.location.origin}/api/channels/wechat-kf/${binding.id}/callback`;

  async function prepareCallbackConfig() {
    const corpId = String(values.corp_id || binding.corp_id || '').trim();
    if (!corpId) {
      notify.error('请先填写企业 ID');
      return;
    }
    setSaving(true);
    try {
      const result = await api.post<{
        callback_token: string;
        encoding_aes_key: string;
      }>(`/api/enterprise/channels/${binding.id}/wechat_kf/callback-config`, {
        tenant_id: TENANT_ID,
        corp_id: corpId,
      });
      setValues((current) => ({ ...current, corp_id: corpId }));
      setCallbackConfig({ token: result.callback_token, aesKey: result.encoding_aes_key });
      notify.success('回调配置已生成');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '生成回调配置失败');
    } finally {
      setSaving(false);
    }
  }

  async function copyCallbackUrl() {
    await copyTextToClipboard(callbackUrl);
    notify.success('回调 URL 已复制');
  }

  async function save() {
    if (fields.some((field) => !field.optional && !String(values[field.key] || '').trim())) {
      notify.error('请填写完整凭证');
      return;
    }
    setSaving(true);
    try {
      const payload: Record<string, string> = {
        tenant_id: TENANT_ID,
        corp_id: String(values.corp_id || binding.corp_id || '').trim(),
        callback_token: callbackConfig?.token || String(values.callback_token || '').trim(),
        encoding_aes_key: callbackConfig?.aesKey || String(values.encoding_aes_key || '').trim(),
      };
      fields.forEach((field) => {
        payload[field.key] = String(values[field.key] || '').trim();
      });
      const updated = await api.post<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/credentials`,
        payload,
      );
      setValues({});
      setEditing(false);
      onChanged(updated);
      notify.success('微信客服 API 已连接到数字员工');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存微信客服凭证失败');
    } finally {
      setSaving(false);
    }
  }

  async function generateContactUrl(openKfId: string, showQr: boolean) {
    setGenerating(true);
    try {
      const result = await api.post<{ url: string }>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/contact-way?tenant_id=${TENANT_ID}&open_kfid=${encodeURIComponent(openKfId)}`,
        {},
      );
      setContactUrl(result.url);
      const qr = showQr ? await QRCode.toDataURL(result.url, { width: 220, margin: 1 }) : '';
      setAccountLinks((current) => ({ ...current, [openKfId]: { url: result.url, qr } }));
      if (showQr) {
        notify.success('客服二维码已生成');
      } else {
        notify.success('咨询链接已生成');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '生成咨询链接失败');
    } finally {
      setGenerating(false);
    }
  }

  async function copyAccountLink(url: string) {
    try {
      await copyTextToClipboard(url);
      notify.success('咨询链接已复制');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复制咨询链接失败');
    }
  }

  async function loadAccounts() {
    setAccountsLoading(true);
    try {
      const result = await api.get<{ accounts: Array<{
        open_kfid: string;
        name: string;
        bound?: boolean;
        bound_binding_id?: string | null;
        bound_agent_id?: string | null;
        bound_team_id?: string | null;
      }> }>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/accounts?tenant_id=${TENANT_ID}`,
      );
      setAccounts(result.accounts);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载客服账号失败');
    } finally {
      setAccountsLoading(false);
    }
  }

  useEffect(() => {
    if (!callbackReady || binding.status !== 'active') {
      setAccounts([]);
      return;
    }
    void loadAccounts();
  }, [binding.id, binding.status, binding.config_revision, callbackReady]);

  async function selectAccount(openKfId: string) {
    setSaving(true);
    try {
      const updated = await api.post<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/account`,
        { tenant_id: TENANT_ID, open_kfid: openKfId },
      );
      onChanged(updated);
      setSelectedAccount(
        updated.wechat_kf_accounts?.find((account) => account.open_kfid === openKfId) || {
          open_kfid: openKfId,
          name: '',
        },
      );
      await loadAccounts();
      notify.success('客服账号已绑定');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '绑定客服账号失败');
    } finally {
      setSaving(false);
    }
  }

  async function createAccount() {
    if (!accountName.trim()) {
      notify.error('请输入客服账号名称');
      return;
    }
    if (!avatarMediaId) {
      notify.error('请先上传客服头像');
      return;
    }
    setSaving(true);
    try {
      const updated = await api.post<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/accounts`,
        { tenant_id: TENANT_ID, name: accountName.trim(), media_id: avatarMediaId },
      );
      setAccountName('');
      setAvatarMediaId('');
      setAvatarName('');
      onChanged(updated);
      const created = updated.wechat_kf_accounts?.[updated.wechat_kf_accounts.length - 1];
      if (created) setSelectedAccount({ open_kfid: created.open_kfid, name: created.name });
      await loadAccounts();
      notify.success('客服账号已创建并绑定');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建客服账号失败');
    } finally {
      setSaving(false);
    }
  }

  async function updateAccount(openKfId: string) {
    if (!editingAccountName.trim()) {
      notify.error('请输入客服账号名称');
      return;
    }
    setSaving(true);
    try {
      const updated = await api.patch<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/account`,
        {
          tenant_id: TENANT_ID,
          open_kfid: openKfId,
          name: editingAccountName.trim(),
          ...(avatarMediaId ? { media_id: avatarMediaId } : {}),
        },
      );
      setEditingAccount(null);
      setEditingAccountName('');
      setAvatarMediaId('');
      setAvatarName('');
      onChanged(updated);
      await loadAccounts();
      notify.success('客服账号已更新');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '修改客服账号失败');
    } finally {
      setSaving(false);
    }
  }

  async function deleteAccount(openKfId: string) {
    if (!window.confirm('确定删除这个微信客服账号吗？删除后微信客服后台账号也会被删除。')) return;
    setSaving(true);
    try {
      const updated = await api.delete<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/account/${encodeURIComponent(openKfId)}?tenant_id=${TENANT_ID}`,
      );
      onChanged(updated);
      await loadAccounts();
      setAccountLinks((current) => {
        const next = { ...current };
        delete next[openKfId];
        return next;
      });
      notify.success('客服账号已删除');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除客服账号失败');
    } finally {
      setSaving(false);
    }
  }

  async function uploadAvatar(file: File) {
    if (!['image/jpeg', 'image/png'].includes(file.type)) {
      notify.error('客服头像仅支持 JPG 或 PNG');
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      notify.error('客服头像不能超过 2MB');
      return;
    }
    setAvatarUploading(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const result = await api.postForm<{ media_id: string }>(
        `/api/enterprise/channels/${binding.id}/wechat_kf/avatar?tenant_id=${TENANT_ID}`,
        form,
      );
      setAvatarMediaId(result.media_id);
      setAvatarName(file.name);
      notify.success('客服头像上传成功');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '上传客服头像失败');
    } finally {
      setAvatarUploading(false);
    }
  }

  return (
    <div className="flex flex-col gap-[14px] rounded-[10px] bg-[#fafbfc] p-[16px]">
      <div className="flex flex-col gap-[6px]">
        <span className="text-[12px] text-[#464c5e]">微信客服回调 URL</span>
        <div className="flex flex-wrap items-center gap-[8px]">
          <code className="min-w-0 flex-1 break-all rounded-[8px] bg-white px-[10px] py-[8px] text-[11px] text-[#464c5e]">
            {callbackUrl}
          </code>
          <UIButton variant="outline" onClick={() => void copyCallbackUrl()} className={OUTLINE_BUTTON_CLASS}>
            复制
          </UIButton>
        </div>
        <span className="text-[11px] leading-[1.6] text-[#858b9c]">
           先生成回调配置，将 URL、Token、EncodingAESKey 填入微信客服后台；后台创建 API 后再回来填写 Secret 和客服账号 ID。
        </span>
      </div>

      {!callbackReady && !callbackConfig && !configured ? (
        <div className="flex flex-col gap-[8px]">
          <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
            企业 ID
            <Input
              value={values.corp_id || ''}
              onChange={(event) => setValues((current) => ({ ...current, corp_id: event.target.value }))}
              className="h-8 rounded-[10px] text-[12px]"
            />
          </label>
          <UIButton onClick={() => void prepareCallbackConfig()} disabled={saving} className={PRIMARY_BUTTON_CLASS}>
            {saving ? '生成中' : '生成回调配置'}
          </UIButton>
        </div>
      ) : credentialsSaved && !editing ? (
        <div className="flex flex-col gap-[10px] rounded-[8px] bg-white p-[12px]">
          <div className="flex flex-wrap items-center gap-[10px]">
            <span className="text-[12px] text-[#464c5e]">Secret 已保存</span>
            <span className="text-[12px] text-[#858b9c]">
              必须先选择已有客服账号或创建新账号，绑定完成后才会接收业务回调。
            </span>
            <UIButton
              variant="outline"
              onClick={() => setEditing(true)}
              className={OUTLINE_BUTTON_CLASS}
            >
              重新配置 Secret
            </UIButton>
          </div>
          <div className="flex flex-wrap items-center gap-[8px]">
            <UIButton variant="outline" onClick={() => void loadAccounts()} disabled={accountsLoading} className={OUTLINE_BUTTON_CLASS}>
              {accountsLoading ? '加载中' : '选择已有客服账号'}
            </UIButton>
            <Input
              value={accountName}
              placeholder="新客服账号名称"
              onChange={(event) => setAccountName(event.target.value)}
              className="h-8 w-[180px] rounded-[10px] text-[12px]"
            />
            <label className="flex h-8 cursor-pointer items-center rounded-[10px] border border-[#e3e7f1] px-[12px] text-[12px] text-[#464c5e]">
              {avatarUploading ? '上传中' : avatarName || '上传客服头像'}
              <input
                type="file"
                accept="image/jpeg,image/png"
                className="hidden"
                disabled={avatarUploading}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void uploadAvatar(file);
                  event.currentTarget.value = '';
                }}
              />
            </label>
            <UIButton onClick={() => void createAccount()} disabled={saving} className={PRIMARY_BUTTON_CLASS}>
              创建并绑定
            </UIButton>
          </div>
          {(binding.wechat_kf_accounts || []).length > 0 && (
            <div className="flex flex-col gap-[6px] rounded-[8px] bg-white p-[10px]">
              <span className="text-[11px] text-[#858b9c]">已绑定客服账号</span>
              {binding.wechat_kf_accounts?.map((account) => (
                <div key={account.open_kfid} className="flex flex-col gap-2 rounded-[8px] border border-[#eef0f4] p-2 text-[12px] text-[#464c5e]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>{account.name || account.open_kfid}</span>
                    <span className="text-[#858b9c]">{account.agent_id || account.team_id || '已绑定'}</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <UIButton
                      variant="outline"
                      onClick={() => void generateContactUrl(account.open_kfid, true)}
                      className="h-7 px-2 text-[11px]"
                    >
                      扫码使用
                    </UIButton>
                    <UIButton
                      variant="outline"
                      onClick={() => void generateContactUrl(account.open_kfid, false)}
                      className="h-7 px-2 text-[11px]"
                    >
                      分享链接
                    </UIButton>
                    {accountLinks[account.open_kfid]?.qr && (
                      <img
                        src={accountLinks[account.open_kfid].qr}
                        alt="客服二维码"
                        className="size-[80px]"
                      />
                    )}
                    {accountLinks[account.open_kfid]?.url && (
                      <div className="flex min-w-0 flex-1 items-center gap-2">
                        <code className="min-w-0 flex-1 break-all text-[11px] text-[#858b9c]">
                          {accountLinks[account.open_kfid].url}
                        </code>
                        <UIButton
                          variant="outline"
                          onClick={() => void copyAccountLink(accountLinks[account.open_kfid].url)}
                          className="h-7 shrink-0 px-2 text-[11px]"
                        >
                          复制
                        </UIButton>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
          {accounts.length > 0 && (
            <div className="flex flex-col gap-[6px] rounded-[8px] bg-white p-[10px]">
              {accounts.map((account) => (
                <div key={account.open_kfid}>
                  <div className="flex items-center justify-between rounded-[6px] px-[8px] py-[6px] text-[12px] hover:bg-[#f4f5f8]">
                  <span>{account.name || account.open_kfid}</span>
                  <div className="flex items-center gap-[6px]">
                    <UIButton variant="outline" onClick={() => setAccountDetail(account.open_kfid)} className="h-7 px-2 text-[11px]">
                      配置详情
                    </UIButton>
                    <UIButton
                      variant="outline"
                      onClick={() => {
                        setEditingAccount(account.open_kfid);
                        setEditingAccountName(account.name);
                      }}
                      className="h-7 px-2 text-[11px]"
                    >
                      编辑
                    </UIButton>
                    <UIButton
                      variant="outline"
                      onClick={() => void deleteAccount(account.open_kfid)}
                      className="h-7 px-2 text-[11px] text-[#c62828]"
                    >
                      删除
                    </UIButton>
                    <UIButton onClick={() => void selectAccount(account.open_kfid)} className="h-7 px-2 text-[11px]">
                      选择
                    </UIButton>
                  </div>
                  </div>
                {accountDetail === account.open_kfid && (
                  <div className="mt-1 rounded-[8px] bg-white p-[10px] text-[12px] text-[#464c5e]">
                    <div>客服账号 ID：{account.open_kfid}</div>
                    <div>绑定渠道：{account.bound_binding_id || '未绑定'}</div>
                    <div>绑定数字员工：{account.bound_agent_id || '未绑定'}</div>
                    <div>绑定团队：{account.bound_team_id || '未绑定'}</div>
                    <UIButton
                      variant="outline"
                      onClick={() => setAccountDetail(null)}
                      className="mt-2 h-7 px-2 text-[11px]"
                    >
                      关闭
                    </UIButton>
                  </div>
                )}
                {editingAccount === account.open_kfid && (
                  <div className="flex flex-wrap items-center gap-2 rounded-[8px] bg-white p-2">
                    <Input
                      value={editingAccountName}
                      placeholder="客服账号名称"
                      onChange={(event) => setEditingAccountName(event.target.value)}
                      className="h-8 w-[180px] rounded-[10px] text-[12px]"
                    />
                    <label className="flex h-8 cursor-pointer items-center rounded-[10px] border border-[#e3e7f1] px-[12px] text-[12px] text-[#464c5e]">
                      {avatarUploading ? '上传中' : avatarName || '更换头像（可选）'}
                      <input
                        type="file"
                        accept="image/jpeg,image/png"
                        className="hidden"
                        disabled={avatarUploading}
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) void uploadAvatar(file);
                          event.currentTarget.value = '';
                        }}
                      />
                    </label>
                    <UIButton onClick={() => void updateAccount(account.open_kfid)} disabled={saving} className="h-7 px-2 text-[11px]">
                      保存
                    </UIButton>
                    <UIButton variant="outline" onClick={() => setEditingAccount(null)} className="h-7 px-2 text-[11px]">
                      取消
                    </UIButton>
                  </div>
                )}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : configured && !editing ? (
        <div className="flex flex-col gap-[10px]">
          <div className="flex flex-wrap items-center gap-[10px]">
              <span className="text-[12px] text-[#464c5e]">已绑定客服账号：{binding.wechat_kf_accounts?.length || 0} 个</span>
            <span className="text-[12px] text-[#858b9c]">企业 ID：{binding.corp_id}</span>
            <UIButton
              variant="outline"
              onClick={() => {
                setValues({ corp_id: binding.corp_id || '' });
                setEditing(true);
              }}
              className={OUTLINE_BUTTON_CLASS}
            >
              重新配置
            </UIButton>
          </div>
          {binding.wechat_kf_accounts?.map((account) => {
            const link = accountLinks[account.open_kfid];
            return <div key={account.open_kfid} className="flex flex-wrap items-center gap-2 rounded-[8px] bg-white p-2 text-[12px]">
              <span>{account.name || account.open_kfid}</span>
              <UIButton variant="outline" onClick={() => void generateContactUrl(account.open_kfid, true)} className="h-7 px-2 text-[11px]">扫码使用</UIButton>
              <UIButton variant="outline" onClick={() => void generateContactUrl(account.open_kfid, false)} className="h-7 px-2 text-[11px]">分享链接</UIButton>
              {link && <><img src={link.qr} alt="客服二维码" className="size-[80px]" /><code className="break-all">{link.url}</code></>}
            </div>;
          })}
        </div>
      ) : (
        <>
          {callbackConfig && (
            <div className="flex flex-col gap-[6px] rounded-[8px] bg-[#fff8e8] p-[10px] text-[11px] text-[#6f4500]">
              <span>请先将以下 Token 和 EncodingAESKey 填入微信客服后台，完成 API 创建：</span>
              <code className="break-all">Token: {callbackConfig.token}</code>
              <code className="break-all">EncodingAESKey: {callbackConfig.aesKey}</code>
            </div>
          )}
          {callbackReady && !configured && (
            <span className="text-[11px] text-[#858b9c]">回调已准备，请填写微信客服后台返回的 Secret；保存后选择已有客服账号或创建新账号。</span>
          )}
          {fields.map((field) => (
            <label key={field.key} className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
              {field.label}
              <Input
                type={field.secret ? 'password' : 'text'}
                value={values[field.key] || ''}
                placeholder={field.placeholder || ''}
                autoComplete="off"
                onChange={(event) =>
                  setValues((current) => ({ ...current, [field.key]: event.target.value }))
                }
                className="h-8 rounded-[10px] text-[12px]"
              />
            </label>
          ))}
          <div className="flex justify-end gap-[8px]">
            {configured && (
              <UIButton variant="outline" onClick={() => setEditing(false)} className={OUTLINE_BUTTON_CLASS}>
                取消
              </UIButton>
            )}
            <UIButton onClick={() => void save()} disabled={saving} className={PRIMARY_BUTTON_CLASS}>
              {saving ? '验证中' : '保存并验证'}
            </UIButton>
          </div>
        </>
      )}
    </div>
  );
}
