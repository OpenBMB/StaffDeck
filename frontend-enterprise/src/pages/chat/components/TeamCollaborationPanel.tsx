import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, LoaderCircle } from 'lucide-react';

import { api, TENANT_ID } from '@/api/client';
import EmployeeAvatar from '@/components/EmployeeAvatar';
import { staffdeckDisplayText } from '@/employee';
import { cn } from '@/lib/utils';
import type {
  AgentProfileRead,
  TeamConversationMessageRead,
  TeamConversationRead,
  TeamConversationsResponse,
  TeamRead,
} from '@/types';

function conversationTitle(conversation: TeamConversationRead): string {
  return staffdeckDisplayText(conversation.title)
    .replace(/^团队任务验收:/, '')
    .replace(/^团队竞标(?:打分|裁决)?:/, '')
    .replace(/^团队任务:/, '')
    .trim() || '团队任务';
}

export function collaborationQuestion(conversation: TeamConversationRead): string {
  const memberName = conversation.agent_name || '团队成员';
  const title = conversationTitle(conversation);
  return conversation.kind === 'member_bid'
    ? `@${memberName}，请参与「${title}」竞标`
    : `@${memberName}，请处理「${title}」`;
}

export default function TeamCollaborationPanel({
  team,
  agents,
}: {
  team: TeamRead;
  agents: AgentProfileRead[];
}) {
  const [conversations, setConversations] = useState<TeamConversationRead[]>([]);
  const [expandedSessionId, setExpandedSessionId] = useState('');
  const [messagesBySession, setMessagesBySession] = useState<Record<string, TeamConversationMessageRead[]>>({});
  const [loadingSessionId, setLoadingSessionId] = useState('');
  const agentById = useMemo(
    () => new Map(agents.map((agent) => [agent.id, agent])),
    [agents],
  );
  const leaderMember = team.members.find((member) => member.role === 'leader');
  const leaderAgent = leaderMember ? agentById.get(leaderMember.agent_id) : undefined;

  useEffect(() => {
    let cancelled = false;
    api.get<TeamConversationsResponse>(
      `/api/enterprise/teams/${team.id}/conversations?tenant_id=${TENANT_ID}`,
    )
      .then((response) => {
        if (cancelled) return;
        const seen = new Set<string>();
        setConversations(response.conversations.filter((conversation) => {
          if (conversation.kind !== 'member_task' && conversation.kind !== 'member_bid') return false;
          if (conversation.agent_id === leaderMember?.agent_id) return false;
          const key = `${conversation.agent_id || ''}:${conversation.kind}:${conversationTitle(conversation)}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        }).slice(0, 4));
      })
      .catch(() => {
        if (!cancelled) setConversations([]);
      });
    return () => {
      cancelled = true;
    };
  }, [leaderMember?.agent_id, team.id]);

  async function toggleReply(conversation: TeamConversationRead) {
    if (expandedSessionId === conversation.session_id) {
      setExpandedSessionId('');
      return;
    }
    setExpandedSessionId(conversation.session_id);
    if (messagesBySession[conversation.session_id]) return;
    setLoadingSessionId(conversation.session_id);
    try {
      const rows = await api.get<TeamConversationMessageRead[]>(
        `/api/enterprise/teams/${team.id}/conversations/${conversation.session_id}/messages?tenant_id=${TENANT_ID}`,
      );
      setMessagesBySession((current) => ({ ...current, [conversation.session_id]: rows }));
    } catch {
      setMessagesBySession((current) => ({ ...current, [conversation.session_id]: [] }));
    } finally {
      setLoadingSessionId('');
    }
  }

  if (conversations.length === 0) return null;

  return conversations.map((conversation, index) => {
    const memberAgent = conversation.agent_id
      ? agentById.get(conversation.agent_id)
      : undefined;
    const memberName = conversation.agent_name || '团队成员';
    const expanded = expandedSessionId === conversation.session_id;
    const loading = loadingSessionId === conversation.session_id;
    const memberReplies = (messagesBySession[conversation.session_id] || [])
      .filter((message) => message.role === 'assistant');
    const preview = staffdeckDisplayText(conversation.preview || '成员已回复');

    return (
      <div
        key={conversation.session_id}
        aria-label={`团队协作 ${memberName}`}
        className="relative flex min-w-0 flex-col gap-[10px]"
      >
        {index === 0 && (
          <div className="my-[2px] flex items-center gap-[10px] text-[10px] text-[#a7adbb]">
            <span className="h-px flex-1 bg-[#eef1f6]" />
            团队内部协作
            <span className="h-px flex-1 bg-[#eef1f6]" />
          </div>
        )}

        <div className="flex min-w-0 items-start gap-[10px]">
          <EmployeeAvatar agent={leaderAgent} size={36} radius={10} />
          <div className="flex min-w-0 max-w-[680px] flex-1 flex-col gap-[5px]">
            <div className="flex items-center gap-[6px] px-[2px]">
              <span className="text-[11px] font-medium text-[#757f9c]">
                {leaderMember?.agent_name || '项目领导'}
              </span>
              <span className="rounded-full bg-[#edf3ff] px-[6px] py-px text-[9px] font-medium text-[#1a71ff]">
                项目领导
              </span>
            </div>
            <div className="rounded-[14px] border border-[#d9e5ff] bg-[#f6f9ff] px-[14px] py-[10px] text-[13px] leading-[20px] text-[#18181a]">
              <span className="font-medium text-[#1a71ff]">{`@${memberName}`}</span>
              {conversation.kind === 'member_bid'
                ? `，请参与「${conversationTitle(conversation)}」竞标`
                : `，请处理「${conversationTitle(conversation)}」`}
            </div>
          </div>
        </div>

        <div className="ml-[18px] h-[8px] w-px bg-[#dbe3f1]" />

        <div className="flex min-w-0 items-start gap-[10px]">
          <EmployeeAvatar agent={memberAgent} size={36} radius={10} />
          <div className="flex min-w-0 max-w-[680px] flex-1 flex-col gap-[5px]">
            <span className="px-[2px] text-[11px] font-medium text-[#757f9c]">{memberName}</span>
            <button
              type="button"
              aria-label={`${expanded ? '收起' : '展开'}${memberName}的回复`}
              aria-expanded={expanded}
              onClick={() => void toggleReply(conversation)}
              className="group w-full rounded-[14px] border border-[#e3e7f1] bg-white px-[14px] py-[11px] text-left shadow-[0_1px_2px_rgba(24,24,26,0.03)] transition-colors hover:border-[#cfd6e3]"
            >
              <span className="flex items-center gap-[8px]">
                <span className="min-w-0 flex-1 truncate text-[12px] text-[#464c5e]">
                  {`${memberName}回复：${preview}`}
                </span>
                {loading ? (
                  <LoaderCircle className="size-[13px] shrink-0 animate-spin text-[#858b9c]" />
                ) : (
                  <ChevronDown className={cn(
                    'size-[14px] shrink-0 text-[#858b9c] transition-transform',
                    expanded && 'rotate-180',
                  )} />
                )}
              </span>
              {expanded && !loading && (
                <span className="mt-[10px] block border-t border-[#eef1f6] pt-[10px]">
                  {memberReplies.length > 0 ? memberReplies.map((message) => (
                    <span
                      key={message.id}
                      className="mb-[8px] block text-[13px] leading-[21px] whitespace-pre-wrap text-[#18181a] last:mb-0"
                      data-i18n-ignore
                    >
                      {staffdeckDisplayText(message.content)}
                    </span>
                  )) : (
                    <span className="block text-[12px] text-[#a7adbb]">暂无回复内容</span>
                  )}
                </span>
              )}
            </button>
          </div>
        </div>
      </div>
    );
  });
}
