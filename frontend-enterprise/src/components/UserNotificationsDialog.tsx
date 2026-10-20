import { useEffect, useState } from 'react';
import { Bell, CheckCheck } from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogTitle,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import { api, TENANT_ID } from '../api/client';
import type { UserNotificationRead } from '../types';
import { formatDateTime } from '@/lib/enterprise-ui';

export const NOTIFICATIONS_CHANGED_EVENT = 'ultrarag-notifications-change';

export function notifyNotificationsChanged() {
  window.dispatchEvent(new Event(NOTIFICATIONS_CHANGED_EVENT));
}

export default function UserNotificationsDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<UserNotificationRead[]>([]);
  const [loading, setLoading] = useState(false);
  const unreadCount = rows.filter((row) => !row.read).length;

  const load = () => {
    setLoading(true);
    return api
      .get<UserNotificationRead[]>(`/api/enterprise/notifications?tenant_id=${TENANT_ID}&limit=100`)
      .then(setRows)
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载消息失败'))
      .finally(() => {
        setLoading(false);
        notifyNotificationsChanged();
      });
  };

  useEffect(() => {
    if (open) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  async function markAllRead() {
    try {
      await api.post(`/api/enterprise/notifications/read-all?tenant_id=${TENANT_ID}`, {});
      setRows((current) => current.map((row) => ({ ...row, read: true })));
      notifyNotificationsChanged();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '标记已读失败');
    }
  }

  async function openNotification(row: UserNotificationRead) {
    try {
      if (!row.read) {
        await api.post(
          `/api/enterprise/notifications/${row.id}/read?tenant_id=${TENANT_ID}`,
          {},
        );
        setRows((current) => current.map((item) => (
          item.id === row.id ? { ...item, read: true } : item
        )));
        notifyNotificationsChanged();
      }
      if (row.link) {
        const agentId = typeof row.metadata.agent_id === 'string' ? row.metadata.agent_id : '';
        if (agentId && row.link.startsWith('/enterprise/tools')) {
          window.localStorage.setItem('ultrarag_enterprise_agent_scope', agentId);
          window.dispatchEvent(
            new CustomEvent('ultrarag-enterprise-agent-scope-change', {
              detail: { agentId },
            }),
          );
        }
        onClose();
        window.location.assign(row.link);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '打开消息失败');
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="flex max-h-[calc(100dvh-80px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[520px]">
        <div className="flex items-center justify-between gap-[12px] border-b border-[#eceef1] px-[20px] py-[14px]">
          <DialogTitle className="flex items-center gap-[8px] text-[15px] font-semibold text-[#18181a]">
            <Bell className="size-[16px]" />
            我的消息
            {unreadCount > 0 && (
              <span className="rounded-full bg-[#f5483b] px-[6px] py-[1px] text-[10px] leading-[14px] text-white">
                {unreadCount}
              </span>
            )}
          </DialogTitle>
          {unreadCount > 0 && (
            <UIButton
              variant="ghost"
              size="sm"
              onClick={() => void markAllRead()}
              className="h-[28px] gap-[4px] px-[8px] text-[12px] text-[#757f9c]"
            >
              <CheckCheck className="size-[13px]" />
              全部已读
            </UIButton>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-[10px]">
          {loading && !rows.length ? (
            <p className="py-[40px] text-center text-[12px] text-[#a7adbb]">加载中…</p>
          ) : rows.length ? (
            <div className="flex flex-col gap-[6px]">
              {rows.map((row) => (
                <button
                  key={row.id}
                  type="button"
                  onClick={() => void openNotification(row)}
                  className={cn(
                    'flex w-full items-start gap-[10px] rounded-[12px] px-[12px] py-[11px] text-left transition-colors hover:bg-[#f6f7fa]',
                    !row.read && 'bg-[#fafbfe]',
                  )}
                >
                  <span
                    className={cn(
                      'mt-[6px] size-[7px] shrink-0 rounded-full',
                      row.read ? 'bg-transparent' : 'bg-[#f5483b]',
                    )}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline justify-between gap-[10px]">
                      <strong className="truncate text-[13px] font-medium text-[#18181a]">
                        {row.title}
                      </strong>
                      <span className="shrink-0 text-[10px] text-[#a7adbb]">
                        {formatDateTime(row.created_at)}
                      </span>
                    </span>
                    <span className="mt-[3px] block text-[12px] leading-[1.55] text-[#757f9c]">
                      {row.message}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="flex flex-col items-center gap-[8px] py-[48px] text-center">
              <Bell className="size-[24px] text-[#c0c6d4]" />
              <p className="text-[13px] text-[#858b9c]">暂无消息</p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
