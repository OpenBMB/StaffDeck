import { useEffect, useState } from 'react';

import { notify } from '@/components/ui/app-toast';
import { Button as UIButton } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui';

import { api, TENANT_ID } from '../../api/client';
import type { ChannelBindingManagerRead } from '../../types';

type TenantUser = {
  id: string;
  username: string;
  display_name?: string;
  source?: string;
};

type Props = {
  bindingId: string;
  users: TenantUser[];
  creatorUserId?: string | null;
};

export default function BindingManagers({ bindingId, users, creatorUserId }: Props) {
  const [managers, setManagers] = useState<ChannelBindingManagerRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [candidate, setCandidate] = useState('');

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bindingId]);

  async function load() {
    setLoading(true);
    try {
      const data = await api.get<ChannelBindingManagerRead[]>(
        `/api/enterprise/channels/${bindingId}/managers?tenant_id=${TENANT_ID}`,
      );
      setManagers(data);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载协作者失败');
    } finally {
      setLoading(false);
    }
  }

  const existingIds = new Set(managers.map((m) => m.user_id));
  const candidates = users.filter(
    (u) => (!u.source || u.source === 'web') && u.id !== creatorUserId && !existingIds.has(u.id),
  );

  async function add() {
    if (!candidate) return;
    setAdding(true);
    try {
      await api.post(`/api/enterprise/channels/${bindingId}/managers?tenant_id=${TENANT_ID}`, {
        user_id: candidate,
      });
      setCandidate('');
      await load();
      notify.success('已添加协作者');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '添加协作者失败');
    } finally {
      setAdding(false);
    }
  }

  async function remove(userId: string) {
    try {
      await api.delete(
        `/api/enterprise/channels/${bindingId}/managers/${userId}?tenant_id=${TENANT_ID}`,
      );
      await load();
      notify.success('已移除协作者');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '移除协作者失败');
    }
  }

  return (
    <div className="flex flex-col gap-[12px] border-t border-[#eef0f4] pt-[16px]">
      <div className="flex min-w-0 flex-col gap-[4px]">
        <span className="text-[13px] font-semibold text-[#18181a]">协作者管理</span>
        <span className="text-[12px] leading-[1.6] text-[#858b9c]">
          协作者可配置/轮换凭证、管理挂载员工、启停渠道，但不能删除渠道或管理其他协作者。
        </span>
      </div>
      {loading ? (
        <span className="text-[12px] text-[#858b9c]">加载中…</span>
      ) : managers.length === 0 ? (
        <span className="text-[12px] text-[#858b9c]">暂无协作者</span>
      ) : (
        <ul className="flex flex-col gap-[8px]">
          {managers.map((m) => (
            <li key={m.user_id} className="flex items-center justify-between gap-[12px]">
              <div className="flex min-w-0 flex-col">
                <span className="truncate text-[12px] text-[#18181a]">{m.name || m.user_id}</span>
                <span className="text-[11px] text-[#858b9c]">
                  授权人：{m.granted_by_name || m.granted_by_user_id || '-'}
                </span>
              </div>
              <UIButton
                variant="outline"
                className="h-7 rounded-[8px] border-[#e3e7f1] px-3 text-[11px] text-[#464c5e] hover:bg-[#f6f6f6]"
                onClick={() => void remove(m.user_id)}
              >
                移除
              </UIButton>
            </li>
          ))}
        </ul>
      )}
      {candidates.length > 0 ? (
        <div className="flex items-center gap-[8px]">
          <Select value={candidate || '__none__'} onValueChange={(v) => setCandidate(v === '__none__' ? '' : v)}>
            <SelectTrigger className="h-[32px] w-[180px] text-[12px]">
              <SelectValue placeholder="选择成员" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">未选择</SelectItem>
              {candidates.map((u) => (
                <SelectItem key={u.id} value={u.id}>
                  {u.display_name || u.username || u.id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <UIButton
            variant="outline"
            className="h-8 rounded-[8px] border-[#e3e7f1] px-4 text-[12px] text-[#464c5e] hover:bg-[#f6f6f6]"
            disabled={adding || !candidate}
            onClick={() => void add()}
          >
            添加
          </UIButton>
        </div>
      ) : (
        <span className="text-[11px] text-[#858b9c]">无可添加的成员（协作者须为当前租户内部成员，且排除创建者与管理员）</span>
      )}
    </div>
  );
}
