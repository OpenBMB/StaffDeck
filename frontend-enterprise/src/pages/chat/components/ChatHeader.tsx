import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft, KeyRound, Lock } from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Badge } from '@/components/ui';
import { api, TENANT_ID } from '@/api/client';
import {
  getEnterpriseAuthSession,
  setEnterpriseAuthSession,
} from '@/auth';
import { staffdeckDisplayText } from '@/employee';
import { EnterpriseRoute } from '@/enums/routes';
import { cn } from '@/lib/utils';
import type { TeamRead } from '@/types';
import IconEdit from '@/assets/icons/edit.svg?react';
import IconChevronDown from '@/assets/icons/chevron-down.svg?react';
import IconLogout from '@/assets/icons/logout.svg?react';
import AccountApiKeyDialog from '@/components/AccountApiKeyDialog';
import ChangePasswordDialog from '@/components/ChangePasswordDialog';
import EditDepartmentDialog from '@/components/EditDepartmentDialog';
import LanguageSwitcher from '@/components/LanguageSwitcher';

import {
  CHAT_HEADER_CLASS,
  CHAT_HEADER_TITLE_NAME_CLASS,
  CHAT_HEADER_TITLE_STACK_CLASS,
} from '../chatPageStyles';
import type { UseChatSession } from '../useChatSession';

