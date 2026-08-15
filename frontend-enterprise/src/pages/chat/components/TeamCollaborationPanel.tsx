import { useEffect, useMemo, useState } from 'react';
import { ArrowRight, LoaderCircle, MessageSquareMore } from 'lucide-react';

import { api, TENANT_ID } from '@/api/client';
import EmployeeAvatar from '@/components/EmployeeAvatar';
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from '@/components/ui';
import { staffdeckDisplayText } from '@/employee';
import { cn } from '@/lib/utils';
import type {
  AgentProfileRead,
  TeamConversationKind,
  TeamConversationMessageRead,
  TeamConversationRead,
  TeamConversationsResponse,
  TeamRead,
} from '@/types';

const KIND_LABELS: Record<TeamConversationKind, string> = {
  tl_chat: '团队群聊',
  member_task: '任务协作',
  member_bid: '竞标沟通',
  tl_review: '验收复盘',
};

function conversationTitle(conversation: TeamConversationRead): string {
  return staffdeckDisplayText(conversation.title)
    .replace(/^团队任务验收:/, '')
    .replace(/^团队竞标(?:打分|裁决)?:/, '')
    .replace(/^团队任务:/, '')
    .trim() || KIND_LABELS[conversation.kind];
}

function taskTitleFromPrompt(content: string): string {
  const match = content.match(/任务标题[:：]\s*([^\n]+)/);
  return match?.[1]?.trim() || '';
}

export function collaborationPromptSummary(
  conversation: TeamConversationRead,
  content: string,
): string {
  if (!content.startsWith('你是团队')) return staffdeckDisplayText(content);
  const title = taskTitleFromPrompt(content) || conversationTitle(conversation);
  if (conversation.kind === 'member_bid') return `邀请参与「${title}」竞标`;
  if (conversation.kind === 'tl_review') return `复核「${title}」的执行结果`;
  return `委派任务「${title}」`;
}

