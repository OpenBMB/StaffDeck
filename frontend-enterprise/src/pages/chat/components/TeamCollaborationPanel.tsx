import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, CircleAlert, LoaderCircle, SendHorizontal } from 'lucide-react';

import { api, TENANT_ID } from '@/api/client';
import EmployeeAvatar from '@/components/EmployeeAvatar';
import { staffdeckDisplayText } from '@/employee';
import { cn } from '@/lib/utils';
import type {
  AgentProfileRead,
  ChatMessage,
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

function conversationTimestamp(conversation: TeamConversationRead): number {
  const createdAt = Date.parse(conversation.created_at);
  if (Number.isFinite(createdAt)) return createdAt;
  const updatedAt = Date.parse(conversation.updated_at);
  return Number.isFinite(updatedAt) ? updatedAt : Number.POSITIVE_INFINITY;
}

export type TeamChatTimelineEntry =
  | { kind: 'message'; message: ChatMessage; messageIndex: number }
  | { kind: 'collaboration'; conversation: TeamConversationRead };

export function mergeTeamChatTimeline(
  messages: ChatMessage[],
  conversations: TeamConversationRead[],
): TeamChatTimelineEntry[] {
  const buckets = Array.from(
    { length: messages.length + 1 },
    () => [] as TeamConversationRead[],
  );

  [...conversations]
    .sort((left, right) => conversationTimestamp(left) - conversationTimestamp(right))
    .forEach((conversation) => {
      const timestamp = conversationTimestamp(conversation);
      const nextMessageIndex = messages.findIndex((message) => {
        const messageTimestamp = Date.parse(message.created_at);
        return Number.isFinite(messageTimestamp) && messageTimestamp > timestamp;
      });
      buckets[nextMessageIndex < 0 ? messages.length : nextMessageIndex].push(conversation);
    });

  const timeline: TeamChatTimelineEntry[] = [];
  buckets[0].forEach((conversation) => timeline.push({ kind: 'collaboration', conversation }));
  messages.forEach((message, messageIndex) => {
    timeline.push({ kind: 'message', message, messageIndex });
    buckets[messageIndex + 1].forEach((conversation) => (
      timeline.push({ kind: 'collaboration', conversation })
    ));
  });
  return timeline;
}

export function useTeamCollaborations(team?: TeamRead | null): TeamConversationRead[] {
  const [conversations, setConversations] = useState<TeamConversationRead[]>([]);
  const leaderAgentId = team?.members.find((member) => member.role === 'leader')?.agent_id;

  useEffect(() => {
    let cancelled = false;
    if (!team) {
      setConversations([]);
      return () => {
        cancelled = true;
      };
    }
    setConversations([]);
    const loadConversations = async () => {
      try {
        const response = await api.get<TeamConversationsResponse>(
          `/api/enterprise/teams/${team.id}/conversations?tenant_id=${TENANT_ID}`,
        );
        if (cancelled) return;
        const seen = new Set<string>();
        const latest = response.conversations.filter((conversation) => {
          if (conversation.kind !== 'member_task' && conversation.kind !== 'member_bid') return false;
          if (conversation.agent_id === leaderAgentId) return false;
          const key = `${conversation.agent_id || ''}:${conversation.kind}:${conversationTitle(conversation)}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        }).slice(0, 4);
        setConversations(latest.sort(
          (left, right) => conversationTimestamp(left) - conversationTimestamp(right),
        ));
      } catch {
        // Keep the last successful snapshot during a transient polling error.
      }
    };
    void loadConversations();
    const pollTimer = window.setInterval(() => void loadConversations(), 2_000);
    return () => {
      cancelled = true;
      window.clearInterval(pollTimer);
    };
  }, [leaderAgentId, team?.id]);

  return conversations;
}

export default function TeamCollaborationPanel({
  team,
  agents,
  conversation,
}: {
  team: TeamRead;
  agents: AgentProfileRead[];
  conversation?: TeamConversationRead;
}) {
  const loadedConversations = useTeamCollaborations(conversation ? undefined : team);
  const conversations = conversation ? [conversation] : loadedConversations;
  const [expandedSessionId, setExpandedSessionId] = useState('');
  const [messagesBySession, setMessagesBySession] = useState<Record<string, TeamConversationMessageRead[]>>({});
  const [loadingSessionId, setLoadingSessionId] = useState('');
  const [answerByTaskId, setAnswerByTaskId] = useState<Record<string, string>>({});
  const [submittingTaskId, setSubmittingTaskId] = useState('');
  const [submittedTaskIds, setSubmittedTaskIds] = useState<string[]>([]);
  const [submitErrorByTaskId, setSubmitErrorByTaskId] = useState<Record<string, string>>({});
  const agentById = useMemo(
    () => new Map(agents.map((agent) => [agent.id, agent])),
    [agents],
  );
  const leaderMember = team.members.find((member) => member.role === 'leader');
  const leaderAgent = leaderMember ? agentById.get(leaderMember.agent_id) : undefined;

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

  async function resumeTask(conversation: TeamConversationRead) {
    const taskId = conversation.task_id;
    const answer = taskId ? (answerByTaskId[taskId] || '').trim() : '';
    if (!taskId || !answer || submittingTaskId) return;
    setSubmittingTaskId(taskId);
    setSubmitErrorByTaskId((current) => ({ ...current, [taskId]: '' }));
    try {
      await api.post(
        `/api/enterprise/teams/${team.id}/tasks/${taskId}/resume`,
        { tenant_id: TENANT_ID, answer },
      );
      setSubmittedTaskIds((current) => (
        current.includes(taskId) ? current : [...current, taskId]
      ));
    } catch (error) {
      setSubmitErrorByTaskId((current) => ({
        ...current,
        [taskId]: error instanceof Error ? error.message : '补充信息提交失败',
      }));
    } finally {
      setSubmittingTaskId('');
    }
  }

  if (conversations.length === 0) return null;

  return conversations.map((conversation) => {
    const memberAgent = conversation.agent_id
      ? agentById.get(conversation.agent_id)
      : undefined;
    const memberName = conversation.agent_name || '团队成员';
    const expanded = expandedSessionId === conversation.session_id;
    const loading = loadingSessionId === conversation.session_id;
    const memberReplies = (messagesBySession[conversation.session_id] || [])
      .filter((message) => message.role === 'assistant');
    const preview = staffdeckDisplayText(conversation.preview || '成员正在处理…');
    const taskId = conversation.task_id || '';
    const waitingForInput = Boolean(conversation.needs_input && taskId);
    const submitted = Boolean(taskId && submittedTaskIds.includes(taskId));
    const pendingQuestion = staffdeckDisplayText(
      conversation.pending_question || conversation.preview || '请补充任务所需信息。',
    );
    const taskAnswer = taskId ? (answerByTaskId[taskId] || '') : '';
    const submitError = taskId ? submitErrorByTaskId[taskId] : '';

    return (
      <div
        key={conversation.session_id}
        aria-label={`团队协作 ${memberName}`}
        className="relative flex min-w-0 flex-col gap-[10px]"
      >
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
            {waitingForInput ? (
              <div className="w-full rounded-[16px] border border-[#f0d8a8] bg-[#fffdf7] px-[14px] py-[12px] shadow-[0_8px_24px_rgba(90,61,8,0.06)]">
                <div className="flex items-center gap-[7px] text-[11px] font-medium text-[#9a6811]">
                  <CircleAlert className="size-[14px]" />
                  <span>{`${memberName}需要补充信息`}</span>
                  <span className="rounded-full bg-[#fff0c8] px-[7px] py-[2px] text-[9px] text-[#8a5b0a]">
                    等待回复
                  </span>
                </div>
                <p className="mt-[8px] whitespace-pre-wrap text-[13px] leading-[21px] text-[#34302a]">
                  {pendingQuestion}
                </p>
                {submitted ? (
                  <div className="mt-[11px] rounded-[10px] bg-[#eef8f3] px-[11px] py-[9px] text-[12px] text-[#277657]">
                    已补充，任务正在继续执行
                  </div>
                ) : (
                  <div className="mt-[11px] flex items-end gap-[8px]">
                    <textarea
                      aria-label={`回复${memberName}的补充问题`}
                      value={taskAnswer}
                      onChange={(event) => setAnswerByTaskId((current) => ({
                        ...current,
                        [taskId]: event.target.value,
                      }))}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault();
                          void resumeTask(conversation);
                        }
                      }}
                      rows={2}
                      placeholder="补充所需信息，Enter 发送"
                      className="min-h-[58px] min-w-0 flex-1 resize-none rounded-[11px] border border-[#e5d7b7] bg-white px-[11px] py-[8px] text-[12px] leading-[18px] text-[#18181a] outline-none transition focus:border-[#c99838] focus:ring-2 focus:ring-[#efdba8]/60"
                    />
                    <button
                      type="button"
                      aria-label="补充并继续"
                      disabled={!taskAnswer.trim() || submittingTaskId === taskId}
                      onClick={() => void resumeTask(conversation)}
                      className="flex h-[36px] shrink-0 items-center gap-[5px] rounded-[10px] bg-[#18181a] px-[12px] text-[11px] font-medium text-white transition hover:bg-[#343437] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {submittingTaskId === taskId
                        ? <LoaderCircle className="size-[13px] animate-spin" />
                        : <SendHorizontal className="size-[13px]" />}
                      继续执行
                    </button>
                  </div>
                )}
                {submitError && (
                  <p className="mt-[7px] text-[11px] text-[#c13e35]">{submitError}</p>
                )}
              </div>
            ) : (
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
                    <span className="block text-[12px] text-[#a7adbb]">
                      成员正在处理，完成后会自动更新
                    </span>
                  )}
                </span>
              )}
              </button>
            )}
          </div>
        </div>
      </div>
    );
  });
}
