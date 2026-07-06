import { useMemo } from 'react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from '@/components/ui/sidebar';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { ComponentType, SVGProps } from 'react';
import { cn } from '@/lib/utils';
import EmployeeAvatar from './EmployeeAvatar';
import BrandLogo from './BrandLogo';
import StaffdeckIcon from './StaffdeckIcon';
import { employeeDisplayName, employeeProfile, staffdeckDisplayText } from '../employee';
import { EnterpriseRoute } from '../enums/routes';
import type { AgentProfileRead, ChatSession } from '../types';
import IconPlatform from '../assets/icons/nav-platform.svg?react';
import IconAgents from '../assets/icons/nav-agents.svg?react';
import IconFile from '../assets/icons/profile-file.svg?react';
import IconAlarm from '../assets/icons/profile-alarm.svg?react';
import IconHistory from '../assets/icons/profile-history.svg?react';
import IconCalendar from '../assets/icons/profile-calendar.svg?react';
import IconFolder from '../assets/icons/cap-folder.svg?react';
import IconMagicWand from '../assets/icons/cap-magicwand.svg?react';
import IconClipboard from '../assets/icons/cap-clipboard.svg?react';
import IconBriefcase from '../assets/icons/cap-briefcase.svg?react';
import IconChat from '../assets/icons/action-chat.svg?react';
import IconToggle from '../assets/icons/action-toggle.svg?react';
import IconHeaderCollapse from '../assets/icons/header-collapse.svg?react';
import IconAccounts from '../assets/icons/sys-accounts.svg?react';
import IconModels from '../assets/icons/sys-models.svg?react';
import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconAdd from '../assets/icons/add.svg?react';
import IconSort from '../assets/icons/sort.svg?react';
import IconGlobe from '../assets/icons/globe.svg?react';
import IconViewMasonry from '../assets/icons/view-masonry.svg?react';
import IconChatBubble from '../assets/icons/chat.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';

type IconComponent = ComponentType<SVGProps<SVGSVGElement>>;

type NavItem = {
  route: EnterpriseRoute;
  label: string;
  Icon: IconComponent;
};

const PRIMARY_NAV: NavItem[] = [
  { route: EnterpriseRoute.Platform, label: '开放广场平台', Icon: IconPlatform },
  { route: EnterpriseRoute.Agents, label: '我的数字员工', Icon: IconAgents },
];

const PROFILE_NAV: NavItem[] = [
  { route: EnterpriseRoute.Dashboard, label: '员工档案', Icon: IconFile },
  { route: EnterpriseRoute.ScheduledTasks, label: '定时任务', Icon: IconAlarm },
  { route: EnterpriseRoute.Memories, label: '记忆', Icon: IconHistory },
  { route: EnterpriseRoute.Feedback, label: '对话日志', Icon: IconCalendar },
];

const CAPABILITY_NAV: NavItem[] = [
  { route: EnterpriseRoute.Knowledge, label: '知识库', Icon: IconFolder },
  { route: EnterpriseRoute.GeneralSkills, label: '技能', Icon: IconMagicWand },
  { route: EnterpriseRoute.Skills, label: 'SOP', Icon: IconClipboard },
  { route: EnterpriseRoute.Tools, label: '工具', Icon: IconBriefcase },
];

const SYSTEM_NAV: NavItem[] = [
  { route: EnterpriseRoute.Accounts, label: '账号管理', Icon: IconAccounts },
  { route: EnterpriseRoute.Models, label: '模型配置', Icon: IconModels },
];

function primaryNavItems(isAdmin: boolean): NavItem[] {
  return isAdmin ? [...PRIMARY_NAV, ...SYSTEM_NAV] : PRIMARY_NAV;
}

export type AppSidebarManagementProps = {
  variant?: 'management';
  selected: string;
  onNavigate: (route: string) => void;
  isAdmin: boolean;
  sidebarAgent?: AgentProfileRead;
  scopeAgents: AgentProfileRead[];
  selectedAgentId: string;
  onSelectAgent: (agentId: string) => void;
  onOpenChat: () => void;
};

export type ChatSessionFilterOption = { value: string; label: string };

export type AppSidebarChatProps = {
  variant: 'chat';
  /** Sessions already filtered for the sidebar list. */
  sessions: ChatSession[];
  /** Whether the initial session list is still loading. */
  sessionsLoading?: boolean;
  /** Full agent roster, used to resolve per-session avatars/roles. */
  agents: AgentProfileRead[];
  activeSessionId?: string;
  sessionFilter: string;
  onSessionFilterChange: (value: string) => void;
  sessionFilterOptions: ChatSessionFilterOption[];
  isSessionUnread: (session: ChatSession) => boolean;
  onOpenSession: (id: string) => void;
  onOpenGallery: () => void;
  onRenameSession: (session: ChatSession) => void;
  onDeleteSession: (session: ChatSession) => void;
  onOpenAdmin: () => void;
};

export type AppSidebarProps = AppSidebarManagementProps | AppSidebarChatProps;

