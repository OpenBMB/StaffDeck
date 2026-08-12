import EmployeeAvatar from '@/components/EmployeeAvatar';
import { teamLeader } from '@/components/TeamCard';
import { employeeDisplayName } from '@/employee';

import {
  CHAT_EMPTY_CARD_CLASS,
  CHAT_EMPTY_CLASS,
  CHAT_EMPTY_GREETING_CARD_CLASS,
  CHAT_EMPTY_ROLE_CLASS,
  CHAT_EMPTY_STAT_CELL_CLASS,
  CHAT_EMPTY_SUBTITLE_CLASS,
  CHAT_EMPTY_TAGS_CLASS,
  CHAT_EMPTY_TITLE_CLASS,
} from '../chatPageStyles';
import type { UseChatSession } from '../useChatSession';

function greetingFontSize(displayName: string): number {
  const length = Array.from(displayName).length;
  return length > 20 ? 20 : length > 12 ? 24 : length > 6 ? 30 : 36;
}

export default function ChatEmptyState({ chat }: { chat: UseChatSession }) {
  if (chat.currentSession?.team_id) {
    return <TeamEmptyCard chat={chat} />;
  }
  return <EmployeeEmptyCard chat={chat} />;
}

function EmployeeEmptyCard({ chat }: { chat: UseChatSession }) {
  const { displayedAgent, displayedProfile, emptyRoleSummary, emptyProfileTags, emptyStats } = chat;
  const displayName = displayedAgent ? employeeDisplayName(displayedAgent) : '';

  return (
    <div className={CHAT_EMPTY_CLASS}>
      <div className={CHAT_EMPTY_GREETING_CARD_CLASS}>
        <div className="flex min-h-[102px] w-full gap-[10px]">
          <div className="relative h-[102px] w-[136px] shrink-0 self-end">
            <div className="absolute bottom-0 left-0 h-[160px] w-[136px]">
            <EmployeeAvatar
              profile={displayedProfile ?? undefined}
              agent={displayedAgent ?? undefined}
              width={136}
              height={160}
              radius={0}
              fit="cover"
              objectPosition="bottom"
              className="bg-transparent!"
            />
            </div>
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] py-[12px] capitalize">
            <strong
              className={`${CHAT_EMPTY_TITLE_CLASS} max-w-full [overflow-wrap:anywhere]`}
              style={{ fontSize: `${greetingFontSize(displayName)}px` }}
              title={displayName}
            >
              Hello 我是{displayName}！
            </strong>
            <span className={CHAT_EMPTY_SUBTITLE_CLASS}>我们来做什么？</span>
          </div>
        </div>
      </div>

      <div className={CHAT_EMPTY_CARD_CLASS}>
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] px-[4px]">
          <p className={CHAT_EMPTY_ROLE_CLASS}>{emptyRoleSummary}</p>
          <div className={CHAT_EMPTY_TAGS_CLASS}>
            {emptyProfileTags.map((tag, index) => (
              <span key={`${tag}-${index}`}>{tag}</span>
            ))}
          </div>
        </div>
        <div className="flex flex-1 items-stretch">
          {emptyStats.map((item) => (
            <div key={item.label} className={CHAT_EMPTY_STAT_CELL_CLASS}>
              <span className="text-[18px] font-medium leading-none">{item.value}</span>
              <span className="text-[10px] leading-none">{item.label}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function TeamEmptyCard({ chat }: { chat: UseChatSession }) {
  const { displayedTeam, currentSession, agents, teamEmptyStats } = chat;
  const members = displayedTeam?.members || [];
  const leader = displayedTeam ? teamLeader(displayedTeam) : null;
  const teamName = displayedTeam?.name || currentSession?.team_name || '';
  const agentById = (agentId: string) => agents.find((agent) => agent.id === agentId) || null;
  const summary = displayedTeam?.description?.trim()
    || `团队由 ${members.length} 名成员组成，TL 是 ${leader?.agent_name || '未设置'}`;
  const memberTags = members.slice(0, 5).map((member) => (
    member.role === 'leader'
      ? `${member.agent_name || '未设置'} · TL`
      : member.agent_name || '未设置'
  ));
  const stats = [
    { label: '成员数', value: members.length },
    { label: '任务数', value: teamEmptyStats.tasks },
    { label: '黑板条目数', value: teamEmptyStats.blackboard },
  ];

  return (
    <div className={CHAT_EMPTY_CLASS}>
      <div className={CHAT_EMPTY_GREETING_CARD_CLASS}>
        <div className="flex min-h-[102px] w-full gap-[10px]">
          <div className="relative h-[102px] w-[136px] shrink-0 self-end">
            <div className="absolute inset-x-0 bottom-[14px] flex items-center justify-center">
              {members.slice(0, 3).map((member) => (
                <EmployeeAvatar
                  key={member.id}
                  agent={agentById(member.agent_id)}
                  size={56}
                  className="-ml-[14px] shrink-0 rounded-full ring-4 ring-[#f6f6f6] first:ml-0"
                />
              ))}
            </div>
          </div>
          <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] py-[12px] capitalize">
            <strong
              className={`${CHAT_EMPTY_TITLE_CLASS} max-w-full [overflow-wrap:anywhere]`}
              style={{ fontSize: `${greetingFontSize(teamName)}px` }}
              title={teamName}
            >
              Hello 我们是{teamName}！
            </strong>
            <span className={CHAT_EMPTY_SUBTITLE_CLASS}>我们来做什么？</span>
          </div>
        </div>
      </div>

      <div className={CHAT_EMPTY_CARD_CLASS}>
        <div className="flex min-w-0 flex-1 flex-col justify-center gap-[8px] px-[4px]">
          <p className={CHAT_EMPTY_ROLE_CLASS}>{summary}</p>
          <div className={CHAT_EMPTY_TAGS_CLASS}>
            {memberTags.map((tag, index) => (
              <span key={`${tag}-${index}`}>{tag}</span>
            ))}
          </div>
        </div>
        <div className="flex flex-1 items-stretch">
          {stats.map((item) => (
            <div key={item.label} className={CHAT_EMPTY_STAT_CELL_CLASS}>
              <span className="text-[18px] font-medium leading-none">{item.value}</span>
              <span className="text-[10px] leading-none">{item.label}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
