import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui';
import { cn } from '@/lib/utils';

import IconChat from '../assets/icons/chat.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconPlatform from '../assets/icons/nav-platform.svg?react';
import IconImage from '../assets/icons/image.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconPause from '../assets/icons/pause.svg?react';
import IconPlay from '../assets/icons/play.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import { isGalleryEmployee } from '../auth';
import { employeeDisplayNameWithCreator, employeeProfile, resourceCount } from '../employee';
import type { AgentProfileRead } from '../types';
import EmployeeAvatar from './EmployeeAvatar';

// Hover colors come from the scoped --accent / --accent-foreground overrides on
// DropdownMenuContent (see below), so items only need layout + default color here.
// Kept in sync with the ScheduledTasksTab action menu.
const MENU_ITEM_CLASS =
  'cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#858b9c] focus:text-[#18181a]';
const MENU_ITEM_DANGER_CLASS =
  'cursor-pointer gap-[4px] rounded-[10px] px-[12px] py-[6px] text-[12px] text-[#d20b0b] focus:bg-[#fce7e7] focus:text-[#d20b0b] focus:[&_svg]:text-[#d20b0b]!';

export type EmployeeCardProps = {
  employee: AgentProfileRead;
  canManage: boolean;
  selected?: boolean;
  /** Show the top-right "更多" actions menu. Hidden on the 对话端 gallery. */
  showMenu?: boolean;
  onOpen: () => void;
  onStatus: (status: 'active' | 'archived') => void;
  onGallery: (published: boolean) => void;
  onDelete: () => void;
  onAvatar: () => void;
  onEdit: () => void;
  onChat: () => void;
};