// Shared shell classes so the management + chat sidebars share the same chrome.
const SIDEBAR_SHELL_CLASS =
  'overflow-hidden border-r border-sidebar-border bg-sidebar backdrop-blur-[9.5px] **:data-[slot=sidebar-inner]:bg-sidebar';

function PrimaryNavButton({
  item,
  selected,
  onNavigate,
}: {
  item: NavItem;
  selected: string;
  onNavigate: (route: string) => void;
}) {
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        tooltip={item.label}
        isActive={selected === item.route}
        onClick={() => onNavigate(item.route)}
        className={cn(
          'h-[40px] gap-[10px] rounded-[14px] px-[20px] py-[10px] text-[14px] text-sidebar-foreground',
          'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
          'data-active:bg-sidebar-accent data-active:text-sidebar-accent-foreground data-active:font-normal',
          'group-data-[collapsible=icon]:px-0!',
        )}
      >
        <item.Icon className="size-[16px]!" />
        <span className="text-[14px]">{item.label}</span>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

function CardNavButton({
  item,
  selected,
  onNavigate,
}: {
  item: NavItem;
  selected: string;
  onNavigate: (route: string) => void;
}) {
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        tooltip={item.label}
        isActive={selected === item.route}
        onClick={() => onNavigate(item.route)}
        className={cn(
          'h-[36px] gap-[8px] rounded-[12px] px-[12px] py-[4px] text-[12px] text-sidebar-foreground',
          'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
          'data-active:bg-sidebar-accent data-active:text-sidebar-accent-foreground data-active:font-normal',
          'group-data-[collapsible=icon]:px-0!',
        )}
      >
        <item.Icon className="size-[14px]!" />
        <span>{item.label}</span>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

function GroupLabel({ children }: { children: string }) {
  return (
    <span className="px-[8px] pt-[6px] pb-[2px] text-[10px] leading-none text-[#464c5e] dark:text-sidebar-foreground group-data-[collapsible=icon]:hidden">
      {children}
    </span>
  );
}

function AgentSwitcher({
  sidebarAgent,
  scopeAgents,
  selectedAgentId,
  onSelectAgent,
}: Pick<AppSidebarManagementProps, 'sidebarAgent' | 'scopeAgents' | 'selectedAgentId' | 'onSelectAgent'>) {
  const caption = sidebarAgent ? '当前员工' : '未选择';
  const nameLabel = sidebarAgent
    ? sidebarAgent.is_overall
      ? '开放广场'
      : employeeDisplayName(sidebarAgent)
    : '-';

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <div
          aria-label="切换当前员工"
          className={cn(
            'flex w-full items-center gap-[12px] rounded-[18px] px-[8px] pt-[8px] pb-[4px] text-left transition-colors',
            'group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0 group-data-[collapsible=icon]:py-0',
          )}
        >
          {sidebarAgent ? (
            <div className="w-[60px] h-[30px] relative">
              <div className="absolute inset-0 flex items-end justify-center">
                <EmployeeAvatar agent={sidebarAgent} width={60} height={71} />
              </div>
            </div>
          ) : (
            <div className="w-[60px] h-[30px] relative">
              <div className="absolute inset-0 flex items-end justify-center">
                <span className="flex w-[60px] h-[71px] items-center justify-center rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white text-sidebar-foreground dark:border-sidebar-border dark:bg-sidebar">
                  <IconAdd className="size-[20px]" />
                </span>
              </div>
            </div>
          )}
          <span className="flex min-w-0 flex-1 flex-col gap-[4px] group-data-[collapsible=icon]:hidden">
            <span className="text-[10px] leading-none text-[#757f9c] dark:text-sidebar-foreground">{caption}</span>
            <span className="block truncate text-[12px] font-medium leading-none text-[#464c5e] dark:text-sidebar-accent-foreground">
              {nameLabel}
            </span>
          </span>
          <IconChevronDown className="size-[14px] shrink-0 text-sidebar-foreground group-data-[collapsible=icon]:hidden" />
        </div>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-(--radix-dropdown-menu-trigger-width)">
        {scopeAgents.map((agent) => (
          <DropdownMenuItem
            key={agent.id}
            data-active={agent.id === selectedAgentId}
            onSelect={() => onSelectAgent(agent.id)}
            className="gap-2 rounded-[14px] cursor-pointer focus:bg-[#F6F6F6] focus:[&_strong]:text-foreground! focus:[&_small]:text-muted-foreground!"
          >
            <EmployeeAvatar agent={agent} size={28} />
            <span className="flex min-w-0 flex-col">
              <strong className="truncate text-[12px] font-medium">
                {agent.is_overall ? '开放广场' : employeeDisplayName(agent)}
              </strong>
              <small className="truncate text-[10px] text-muted-foreground">
                {agent.is_overall ? '平台' : employeeProfile(agent).roleName}
              </small>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function SidebarFooterActions({ onOpenChat }: { onOpenChat: () => void }) {
  return (
    <div
      className={cn(
        'flex items-center justify-center gap-[10px]',
        'group-data-[collapsible=icon]:flex-col',
      )}
    >
      <button
        type="button"
        onClick={onOpenChat}
        title="对话端"
        className={cn(
          'flex h-[40px] w-[130px] items-center justify-center gap-[6px] rounded-[10px] border-[0.5px] border-[#E3E7F1] bg-[#F6F6F6] px-[20px] py-[4px] text-[14px] text-sidebar-accent-foreground transition-opacity hover:opacity-70 dark:border-sidebar-border dark:bg-sidebar-accent',
          'group-data-[collapsible=icon]:size-[40px] group-data-[collapsible=icon]:w-[40px] group-data-[collapsible=icon]:px-0',
        )}
      >
        <IconChat className="size-[16px]!" />
        <span className="group-data-[collapsible=icon]:hidden">对话端</span>
      </button>
      <button
        type="button"
        onClick={onOpenChat}
        title="切换到对话端"
        aria-label="切换到对话端"
        className="flex size-[32px] shrink-0 items-center justify-center rounded-[8px] rotate-90 text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
      >
        <IconToggle className="size-[16px]!" />
      </button>
    </div>
  );
}

function CollapsedGroupLabel({ children }: { children: string }) {
  return (
    <span className="text-[10px] leading-none text-[#464c5e] dark:text-sidebar-foreground">
      {children}
    </span>
  );
}

function CollapsedNavButton({
  item,
  selected,
  onNavigate,
  radius,
  iconSize,
}: {
  item: NavItem;
  selected: string;
  onNavigate: (route: string) => void;
  radius: number;
  iconSize: number;
}) {
  const active = selected === item.route;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={item.label}
          onClick={() => onNavigate(item.route)}
          className={cn(
            'flex size-[32px] shrink-0 items-center justify-center text-sidebar-foreground transition-colors',
            'hover:bg-sidebar-accent hover:text-sidebar-accent-foreground',
            active && 'bg-sidebar-accent text-sidebar-accent-foreground',
          )}
          style={{ borderRadius: radius }}
        >
          <item.Icon style={{ width: iconSize, height: iconSize }} />
        </button>
      </TooltipTrigger>
      <TooltipContent side="right" align="center">
        {item.label}
      </TooltipContent>
    </Tooltip>
  );
}

function CollapsedAgentSwitcher({
  sidebarAgent,
  scopeAgents,
  selectedAgentId,
  onSelectAgent,
  nameLabel,
}: Pick<AppSidebarManagementProps, 'sidebarAgent' | 'scopeAgents' | 'selectedAgentId' | 'onSelectAgent'> & {
  nameLabel: string;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="切换当前员工"
          className="flex flex-col items-center gap-[2px] pt-[8px]"
        >
          {sidebarAgent ? (
            <EmployeeAvatar agent={sidebarAgent} width={32} height={38} radius={8} />
          ) : (
            <span className="flex h-[38px] w-[32px] items-center justify-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-sidebar-foreground dark:border-sidebar-border dark:bg-sidebar">
              <IconAdd className="size-[16px]" />
            </span>
          )}
          <span className="w-[34px] text-center text-[10px] font-medium leading-tight wrap-break-word text-[#18181a] dark:text-sidebar-accent-foreground">
            {nameLabel}
          </span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[180px]">
        {scopeAgents.map((agent) => (
          <DropdownMenuItem
            key={agent.id}
            data-active={agent.id === selectedAgentId}
            onSelect={() => onSelectAgent(agent.id)}
            className="gap-2 rounded-[14px] cursor-pointer focus:bg-[#F6F6F6] focus:[&_strong]:text-foreground! focus:[&_small]:text-muted-foreground!"
          >
            <EmployeeAvatar agent={agent} size={28} />
            <span className="flex min-w-0 flex-col">
              <strong className="truncate text-[12px] font-medium">
                {agent.is_overall ? '开放广场' : employeeDisplayName(agent)}
              </strong>
              <small className="truncate text-[10px] text-muted-foreground">
                {agent.is_overall ? '平台' : employeeProfile(agent).roleName}
              </small>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function CollapsedSidebar({
  selected,
  onNavigate,
  isAdmin,
  sidebarAgent,
  scopeAgents,
  selectedAgentId,
  onSelectAgent,
  onOpenChat,
  onToggle,
}: Pick<
  AppSidebarManagementProps,
  'selected' | 'onNavigate' | 'isAdmin' | 'sidebarAgent' | 'scopeAgents' | 'selectedAgentId' | 'onSelectAgent' | 'onOpenChat'
> & { onToggle: () => void }) {
  const nameLabel = sidebarAgent
    ? sidebarAgent.is_overall
      ? '开放广场'
      : employeeDisplayName(sidebarAgent)
    : '未选择';
  const primaryItems = primaryNavItems(isAdmin);

  return (
    <div className="flex h-full w-(--sidebar-width-icon) shrink-0 flex-col items-center gap-[32px] px-[16px] py-[10px]">
      <div className="flex w-full flex-col items-center gap-[10px]">
        <button type="button" title="开放广场" className="flex items-center justify-center p-[10px]">
          <BrandLogo markOnly />
        </button>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={onToggle}
              aria-label="展开边栏"
              className="flex size-[16px] items-center justify-center text-sidebar-foreground transition-colors hover:text-sidebar-accent-foreground"
            >
              <IconHeaderCollapse className="size-[16px]! -rotate-90" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" align="center">
            展开边栏
          </TooltipContent>
        </Tooltip>
      </div>

      <div className="flex w-full flex-col items-center gap-[12px]">
        {primaryItems.map((item) => (
          <CollapsedNavButton
            key={item.route}
            item={item}
            selected={selected}
            onNavigate={onNavigate}
            radius={10}
            iconSize={16}
          />
        ))}
        <div className="h-px w-full bg-sidebar-border" />
      </div>

      <div className="flex min-h-0 w-full flex-1 flex-col items-center justify-between">
        <div className="flex w-[38px] flex-col items-center gap-[8px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[2px] pt-[6px] pb-[8px] dark:border-sidebar-border dark:bg-sidebar">
          <CollapsedAgentSwitcher
            sidebarAgent={sidebarAgent}
            scopeAgents={scopeAgents}
            selectedAgentId={selectedAgentId}
            onSelectAgent={onSelectAgent}
            nameLabel={nameLabel}
          />

          <div className="h-px w-[28px] bg-sidebar-border" />

          <div className="flex flex-col items-center gap-[4px]">
            <CollapsedGroupLabel>资料</CollapsedGroupLabel>
            {PROFILE_NAV.map((item) => (
              <CollapsedNavButton
                key={item.route}
                item={item}
                selected={selected}
                onNavigate={onNavigate}
                radius={10}
                iconSize={14}
              />
            ))}

            <CollapsedGroupLabel>能力</CollapsedGroupLabel>
            {CAPABILITY_NAV.map((item) => (
              <CollapsedNavButton
                key={item.route}
                item={item}
                selected={selected}
                onNavigate={onNavigate}
                radius={10}
                iconSize={14}
              />
            ))}
          </div>
        </div>

        <div className="flex items-center justify-center pb-[20px]">
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={onOpenChat}
                aria-label="切换到对话端"
                className="flex size-[32px] shrink-0 items-center justify-center rounded-[10px] border-[0.5px] border-[#E3E7F1] bg-[#F6F6F6] text-sidebar-accent-foreground transition-opacity hover:opacity-70 dark:border-sidebar-border dark:bg-sidebar-accent"
              >
                <IconChat className="size-[16px]!" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="right" align="center">
              切换到对话端
            </TooltipContent>
          </Tooltip>
        </div>
      </div>
    </div>
  );
}

function ManagementSidebar({
  selected,
  onNavigate,
  isAdmin,
  sidebarAgent,
  scopeAgents,
  selectedAgentId,
  onSelectAgent,
  onOpenChat,
}: AppSidebarManagementProps) {
  const { toggleSidebar, state } = useSidebar();
  const brandCollapsed = useMemo(() => state === 'collapsed', [state]);
  const primaryItems = useMemo(() => primaryNavItems(isAdmin), [isAdmin]);

  if (brandCollapsed) {
    return (
      <Sidebar collapsible="icon" className={SIDEBAR_SHELL_CLASS}>
        <CollapsedSidebar
          selected={selected}
          onNavigate={onNavigate}
          isAdmin={isAdmin}
          sidebarAgent={sidebarAgent}
          scopeAgents={scopeAgents}
          selectedAgentId={selectedAgentId}
          onSelectAgent={onSelectAgent}
          onOpenChat={onOpenChat}
          onToggle={toggleSidebar}
        />
      </Sidebar>
    );
  }

  return (
    <Sidebar collapsible="icon" className={SIDEBAR_SHELL_CLASS}>
      <div className="flex h-full w-(--sidebar-width) shrink-0 flex-col">
      <SidebarHeader className="gap-[24px] px-[20px] pt-[10px] group-data-[collapsible=icon]:px-[20px]">
        <div className="flex items-center justify-between">
          <button type="button" title="开放广场">
            <BrandLogo wordmarkClassName="group-data-[collapsible=icon]:hidden" />
          </button>
          {!brandCollapsed && (
            <button
              type="button"
              onClick={toggleSidebar}
              title="收起边栏"
              aria-label="收起边栏"
              className="flex size-[28px] shrink-0 items-center justify-center rounded-[8px] text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            >
              <IconHeaderCollapse className="size-[14px]! -rotate-90" />
            </button>
          )}
        </div>

        <div className="flex flex-col gap-[18px]">
          <SidebarMenu className="gap-[10px]">
            {primaryItems.map((item) => (
              <PrimaryNavButton key={item.route} item={item} selected={selected} onNavigate={onNavigate} />
            ))}
          </SidebarMenu>
          <div className="h-px w-full bg-sidebar-border group-data-[collapsible=icon]:hidden" />
        </div>
      </SidebarHeader>

      <SidebarContent className="px-[20px] group-data-[collapsible=icon]:px-[20px]">
        <div
          className={cn(
            'mt-[36px] mb-[24px] flex flex-col gap-[8px] rounded-[20px] border-[0.5px] border-[#e3e7f1] bg-sidebar px-[4px] pt-[6px] pb-[8px] dark:border-sidebar-border',
            'group-data-[collapsible=icon]:mt-[24px] group-data-[collapsible=icon]:border-transparent group-data-[collapsible=icon]:bg-transparent group-data-[collapsible=icon]:p-0 group-data-[collapsible=icon]:shadow-none',
          )}
        >
          <AgentSwitcher
            sidebarAgent={sidebarAgent}
            scopeAgents={scopeAgents}
            selectedAgentId={selectedAgentId}
            onSelectAgent={onSelectAgent}
          />
          <div className="mx-[8px] h-px bg-sidebar-border group-data-[collapsible=icon]:hidden" />

          <div className="flex flex-col gap-[2px] px-[10px] group-data-[collapsible=icon]:px-0">
            <GroupLabel>基本资料</GroupLabel>
            <SidebarMenu className="gap-[2px]">
              {PROFILE_NAV.map((item) => (
                <CardNavButton key={item.route} item={item} selected={selected} onNavigate={onNavigate} />
              ))}
            </SidebarMenu>

            <GroupLabel>员工能力</GroupLabel>
            <SidebarMenu className="gap-[2px]">
              {CAPABILITY_NAV.map((item) => (
                <CardNavButton key={item.route} item={item} selected={selected} onNavigate={onNavigate} />
              ))}
            </SidebarMenu>
          </div>
        </div>
      </SidebarContent>

      <SidebarFooter className="px-[20px] pb-[20px] group-data-[collapsible=icon]:px-[20px]">
        <SidebarFooterActions onOpenChat={onOpenChat} />
      </SidebarFooter>
      </div>
    </Sidebar>
  );
}

// ---------------------------------------------------------------------------
// Chat variant (Figma node 38:5767) — reuses the sidebar shell + brand chrome
// while swapping the body for the "员工会话" session list.
// ---------------------------------------------------------------------------

function sessionAgentFor(session: ChatSession, agents: AgentProfileRead[]): AgentProfileRead | null {
  if (!session.agent_id) return null;
  return agents.find((agent) => agent.id === session.agent_id) || null;
}

function sessionTitleFor(session: ChatSession, _agent: AgentProfileRead | null): string {
  if (session.title) return staffdeckDisplayText(session.title);
  return session.id || '新对话';
}

function sessionSubtitleFor(session: ChatSession, _agent: AgentProfileRead | null): string {
  const recent = (session.last_agent_question || session.summary || '').replace(/^最近回复[:：]\s*/, '');
  return recent ? staffdeckDisplayText(recent) : '新对话';
}

function ChatSessionFilter({
  sessionFilter,
  sessionFilterOptions,
  onSessionFilterChange,
  collapsed = false,
}: Pick<AppSidebarChatProps, 'sessionFilter' | 'sessionFilterOptions' | 'onSessionFilterChange'> & {
  collapsed?: boolean;
}) {
  const current = sessionFilterOptions.find((option) => option.value === sessionFilter) || sessionFilterOptions[0];
  const [namePart, countPart] = current
    ? current.label.split('·').map((part) => part.trim())
    : ['全部员工', ''];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {collapsed ? (
          <button
            type="button"
            aria-label="筛选会话"
            className="flex h-[32px] w-full items-center justify-center rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] transition-colors hover:border-[#c9d2e4] dark:border-sidebar-border dark:bg-sidebar-accent"
          >
            <IconSort className="size-[14px]! shrink-0 text-[#858b9c]" />
          </button>
        ) : (
          <button
            type="button"
            aria-label="筛选会话"
            className="flex h-[40px] w-full items-center justify-between rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] px-[20px] py-[10px] text-left transition-colors hover:border-[#c9d2e4] dark:border-sidebar-border dark:bg-sidebar-accent"
          >
            <span className="flex min-w-0 items-center gap-[6px]">
              <span className="truncate text-[14px] text-[#464c5e] dark:text-sidebar-accent-foreground">{namePart}</span>
              {countPart && (
                <span className="inline-flex h-[18px] min-w-[30px] items-center justify-center rounded-full bg-white px-[4px] text-[12px] text-[#757f9c] dark:bg-sidebar dark:text-sidebar-foreground">
                  {countPart}
                </span>
              )}
            </span>
            <IconSort className="size-[14px]! shrink-0 text-[#858b9c]" />
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align={collapsed ? 'center' : 'start'}
        side={collapsed ? 'right' : 'bottom'}
        className={cn(
          'flex flex-col gap-[6px] rounded-[14px] bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 dark:bg-sidebar dark:ring-1 dark:ring-sidebar-border',
          collapsed ? 'min-w-[160px]' : 'w-(--radix-dropdown-menu-trigger-width)',
        )}
      >
        {sessionFilterOptions.map((option) => {
          const [optionName, optionCount] = option.label.split('·').map((part) => part.trim());
          const active = option.value === sessionFilter;
          return (
            <DropdownMenuItem
              key={option.value}
              data-active={active}
              onSelect={() => onSessionFilterChange(option.value)}
              className={cn(
                'group/filter flex h-[32px] cursor-pointer items-center gap-[4px] rounded-[14px] px-[12px] py-[4px] transition-colors focus:bg-[#f6f6f6] dark:focus:bg-sidebar-accent',
                active ? 'bg-[#f6f6f6] dark:bg-sidebar-accent' : 'bg-transparent',
              )}
            >
              <span
                className={cn(
                  'truncate text-[12px] leading-none',
                  active
                    ? 'text-[#18181a]! dark:text-sidebar-accent-foreground!'
                    : 'text-[#858b9c]! dark:text-sidebar-foreground!',
                )}
              >
                {optionName}
              </span>
              {optionCount && (
                <span
                  className={cn(
                    'inline-flex h-[14px] items-center justify-center rounded-full px-[8px] text-[10px] leading-none text-[#757f9c]! capitalize',
                    active
                      ? 'bg-white dark:bg-sidebar'
                      : 'bg-[#f6f6f6] group-focus/filter:bg-white dark:bg-sidebar-accent',
                  )}
                >
                  {optionCount}
                </span>
              )}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ChatSessionRow({
  session,
  agent,
  active,
  unread,
  onOpenSession,
  onRenameSession,
  onDeleteSession,
}: {
  session: ChatSession;
  agent: AgentProfileRead | null;
  active: boolean;
  unread: boolean;
  onOpenSession: (id: string) => void;
  onRenameSession: (session: ChatSession) => void;
  onDeleteSession: (session: ChatSession) => void;
}) {
  const title = sessionTitleFor(session, agent);
  const subtitle = sessionSubtitleFor(session, agent);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpenSession(session.id)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpenSession(session.id);
        }
      }}
      className={cn(
        'group/session relative flex w-full cursor-pointer items-center gap-[6px] rounded-[14px] py-[6px] pl-[8px] pr-[12px] text-left transition-colors',
        active
          ? 'border-[0.5px] border-[#e3e7f1] bg-white shadow-[0px_0px_5px_rgba(0,0,0,0.05)] dark:border-sidebar-border dark:bg-sidebar-accent'
          : 'border-[0.5px] border-transparent hover:bg-[#f4f5f7] dark:hover:bg-sidebar-accent',
      )}
    >
      <span className="inline-grid size-[42px] shrink-0 place-items-center overflow-hidden rounded-[12px] bg-[#f1f2f5] text-[#464c5e] dark:bg-sidebar">
        {agent ? (
          <EmployeeAvatar agent={agent} size={42} radius={12} />
        ) : (
          <IconChatBubble className="size-[20px]!" />
        )}
      </span>
      <span className="flex min-w-0 flex-1 flex-col justify-between self-stretch py-[3px]">
        <span className="truncate text-[14px] leading-none text-[#464c5e] capitalize dark:text-sidebar-accent-foreground" title={title}>
          {title}
        </span>
        <span className="truncate text-[12px] leading-none text-[#757f9c] dark:text-sidebar-foreground" title={subtitle}>
          {subtitle}
        </span>
      </span>
      {unread && (
        <span className="ml-[2px] size-[7px] shrink-0 rounded-full bg-[#f5483b]" aria-label="未读回复" />
      )}
      <span className="ml-auto hidden shrink-0 items-center gap-[6px] group-hover/session:flex">
        <button
          type="button"
          aria-label="重命名会话"
          onClick={(event) => {
            event.stopPropagation();
            onRenameSession(session);
          }}
          className="inline-grid size-[24px] place-items-center rounded-[10px] text-[#858b9c] transition-colors hover:bg-[#e3e7f1] hover:text-[#18181a] dark:hover:bg-sidebar dark:hover:text-sidebar-accent-foreground"
        >
          <IconEdit className="size-[14px]!" />
        </button>
        <button
          type="button"
          aria-label="删除会话"
          onClick={(event) => {
            event.stopPropagation();
            onDeleteSession(session);
          }}
          className="inline-grid size-[24px] place-items-center rounded-[10px] text-[#858b9c] transition-colors hover:bg-[#fce7e7] hover:text-[#f5483b]"
        >
          <IconTrash className="size-[14px]!" />
        </button>
      </span>
    </div>
  );
}

function ChatSessionRowSkeleton() {
  return (
    <div className="flex w-full animate-pulse items-center gap-[6px] rounded-[14px] border-[0.5px] border-transparent px-[8px] py-[6px]">
      <span className="size-[42px] shrink-0 rounded-[12px] bg-[#eef0f4] dark:bg-sidebar-accent" />
      <span className="flex min-w-0 flex-1 flex-col gap-[6px] pb-[2px]">
        <span className="h-[12px] w-[60%] rounded-full bg-[#eef0f4] dark:bg-sidebar-accent" />
        <span className="h-[10px] w-[40%] rounded-full bg-[#f1f2f5] dark:bg-sidebar" />
      </span>
    </div>
  );
}

function ChatSessionSkeletonList({ rows = 5 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-[2px]">
      {Array.from({ length: rows }).map((_, index) => (
        <ChatSessionRowSkeleton key={index} />
      ))}
    </div>
  );
}

function ChatFooterActions({ onOpenAdmin }: { onOpenAdmin: () => void }) {
  return (
    <div className="flex items-center justify-center gap-[10px] pb-[20px]">
      <button
        type="button"
        onClick={onOpenAdmin}
        title="管理端"
        className="flex h-[40px] w-[130px] items-center justify-center gap-[6px] rounded-[10px] border-[0.5px] border-[#E3E7F1] bg-[#F6F6F6] px-[20px] py-[4px] text-[14px] text-[#858b9c] transition-opacity hover:opacity-70 dark:border-sidebar-border dark:bg-sidebar-accent"
      >
        <IconViewMasonry className="size-[16px]!" />
        <span>管理端</span>
      </button>
      <button
        type="button"
        onClick={onOpenAdmin}
        title="切换到管理端"
        aria-label="切换到管理端"
        className="flex size-[32px] shrink-0 items-center justify-center rounded-[8px] rotate-90 text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
      >
        <IconToggle className="size-[16px]!" />
      </button>
    </div>
  );
}

function CollapsedChatSidebar({
  sessions,
  sessionsLoading = false,
  agents,
  activeSessionId,
  sessionFilter,
  onSessionFilterChange,
  sessionFilterOptions,
  isSessionUnread,
  onOpenSession,
  onOpenGallery,
  onOpenAdmin,
  onToggle,
}: Pick<
  AppSidebarChatProps,
  'sessions' | 'sessionsLoading' | 'agents' | 'activeSessionId' | 'sessionFilter' | 'onSessionFilterChange' | 'sessionFilterOptions' | 'isSessionUnread' | 'onOpenSession' | 'onOpenGallery' | 'onOpenAdmin'
> & { onToggle: () => void }) {
  return (
    <div className="flex h-full w-(--sidebar-width-icon) shrink-0 flex-col items-center gap-[32px] px-[20px] py-[10px]">
      <div className="flex w-full flex-col items-center gap-[10px]">
        <button type="button" title="数字员工广场" onClick={onOpenGallery} className="flex items-center justify-center p-[10px]">
          <BrandLogo markOnly />
        </button>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={onToggle}
              aria-label="展开边栏"
              className="flex size-[16px] items-center justify-center text-sidebar-foreground transition-colors hover:text-sidebar-accent-foreground"
            >
              <IconHeaderCollapse className="size-[16px]! -rotate-90" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" align="center">
            展开边栏
          </TooltipContent>
        </Tooltip>
      </div>

      <div className="flex min-h-0 w-full flex-1 flex-col items-center gap-[16px]">
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={onOpenGallery}
              aria-label="数字员工广场"
              className="flex h-[32px] w-full items-center justify-center rounded-[8px] text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            >
              <IconGlobe className="size-[16px]!" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" align="center">
            数字员工广场
          </TooltipContent>
        </Tooltip>

        <div className="h-px w-full bg-sidebar-border" />

        <ChatSessionFilter
          collapsed
          sessionFilter={sessionFilter}
          sessionFilterOptions={sessionFilterOptions}
          onSessionFilterChange={onSessionFilterChange}
        />

        <span className="text-[10px] leading-none text-[#464c5e] dark:text-sidebar-foreground">会话</span>

        <div className="no-scrollbar mx-[-8px] flex min-h-0 w-[calc(100%+16px)] flex-1 flex-col items-center gap-[10px] overflow-y-auto py-[2px]">
          {sessionsLoading
            ? Array.from({ length: 5 }).map((_, index) => (
                <span key={index} className="size-[36px] shrink-0 animate-pulse rounded-[10px] bg-[#eef0f4] dark:bg-sidebar-accent" />
              ))
            : sessions.map((session) => {
                const agent = sessionAgentFor(session, agents);
                const active = session.id === activeSessionId;
                const unread = isSessionUnread(session);
                const title = sessionTitleFor(session, agent);
                return (
                  <Tooltip key={session.id}>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        onClick={() => onOpenSession(session.id)}
                        aria-label={title}
                        className={cn(
                          'relative flex shrink-0 items-center justify-center overflow-hidden transition-shadow',
                          active
                            ? 'size-[44px] rounded-[14px] border-[0.5px] border-[#464c5e] bg-white shadow-[0px_0px_5px_rgba(0,0,0,0.1)] dark:border-sidebar-accent-foreground dark:bg-sidebar-accent'
                            : 'size-[36px] rounded-[10px] bg-[#D8D8D8] text-[#464c5e] dark:bg-sidebar',
                        )}
                      >
                        {agent ? (
                          <EmployeeAvatar agent={agent} size={active ? 34 : 36} radius={10} />
                        ) : (
                          <IconChatBubble className="size-[18px]!" />
                        )}
                        {unread && (
                          <span className="absolute right-[2px] top-[2px] size-[7px] rounded-full bg-[#f5483b] ring-[1.5px] ring-white" aria-label="未读回复" />
                        )}
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="right" align="center">
                      {title}
                    </TooltipContent>
                  </Tooltip>
                );
              })}
        </div>
      </div>

      <div className="flex items-center justify-center pb-[20px]">
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={onOpenAdmin}
              aria-label="切换到管理端"
              className="flex size-[32px] shrink-0 items-center justify-center rounded-[10px] border-[0.5px] border-[#E3E7F1] bg-[#F6F6F6] text-[#858b9c] transition-opacity hover:opacity-70 dark:border-sidebar-border dark:bg-sidebar-accent"
            >
              <IconViewMasonry className="size-[16px]!" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="right" align="center">
            切换到管理端
          </TooltipContent>
        </Tooltip>
      </div>
    </div>
  );
}

function ChatSidebarVariant({
  sessions,
  sessionsLoading = false,
  agents,
  activeSessionId,
  sessionFilter,
  onSessionFilterChange,
  sessionFilterOptions,
  isSessionUnread,
  onOpenSession,
  onOpenGallery,
  onRenameSession,
  onDeleteSession,
  onOpenAdmin,
}: AppSidebarChatProps) {
  const { toggleSidebar, state } = useSidebar();
  const collapsed = state === 'collapsed';
  const showSkeleton = sessionsLoading && sessions.length === 0;

  if (collapsed) {
    return (
      <Sidebar collapsible="icon" className={SIDEBAR_SHELL_CLASS}>
        <CollapsedChatSidebar
          sessions={sessions}
          sessionsLoading={showSkeleton}
          agents={agents}
          activeSessionId={activeSessionId}
          sessionFilter={sessionFilter}
          onSessionFilterChange={onSessionFilterChange}
          sessionFilterOptions={sessionFilterOptions}
          isSessionUnread={isSessionUnread}
          onOpenSession={onOpenSession}
          onOpenGallery={onOpenGallery}
          onOpenAdmin={onOpenAdmin}
          onToggle={toggleSidebar}
        />
      </Sidebar>
    );
  }

  return (
    <Sidebar collapsible="icon" className={SIDEBAR_SHELL_CLASS}>
      <div className="flex h-full w-(--sidebar-width) shrink-0 flex-col">
        <SidebarHeader className="gap-[24px] px-[20px] pt-[10px]">
          <div className="flex items-center justify-between">
            <button type="button" title="数字员工广场" onClick={onOpenGallery}>
              <BrandLogo />
            </button>
            <button
              type="button"
              onClick={toggleSidebar}
              title="收起边栏"
              aria-label="收起边栏"
              className="flex size-[28px] shrink-0 items-center justify-center rounded-[8px] text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            >
              <IconHeaderCollapse className="size-[14px]! -rotate-90" />
            </button>
          </div>

          <div className="flex flex-col gap-[16px]">
            <button
              type="button"
              onClick={onOpenGallery}
              className="flex items-center gap-[12px] rounded-[8px] px-[20px] py-[10px] text-left text-[14px] text-[#858b9c] transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
            >
              <IconGlobe className="size-[16px]! shrink-0" />
              <span className="truncate">数字员工广场</span>
            </button>
            <div className="h-px w-full bg-sidebar-border" />
            <ChatSessionFilter
              sessionFilter={sessionFilter}
              sessionFilterOptions={sessionFilterOptions}
              onSessionFilterChange={onSessionFilterChange}
            />
            <span className="text-[12px] leading-none text-[#858b9c] dark:text-sidebar-foreground">员工会话</span>
          </div>
        </SidebarHeader>

        <SidebarContent className="px-[20px]">
          <div className="flex flex-col gap-[2px] pb-[10px]">
            {showSkeleton ? (
              <ChatSessionSkeletonList />
            ) : sessions.length === 0 ? (
              <div className="flex flex-col items-center gap-[8px] py-[28px] text-center text-[12px] text-[#a2a8b8]">
                <StaffdeckIcon name="inbox" size={22} />
                <span>暂无历史会话</span>
              </div>
            ) : (
              <div className="flex flex-col gap-[2px]">
                {sessions.map((session) => (
                  <ChatSessionRow
                    key={session.id}
                    session={session}
                    agent={sessionAgentFor(session, agents)}
                    active={session.id === activeSessionId}
                    unread={isSessionUnread(session)}
                    onOpenSession={onOpenSession}
                    onRenameSession={onRenameSession}
                    onDeleteSession={onDeleteSession}
                  />
                ))}
              </div>
            )}
          </div>
        </SidebarContent>

        <SidebarFooter className="px-[20px]">
          <ChatFooterActions onOpenAdmin={onOpenAdmin} />
        </SidebarFooter>
      </div>
    </Sidebar>
  );
}

export default function AppSidebar(props: AppSidebarProps) {
  if (props.variant === 'chat') {
    return <ChatSidebarVariant {...props} />;
  }
  return <ManagementSidebar {...props} />;
}