export default function TeamCollaborationPanel({
  team,
  agents,
}: {
  team: TeamRead;
  agents: AgentProfileRead[];
}) {
  const [conversations, setConversations] = useState<TeamConversationRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<TeamConversationRead | null>(null);
  const [messages, setMessages] = useState<TeamConversationMessageRead[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const agentById = useMemo(
    () => new Map(agents.map((agent) => [agent.id, agent])),
    [agents],
  );
  const leaderMember = team.members.find((member) => member.role === 'leader');
  const leaderAgent = leaderMember ? agentById.get(leaderMember.agent_id) : undefined;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
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
        }).slice(0, 8));
      })
      .catch(() => {
        if (!cancelled) setConversations([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [leaderMember?.agent_id, team.id]);

  async function openConversation(conversation: TeamConversationRead) {
    setSelected(conversation);
    setMessages([]);
    setMessagesLoading(true);
    try {
      const rows = await api.get<TeamConversationMessageRead[]>(
        `/api/enterprise/teams/${team.id}/conversations/${conversation.session_id}/messages?tenant_id=${TENANT_ID}`,
      );
      setMessages(rows);
    } catch {
      setMessages([]);
    } finally {
      setMessagesLoading(false);
    }
  }

  if (!loading && conversations.length === 0) return null;

  return (
    <>
      <section
        aria-label="团队协作"
        className="shrink-0 border-b border-[#eef1f6] bg-white/80 px-[18px] py-[10px] backdrop-blur-sm"
      >
        <div className="mx-auto flex w-full max-w-[920px] items-center gap-[12px]">
          <div className="hidden w-[108px] shrink-0 sm:block">
            <div className="flex items-center gap-[6px] text-[12px] font-medium text-[#18181a]">
              <MessageSquareMore className="size-[14px] text-[#1a71ff]" />
              团队协作
            </div>
            <p className="mt-[2px] text-[10px] leading-[15px] text-[#858b9c]">项目领导与成员</p>
          </div>

          <div className="no-scrollbar flex min-w-0 flex-1 gap-[8px] overflow-x-auto py-[2px]">
            {loading ? (
              <span className="inline-flex h-[66px] w-full items-center justify-center gap-[6px] rounded-[12px] bg-[#f7f8fa] text-[11px] text-[#858b9c]">
                <LoaderCircle className="size-[13px] animate-spin" />
                正在同步协作记录
              </span>
            ) : conversations.map((conversation) => {
              const memberAgent = conversation.agent_id
                ? agentById.get(conversation.agent_id)
                : undefined;
              return (
                <button
                  key={conversation.session_id}
                  type="button"
                  onClick={() => void openConversation(conversation)}
                  className="group flex h-[66px] w-[220px] shrink-0 items-center gap-[9px] rounded-[12px] border border-[#e8ebf1] bg-[#fafbfc] px-[10px] text-left transition-all hover:border-[#cfe0ff] hover:bg-white hover:shadow-[0_5px_16px_rgba(24,24,26,0.06)]"
                >
                  <span className="flex shrink-0 items-center">
                    <EmployeeAvatar agent={leaderAgent} size={28} radius={9} />
                    <ArrowRight className="mx-[3px] size-[12px] text-[#a7adbb] transition-transform group-hover:translate-x-px" />
                    <EmployeeAvatar agent={memberAgent} size={28} radius={9} />
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col gap-[3px]">
                    <span className="flex min-w-0 items-center gap-[5px]">
                      <span className="truncate text-[11px] font-medium text-[#18181a]">
                        {conversationTitle(conversation)}
                      </span>
                      <span className="shrink-0 rounded-full bg-[#edf3ff] px-[5px] py-px text-[9px] text-[#1a71ff]">
                        {KIND_LABELS[conversation.kind]}
                      </span>
                    </span>
                    <span className="truncate text-[10px] text-[#858b9c]">
                      {`${leaderMember?.agent_name || '项目领导'} → ${conversation.agent_name || '团队成员'}`}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </section>

      <Dialog open={Boolean(selected)} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="flex max-h-[min(720px,calc(100dvh-32px))] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[18px] p-0 sm:max-w-[620px]">
          <div className="shrink-0 border-b border-[#eef1f6] px-[22px] py-[16px]">
            <DialogTitle className="text-[15px] font-semibold text-[#18181a]">
              {selected ? conversationTitle(selected) : '协作记录'}
            </DialogTitle>
            {selected && (
              <p className="mt-[3px] text-[11px] text-[#858b9c]">
                {`${leaderMember?.agent_name || '项目领导'} 与 ${selected.agent_name || '团队成员'} · ${KIND_LABELS[selected.kind]}`}
              </p>
            )}
          </div>
          <div className="min-h-[220px] flex-1 overflow-y-auto bg-[#f8f9fb] px-[18px] py-[18px]">
            {messagesLoading ? (
              <div className="grid min-h-[220px] place-items-center text-[12px] text-[#858b9c]">
                <span className="inline-flex items-center gap-[6px]">
                  <LoaderCircle className="size-[14px] animate-spin" />
                  正在加载协作记录
                </span>
              </div>
            ) : messages.map((message) => {
              if (!selected) return null;
              const fromLeader = message.role === 'user';
              const sender = fromLeader ? leaderAgent : (
                selected.agent_id ? agentById.get(selected.agent_id) : undefined
              );
              const senderName = fromLeader
                ? leaderMember?.agent_name || '项目领导'
                : selected.agent_name || '团队成员';
              const content = fromLeader
                ? collaborationPromptSummary(selected, message.content)
                : staffdeckDisplayText(message.content);
              return (
                <div
                  key={message.id}
                  className={cn('mb-[14px] flex items-start gap-[8px]', fromLeader && 'flex-row-reverse')}
                >
                  <EmployeeAvatar agent={sender} size={32} radius={10} />
                  <div className={cn('flex min-w-0 max-w-[82%] flex-col gap-[4px]', fromLeader && 'items-end')}>
                    <span className="px-[2px] text-[10px] text-[#858b9c]">{senderName}</span>
                    <div className={cn(
                      'rounded-[12px] px-[12px] py-[9px] text-[12px] leading-[19px] whitespace-pre-wrap text-[#18181a] shadow-[0_1px_2px_rgba(24,24,26,0.04)]',
                      fromLeader ? 'bg-[#e8f0ff]' : 'border border-[#e8ebf1] bg-white',
                    )}>
                      {content}
                    </div>
                  </div>
                </div>
              );
            })}
            {!messagesLoading && messages.length === 0 && (
              <div className="grid min-h-[220px] place-items-center text-[12px] text-[#a7adbb]">暂无协作内容</div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