export default function EmployeeCard({
  employee,
  canManage,
  selected = false,
  showMenu = true,
  onOpen,
  onStatus,
  onGallery,
  onDelete,
  onAvatar,
  onEdit,
  onChat,
}: EmployeeCardProps) {
  const profile = employeeProfile(employee);
  const sopCount = resourceCount(employee.resources, 'skill');
  const skillCount = resourceCount(employee.resources, 'general_skill');
  const kbCount = resourceCount(employee.resources, 'knowledge_base');
  const galleryPublished = isGalleryEmployee(employee);
  const online = employee.status === 'active';

  // Show raw API values on the card (bypass the SD1 term relabeling in staffdeckDisplayText).
  const rawRoleName = (employee.metadata?.role_name as string | undefined) || profile.roleName;
  const displayName = employee.is_overall ? '开放广场' : employeeDisplayNameWithCreator(employee);
  const displayDescription = employee.description || '暂无描述';

  const stats: Array<{ value: number; label: string }> = [
    { value: kbCount, label: '资料' },
    { value: skillCount, label: '技能' },
    { value: sopCount, label: 'SOP' },
  ];

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpen();
        }
      }}
      aria-pressed={selected}
      className={cn(
        'group relative flex h-full flex-col cursor-pointer overflow-visible rounded-[20px] border border-[#F6F6F6] bg-white py-[12px] px-[10px] transition-shadow',
        '',
        selected && 'shadow-[0_16px_30px_0_rgba(0,0,0,0.10)]',
      )}
    >
      {/* Header band (shorter than the avatar so the illustration overflows above it) */}
      <div className="flex rounded-[18px] h-[68px] box-border gap-[10px] bg-[#f6f6f6] p-[8px] mt-[34px]" >

        {/* Avatar illustration — absolutely positioned so its head pokes above the gray band */}
        <div className='w-[80px] relative'>
          <div className='absolute inset-0 flex items-end justify-center'>
            <EmployeeAvatar
              agent={employee}
              width={80}
              height={94}
              fit="contain"
              objectPosition="center bottom"
              className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
            />
          </div>
          

        </div>

        {/* Name / role / status */}
        <div className="flex-1 flex flex-col gap-[2px]">
          <strong className="truncate text-[12px] font-bold text-[#18181A]">
            {displayName}
          </strong>
          <span className="truncate text-[10px] text-[#757F9C]">{rawRoleName}</span>
          <div className="leading-none">
            <span className="inline-flex items-center gap-[2px] py-[2px] px-[4px] text-[8px] font-semibold text-[#757F9C] rounded-[90px] bg-white">
              <i className={cn('size-[6px] shrink-0 rounded-full', online ? 'bg-[#22c55e]' : 'bg-[#9ca3af]')} aria-hidden="true" />
              {online ? '在线' : '下线'}
            </span>
          </div>
        </div>

        {/* Chat button */}
        <button
          type="button"
          aria-label="发起对话"
          disabled={!online}
          onClick={(event) => {
            event.stopPropagation();
            onChat();
          }}
          className="grid size-[28px] shrink-0 self-center place-items-center rounded-[10px] bg-white text-[#757F9C] transition-colors hover:text-[#18181A] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:text-[#757F9C]"
        >
          <IconChat className="size-[16px]!" />
        </button>

      </div>

      {/* Actions menu */}
      {showMenu && (
      <div className="absolute right-[12px] top-[12px] z-20">
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="员工操作"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
            className="grid size-7 place-items-center rounded-[10px] text-[#757F9C] transition-colors outline-none hover:bg-black/5 focus-visible:bg-black/5"
          >
            <IconMore className="size-[16px]!" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="flex w-auto min-w-[128px] flex-col gap-[4px] rounded-[14px] border-0 bg-white p-[4px] shadow-[0px_0px_8px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
            onCloseAutoFocus={(event) => event.preventDefault()}
          >
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!online}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onChat()}
            >
              <IconChat className="size-[16px]" />
              发起对话
            </DropdownMenuItem>
            {online ? (
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                disabled={!canManage}
                onClick={(event) => event.stopPropagation()}
                onSelect={() => onStatus('archived')}
              >
                <IconPause className="size-[16px]" />
                下线
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                className={MENU_ITEM_CLASS}
                disabled={!canManage}
                onClick={(event) => event.stopPropagation()}
                onSelect={() => onStatus('active')}
              >
                <IconPlay className="size-[16px]" />
                上线
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onGallery(!galleryPublished)}
            >
              <IconPlatform className="size-[16px]" />
              {galleryPublished ? '从广场下架' : '发布到广场'}
            </DropdownMenuItem>
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onEdit()}
            >
              <IconEdit className="size-[16px]" />
              编辑资料
            </DropdownMenuItem>
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={!canManage}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onAvatar()}
            >
              <IconImage className="size-[16px]" />
              设置头像
            </DropdownMenuItem>
            <DropdownMenuSeparator className="my-[2px] bg-[#eef0f4]" />
            <DropdownMenuItem
              variant="destructive"
              className={MENU_ITEM_DANGER_CLASS}
              disabled={!canManage}
              onClick={(event) => event.stopPropagation()}
              onSelect={() => onDelete()}
            >
              <IconTrash className="size-[16px]" />
              删除
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      )}

      {/* Description */}
      <p className="line-clamp-2 mt-[8px] h-[36px] shrink-0 text-[12px] leading-[18px] text-[#757F9C]">
        {displayDescription}
      </p>

      {/* Work style tags */}
      <div className="flex flex-wrap my-[8px] items-center gap-[10px]">
        {profile.workStyles.slice(0, 3).map((item) => (
          <span
            key={item}
            className="rounded-[20px] px-[8px] py-px text-[10px] leading-[13px] text-[#757f9c] border border-[#E3E7F1]"
          >
            {item}
          </span>
        ))}
      </div>

      {/* Stats — pinned to the bottom of the card */}
      <div className="mt-auto grid grid-cols-3 rounded-[14px] border border-[#E3E7F1] box-sizing: border-box">
        {stats.map((stat, index) => (
          <div
            key={stat.label}
            className={cn(
              'flex flex-col justify-center gap-[4px] px-[20px] py-[6px]',
              index < stats.length - 1 && 'border-r border-[#eef1f5]',
            )}
          >
            <strong className="text-[18px] leading-[24px] font-bold text-[#18181A]">{stat.value}</strong>
            <em className="text-[10px] not-italic text-[#464C5E]">{stat.label}</em>
          </div>
        ))}
      </div>
    </div>
  );
}
