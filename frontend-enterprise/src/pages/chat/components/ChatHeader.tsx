import { useEffect, useState } from 'react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Badge } from '@/components/ui';
import { api, TENANT_ID } from '@/api/client';
import { staffdeckDisplayText } from '@/employee';
import type { TeamRead } from '@/types';
import IconEdit from '@/assets/icons/edit.svg?react';
import IconChevronDown from '@/assets/icons/chevron-down.svg?react';
import IconLogout from '@/assets/icons/logout.svg?react';
import LanguageSwitcher from '@/components/LanguageSwitcher';

import {
  CHAT_HEADER_CLASS,
  CHAT_HEADER_TITLE_NAME_CLASS,
  CHAT_HEADER_TITLE_STACK_CLASS,
} from '../chatPageStyles';
import type { UseChatSession } from '../useChatSession';

export default function ChatHeader({ chat }: { chat: UseChatSession }) {
  const { auth, currentSession, openRename, logout } = chat;
  const teamId = currentSession?.team_id || null;
  const rawName = currentSession?.title
    ? staffdeckDisplayText(currentSession.title)
    : currentSession?.id || '新对话';
  const name = teamId ? rawName.replace(/TL 对话/g, '项目领导对话') : rawName;
  const username = auth?.user?.username || '';
  const initial = username ? username.slice(0, 1).toUpperCase() : '--';

  // 团队会话徽标：read 带 team_name 直接用；缺省时用团队列表做 id→name 映射。
  const sessionTeamName = currentSession?.team_name || null;
  const [teamName, setTeamName] = useState<string | null>(sessionTeamName);

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
      <div className={CHAT_HEADER_TITLE_STACK_CLASS}>
        <span className="flex min-w-0 items-center gap-[4px]">
          <span className={CHAT_HEADER_TITLE_NAME_CLASS}>{name}</span>
          {teamId && (
            <Badge
              variant="secondary"
              className="shrink-0 rounded-full bg-[#e8f0ff] text-[12px] font-normal text-[#1a71ff]"
            >
              {teamName ? `团队 · ${teamName}` : '团队'}
            </Badge>
          )}
          {currentSession && (
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
    </div>
  );
}