export default function ChatHeader({ chat }: { chat: UseChatSession }) {
  const { auth, currentSession, openRename, logout } = chat;
  const navigate = useNavigate();
  const teamId = currentSession?.team_id || null;
  const rawName = currentSession?.title
    ? staffdeckDisplayText(currentSession.title)
    : currentSession?.id || '新对话';
  const username = auth?.user?.username || '';
  const initial = username ? username.slice(0, 1).toUpperCase() : '--';

  // 团队会话徽标：read 带 team_name 直接用；缺省时用团队列表做 id→name 映射。
  const sessionTeamName = currentSession?.team_name || null;
  const [teamName, setTeamName] = useState<string | null>(sessionTeamName);
  const [apiKeyOpen, setApiKeyOpen] = useState(false);
  const [changePasswordOpen, setChangePasswordOpen] = useState(false);
  const [editDepartmentOpen, setEditDepartmentOpen] = useState(false);
  const name = teamId
    ? teamName || rawName.replace(/^团队\s*/, '').replace(/\s*·\s*TL 对话$/, '')
    : rawName;

  const user = auth?.user;
  const displayName = user?.display_name || user?.username || '';
  const isAdmin = user?.role === 'admin';
  // 发起人非当前用户时展示其名字（管理员/处理人查看他人会话的场景）
  const sessionOwnerName = currentSession?.user_display_name
    && currentSession.user_id
    && currentSession.user_id !== user?.id
    ? currentSession.user_display_name
    : null;

  useEffect(() => {
    setTeamName(sessionTeamName);
    if (!teamId || sessionTeamName) return;
    let cancelled = false;
    api.get<TeamRead[]>(`/api/enterprise/teams?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        if (!cancelled) setTeamName(rows.find((team) => team.id === teamId)?.name || null);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [teamId, sessionTeamName]);

  return (
    <div className={CHAT_HEADER_CLASS}>
      <div className="flex min-w-0 items-center gap-[8px]">
        {teamId && (
          <button
            type="button"
            aria-label="返回团队"
            onClick={() => navigate(`${EnterpriseRoute.Teams}/${teamId}`)}
            className="inline-grid size-[30px] shrink-0 place-items-center rounded-[9px] border border-[#e3e7f1] text-[#464c5e] transition-colors hover:bg-[#f6f6f6]"
          >
            <ChevronLeft className="size-[16px]" />
          </button>
        )}
        <div className={CHAT_HEADER_TITLE_STACK_CLASS}>
        <span className="flex min-w-0 items-center gap-[4px]">
          <span className={CHAT_HEADER_TITLE_NAME_CLASS}>{name}</span>
          {sessionOwnerName && (
            <Badge
              variant="secondary"
              className="shrink-0 rounded-full bg-[#f2f3f7] text-[11px] font-normal text-[#757f9c]"
            >
              {sessionOwnerName} 发起
            </Badge>
          )}
          {teamId && (
            <Badge
              variant="secondary"
              className="shrink-0 rounded-full bg-[#e8f0ff] text-[12px] font-normal text-[#1a71ff]"
            >
              群聊
            </Badge>
          )}
          {currentSession && !teamId && (
            <button
              type="button"
              aria-label="重命名会话"
              onClick={() => openRename(currentSession)}
              className="inline-grid size-[14px] shrink-0 place-items-center text-[#858b9c] transition-colors hover:text-[#18181a]"
            >
              <IconEdit className="size-[14px]!" />
            </button>
          )}
        </span>
      </div>
    </div>

      <div className="flex shrink-0 items-center gap-[8px]">
        <LanguageSwitcher />
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="账户菜单"
            className="flex shrink-0 items-center gap-[10px] rounded-[10px] py-[4px] pl-[6px] pr-[10px] outline-none transition-colors"
          >
            <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[14px] font-medium text-[#7e96dc]">
              {initial}
            </span>
            <IconChevronDown className="size-[14px] shrink-0 text-[#757F9C]" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="w-fit min-w-[160px] rounded-[14px] border-0 bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
          >
            {user && (
              <>
                <div className="flex max-w-[240px] flex-col gap-[10px] px-[12px] pt-[8px] pb-[12px]">
                  <div className="flex items-center gap-[10px]">
                    <span className="grid size-[40px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[16px] font-medium text-[#7e96dc]">
                      {initial}
                    </span>
                  <div className="flex min-w-0 flex-col gap-[2px]">
                    <span className="truncate text-[14px] font-medium text-[#18181a]">
                      {displayName}
                    </span>
                    {user.username && user.username !== displayName && (
                      <span className="truncate text-[12px] text-[#858b9c]">
                        @{user.username}
                      </span>
                    )}
                    <span className="mt-[2px] flex min-w-0 items-center gap-[4px]">
                      <span className="shrink-0 text-[12px] text-[#858b9c]">部门</span>
                      <span className="truncate text-[12px] text-[#464c5e]">
                        {user?.department || '未设置'}
                      </span>
                      <button
                        type="button"
                        title="修改部门名"
                        aria-label="修改部门名"
                        onClick={() => setEditDepartmentOpen(true)}
                        className="inline-grid size-[16px] shrink-0 place-items-center text-[#a0a8bd] transition-colors hover:text-[#18181a]"
                      >
                        <IconEdit className="size-[11px]" />
                      </button>
                    </span>
                  </div>
                  </div>
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
            {user && (
              <DropdownMenuItem
                onSelect={() => setApiKeyOpen(true)}
                className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
              >
                <KeyRound className="size-[16px]" />
                API 全量密钥
              </DropdownMenuItem>
            )}
            {user && (
              <DropdownMenuItem
                onSelect={() => setChangePasswordOpen(true)}
                className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
              >
                <Lock className="size-[16px]" />
                修改密码
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              onSelect={logout}
              className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
            >
              <IconLogout className="size-[16px]" />
              退出登录
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <AccountApiKeyDialog
        account={user ?? null}
        open={apiKeyOpen}
        onClose={() => setApiKeyOpen(false)}
      />
      <ChangePasswordDialog
        open={changePasswordOpen}
        onClose={() => setChangePasswordOpen(false)}
      />
      <EditDepartmentDialog
        open={editDepartmentOpen}
        currentDepartment={user?.department || ''}
        onClose={() => setEditDepartmentOpen(false)}
        onSaved={(department) => {
          const session = getEnterpriseAuthSession();
          if (session) {
            setEnterpriseAuthSession({
              ...session,
              user: { ...session.user, department },
            });
          }
        }}
      />
    </div>
  );
}
