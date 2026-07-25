import { useRef, useState, type ReactNode } from 'react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconLogout from '../assets/icons/logout.svg?react';
import { api } from '../api/client';
import {
  getEnterpriseAuthSession,
  setEnterpriseAuthSession,
  type EnterpriseAuthUser,
} from '../auth';
import LanguageSwitcher from './LanguageSwitcher';

/** 只允许 http/https/data:image/blob 协议的图片地址,其余一律视为无效。 */
function safeImageUrl(value: string): string {
  const text = value.trim();
  if (!text) return '';
  try {
    const parsed = new URL(text, window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
    if (parsed.protocol === 'blob:') return parsed.href;
    if (parsed.protocol === 'data:' && /^data:image\//i.test(text)) return text;
  } catch {
    // 非法 URL 视为无效
  }
  return '';
}

export type AppHeaderProps = {
  /**
   * Page-specific content rendered on the left side of the header. When
   * provided it takes precedence over the `title` / `description` fields.
   */
  left?: ReactNode;
  /** Convenience field for the left slot's title line. Ignored when `left` is set. */
  title?: ReactNode;
  /** Convenience field for the left slot's description line. Ignored when `left` is set. */
  description?: ReactNode;
  /**
   * Custom content for the right side of the header. When provided it fully
   * replaces the default user avatar / logout dropdown (used e.g. on the
   * signed-out login page which shows a theme toggle + login button instead).
   */
  right?: ReactNode;
  /** Called when the logout menu item is clicked. */
  onLogout?: () => void;
  /** Current user's display name, used for the avatar initial. */
  userName?: string;
  className?: string;
};

/**
 * Global page header. The right side shows a user avatar button whose dropdown
 * holds the logout action; the left side is provided per-page via the `left`
 * slot, or via the `title` / `description` convenience fields. When `left` is
 * passed it is rendered as-is and the convenience fields are ignored.
 * Pass `right` to override the default avatar with page-specific actions.
 */
export default function AppHeader({
  left,
  title,
  description,
  right,
  onLogout,
  userName,
  className,
}: AppHeaderProps) {
  const [user, setUser] = useState(() => getEnterpriseAuthSession()?.user);
  const [uploadPreviewUrl, setUploadPreviewUrl] = useState('');
  const [avatarSaving, setAvatarSaving] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const displayName = user?.display_name || user?.username || '';
  const initial = (displayName || userName || '').trim()?.[0]?.toUpperCase();
  const isAdmin = user?.role === 'admin';
  const avatarUrl = uploadPreviewUrl || user?.avatar_url || '';
  // 仅放行 http/https/data:image/blob 协议,阻止 javascript: 等可执行协议注入
  const safeAvatarUrl = safeImageUrl(avatarUrl);

  function clearUploadPreview() {
    if (uploadPreviewUrl) URL.revokeObjectURL(uploadPreviewUrl);
    setUploadPreviewUrl('');
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  async function refreshSessionUser() {
    const session = getEnterpriseAuthSession();
    if (!session?.token) return;
    try {
      const fresh = await api.get<EnterpriseAuthUser>('/api/auth/me');
      setEnterpriseAuthSession({ token: session.token, user: fresh });
      setUser(fresh);
    } catch {
      // 头像操作已成功时会话刷新失败不阻断,下次登录/刷新自然同步
    }
  }

  // 选图即传:本地预览乐观渲染,成功后刷新会话;失败回滚并提示
  async function pickAvatar(file: File | null) {
    if (!file || avatarSaving) return;
    clearUploadPreview();
    const objectUrl = URL.createObjectURL(file);
    setUploadPreviewUrl(objectUrl);
    setAvatarSaving(true);
    try {
      const session = getEnterpriseAuthSession();
      const form = new FormData();
      form.append('file', file);
      const apiBase = import.meta.env.VITE_API_BASE_URL || '';
      const response = await fetch(`${apiBase}/api/auth/me/avatar`, {
        method: 'PUT',
        headers: session?.token ? { Authorization: `Bearer ${session.token}` } : {},
        body: form,
      });
      if (!response.ok) throw new Error('上传头像失败');
      notify.success('头像已更新');
      await refreshSessionUser();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '上传头像失败');
    } finally {
      setAvatarSaving(false);
      clearUploadPreview();
    }
  }

  async function removeAvatar() {
    if (avatarSaving) return;
    setAvatarSaving(true);
    try {
      await api.delete('/api/auth/me/avatar');
      notify.success('头像已移除');
      await refreshSessionUser();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '移除头像失败');
    } finally {
      setAvatarSaving(false);
    }
  }

  const leftContent = left ?? (
    (title !== undefined || description !== undefined) ? (
      <div className="flex min-h-[40px] flex-col justify-center gap-[4px]">
        {title !== undefined && (
          <p className="text-[16px] font-medium leading-[normal] text-[#464c5e]">{title}</p>
        )}
        {description !== undefined && (
          <p className="text-[14px] leading-[normal] text-[#757f9c]">{description}</p>
        )}
      </div>
    ) : null
  );

  return (
    <header className={cn('flex w-full items-start gap-[16px]', className)}>
      <div className="min-w-0 flex-1">{leftContent}</div>
      <div className="flex h-[32px] shrink-0 items-center gap-[8px]">
        <LanguageSwitcher />
        {right !== undefined ? right : (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label="账户菜单"
              className="flex h-[32px] shrink-0 items-center gap-[8px] rounded-[10px] pl-[4px] pr-[8px] outline-none"
            >
              <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[14px] font-medium leading-none text-[#7e96dc]">
                {safeAvatarUrl ? (
                  <img src={safeAvatarUrl} alt="" className="size-full object-cover" />
                ) : (
                  (initial ?? '--')
                )}
              </span>
              <IconChevronDown className="size-[14px] shrink-0 text-[#757F9C]" />
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="w-fit min-w-0 rounded-[14px] border-0 bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
            >
              {user && (
                <>
                  <div className="flex max-w-[240px] flex-col gap-[10px] px-[12px] pt-[8px] pb-[12px]">
                    <div className="flex items-center gap-[10px]">
                      <div className="relative shrink-0">
                        <button
                          type="button"
                          title="更换头像"
                          aria-label="更换头像"
                          onClick={() => {
                            // 打开文件对话框前清空 input,确保重复选择同一文件也能触发 onChange
                            if (fileInputRef.current) fileInputRef.current.value = '';
                            fileInputRef.current?.click();
                          }}
                          className={cn(
                            'block size-[40px] overflow-hidden rounded-full transition-opacity',
                            avatarSaving && 'pointer-events-none opacity-60',
                          )}
                        >
                          {safeAvatarUrl ? (
                            <img
                              src={safeAvatarUrl}
                              alt=""
                              className="size-full object-cover"
                            />
                          ) : (
                            <span className="grid size-full place-items-center bg-[#eef1fb] text-[16px] font-medium leading-none text-[#7e96dc]">
                              {initial ?? '--'}
                            </span>
                          )}
                        </button>
                        {avatarSaving && (
                          <span className="pointer-events-none absolute inset-0 grid place-items-center">
                            <span className="size-[18px] animate-spin rounded-full border-2 border-white/40 border-t-white" />
                          </span>
                        )}
                        {!avatarSaving && (
                          <span className="pointer-events-none absolute -bottom-[2px] -right-[2px] grid size-[16px] place-items-center rounded-full bg-[#18181a] text-white">
                            <IconEdit className="size-[9px]" />
                          </span>
                        )}
                      </div>
                      <div className="flex min-w-0 flex-col gap-[2px]">
                        <span className="truncate text-[14px] font-medium text-[#18181a]">
                          {displayName}
                        </span>
                        {user.username && user.username !== displayName && (
                          <span className="truncate text-[12px] text-[#858b9c]">
                            @{user.username}
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center justify-between gap-[12px]">
                      <div className="flex items-center gap-[10px]">
                        <span
                          className={cn(
                            'inline-flex items-center rounded-full px-[12px] py-[4px] text-[10px] leading-none whitespace-nowrap',
                            isAdmin
                              ? 'bg-[#e8f0ff] text-[#1a71ff]'
                              : 'bg-[#f2f3f7] text-[#858b9c]',
                          )}
                        >
                          {isAdmin ? '管理员' : '成员'}
                        </span>
                        {user.avatar_url && (
                          <button
                            type="button"
                            onClick={() => void removeAvatar()}
                            disabled={avatarSaving}
                            className="text-[11px] text-[#a0a8bd] transition-colors hover:text-[#d20b0b] disabled:opacity-50"
                          >
                            移除头像
                          </button>
                        )}
                      </div>
                      {isAdmin && (
                        <span className="max-w-[150px] truncate text-[11px] text-[#a0a8bd]">
                          {user.tenant_id}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="mx-[6px] mb-[6px] h-px bg-[#eef0f4]" />
                </>
              )}
              <DropdownMenuItem
                onSelect={() => onLogout?.()}
                className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
              >
                <IconLogout className="size-[16px]" />
                退出登录
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {/* 文件 input 常驻在 header 根部(不在下拉菜单内),菜单关闭也不会被卸载;
          选图即传:本地预览乐观渲染,上传成功刷新会话,失败回滚 */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(event) => pickAvatar(event.target.files?.[0] || null)}
      />
    </header>
  );
}
