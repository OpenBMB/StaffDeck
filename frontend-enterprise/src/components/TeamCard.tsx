import { Badge } from '@/components/ui';

import type { AgentProfileRead, TeamMemberRead, TeamRead } from '../types';
import EmployeeAvatar from './EmployeeAvatar';

export type TeamCardProps = {
  team: TeamRead;
  /** 画廊已加载的员工列表，用于把成员 agent_id 映射到头像。 */
  agents: AgentProfileRead[];
  busy?: boolean;
  onOpen: () => void;
};

export function teamLeader(team: TeamRead): TeamMemberRead | null {
  return (team.members || []).find((member) => member.role === 'leader') || null;
}

export default function TeamCard({ team, agents, busy = false, onOpen }: TeamCardProps) {
  const members = team.members || [];
  const leader = teamLeader(team);
  const stacked = members.slice(0, 3);
  const extraCount = members.length - stacked.length;
  const agentById = (agentId: string) => agents.find((agent) => agent.id === agentId) || null;

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={team.name}
      aria-busy={busy}
      onClick={() => {
        if (!busy) onOpen();
      }}
      onKeyDown={(event) => {
        if (!busy && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          onOpen();
        }
      }}
      className="flex cursor-pointer flex-col gap-[12px] rounded-[20px] border border-[#F6F6F6] bg-white p-[20px] transition-shadow hover:shadow-[0_16px_30px_0_rgba(0,0,0,0.10)]"
    >
      <div className="flex items-start justify-between gap-[8px]">
        <span className="min-w-0 truncate text-[16px] font-medium text-[#18181a]" title={team.name}>
          {team.name}
        </span>
        <Badge
          variant="secondary"
          className="shrink-0 rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]"
        >
          {`${members.length} 名成员`}
        </Badge>
      </div>
      <p className="line-clamp-2 min-h-[34px] text-[12px] leading-[17px] text-[#757f9c]">
        {team.description || '暂无描述'}
      </p>
      <div className="flex items-center justify-between gap-[8px]">
        <span className="flex min-w-0 items-center gap-[6px] text-[12px] text-[#757f9c]">
          {leader && (
            <EmployeeAvatar agent={agentById(leader.agent_id)} size={20} className="shrink-0" />
          )}
          <span className="truncate">{`项目领导：${leader?.agent_name || '未设置'}`}</span>
        </span>
        <span className="flex shrink-0 items-center">
          {stacked.map((member) => (
            <EmployeeAvatar
              key={member.id}
              agent={agentById(member.agent_id)}
              size={24}
              className="-ml-[6px] ring-2 ring-white first:ml-0"
            />
          ))}
          {extraCount > 0 && (
            <span className="-ml-[6px] grid size-[24px] place-items-center rounded-full bg-[#eef1f6] text-[10px] text-[#464c5e] ring-2 ring-white">
              {`+${extraCount}`}
            </span>
          )}
        </span>
      </div>
    </div>
  );
}
