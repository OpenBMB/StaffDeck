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
import { employeeDisplayName, employeeProfile } from '../employee';
import { EnterpriseRoute } from '../enums/routes';
import type { AgentProfileRead } from '../types';
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

export type AppSidebarProps = {
  selected: string;
  onNavigate: (route: string) => void;
  isAdmin: boolean;
  sidebarAgent?: AgentProfileRead;
  scopeAgents: AgentProfileRead[];
  selectedAgentId: string;
  onSelectAgent: (agentId: string) => void;
  onOpenChat: () => void;
};

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
}: Pick<AppSidebarProps, 'sidebarAgent' | 'scopeAgents' | 'selectedAgentId' | 'onSelectAgent'>) {
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
}: Pick<AppSidebarProps, 'sidebarAgent' | 'scopeAgents' | 'selectedAgentId' | 'onSelectAgent'> & {
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
  AppSidebarProps,
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

      <div className="flex w-full flex-col items-center gap-[18px]">
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
                aria-label="对话端"
                className="flex size-[32px] shrink-0 items-center justify-center rounded-[10px] border-[0.5px] border-[#E3E7F1] bg-[#F6F6F6] text-sidebar-accent-foreground transition-opacity hover:opacity-70 dark:border-sidebar-border dark:bg-sidebar-accent"
              >
                <IconChat className="size-[16px]!" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="right" align="center">
              对话端
            </TooltipContent>
          </Tooltip>
        </div>
      </div>
    </div>
  );
}

export default function AppSidebar({
  selected,
  onNavigate,
  isAdmin,
  sidebarAgent,
  scopeAgents,
  selectedAgentId,
  onSelectAgent,
  onOpenChat,
}: AppSidebarProps) {
  const { toggleSidebar, state } = useSidebar();
  const brandCollapsed = useMemo(() => state === 'collapsed', [state]);
  const primaryItems = useMemo(() => primaryNavItems(isAdmin), [isAdmin]);

  if (brandCollapsed) {
    return (
      <Sidebar
        collapsible="icon"
        className="overflow-hidden border-r border-sidebar-border bg-sidebar backdrop-blur-[9.5px] **:data-[slot=sidebar-inner]:bg-sidebar"
      >
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
    <Sidebar
      collapsible="icon"
      className="overflow-hidden border-r border-sidebar-border bg-sidebar backdrop-blur-[9.5px] **:data-[slot=sidebar-inner]:bg-sidebar"
    >
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
