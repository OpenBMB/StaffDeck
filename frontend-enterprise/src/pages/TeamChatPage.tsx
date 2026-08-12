import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { Badge, Button, Input } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '../components/AppHeader';
import EmployeeAvatar from '../components/EmployeeAvatar';
import { formatClientDateTime } from '../lib/timezone';
import type { AgentProfileRead } from '../types';

import { relativeTimeLabel } from './TeamsPage';

export type TeamConversationKind = 'tl_chat' | 'member_task' | 'member_bid' | 'tl_review';

export interface TeamConversationSummary {
  session_id: string;
  kind: TeamConversationKind;
  agent_id: string | null;
  agent_name: string | null;
  task_id: string | null;
  title: string;
  preview: string;
  updated_at: string;
}

export interface TeamConversationsResponse {
  team_id: string;
  team_name: string;
  tl: { agent_id: string; agent_name: string | null; session_id: string | null };
  conversations: TeamConversationSummary[];
}

export interface TeamChatMessage {
  id: string;
  role: string;
  content: string;
  created_at: string;
}

const CONVERSATION_KIND_LABELS: Record<TeamConversationKind, string> = {
  tl_chat: 'TL 对话',
  member_task: '任务执行',
  member_bid: '竞标',
  tl_review: '验收',
};

export function conversationKindLabel(kind: string): string {
  return CONVERSATION_KIND_LABELS[kind as TeamConversationKind] || kind;
}

const TL_KEY = '__tl__';

export default function TeamChatPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  isAdmin?: boolean;
  onLogout?: () => void;
}) {
  const { teamId = '' } = useParams<{ teamId: string }>();
  const navigate = useNavigate();
  const [data, setData] = useState<TeamConversationsResponse | null>(null);
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string>(TL_KEY);
  const [tlSessionId, setTlSessionId] = useState<string | null>(null);
  const [creatingTlSession, setCreatingTlSession] = useState(false);
  const [messages, setMessages] = useState<TeamChatMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setLoading(true);
    void Promise.all([
      api
        .get<TeamConversationsResponse>(
          `/api/enterprise/teams/${teamId}/conversations?tenant_id=${TENANT_ID}`,
        )
        .then((response) => {
          setData(response);
          setTlSessionId(response.tl.session_id);
        })
        .catch((error) => {
          notify.error(error instanceof Error ? error.message : '加载团队会话失败');
        }),
      api
        .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
        .then(setAgents)
        .catch(() => setAgents([])),
    ]).finally(() => setLoading(false));
  }, [teamId]);

  const agentById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);

  const tlConversation = useMemo(
    () => data?.conversations.find((item) => item.kind === 'tl_chat') || null,
    [data],
  );
  const memberConversations = useMemo(
    () => (data?.conversations || []).filter((item) => item.kind !== 'tl_chat'),
    [data],
  );

  const selectedSessionId = selectedKey === TL_KEY ? tlSessionId : selectedKey;

  const selectedConversation = useMemo(() => {
    if (!data) return null;
    if (selectedKey === TL_KEY) {
      return {
        kind: 'tl_chat' as TeamConversationKind,
        agent_id: data.tl.agent_id,
        agent_name: data.tl.agent_name,
        title: tlConversation?.title || 'TL 对话',
      };
    }
    const found = memberConversations.find((item) => item.session_id === selectedKey);
    if (!found) return null;
    return {
      kind: found.kind,
      agent_id: found.agent_id,
      agent_name: found.agent_name,
      title: found.title,
    };
  }, [data, selectedKey, tlConversation, memberConversations]);

  // TL 会话不存在时，进入 TL 对话先创建会话
  useEffect(() => {
    if (!data || selectedKey !== TL_KEY || tlSessionId || creatingTlSession) return;
    setCreatingTlSession(true);
    api
      .post<{ session_id: string }>(`/api/enterprise/teams/${teamId}/tl/session`, {
        tenant_id: TENANT_ID,
      })
      .then((response) => setTlSessionId(response.session_id || null))
      .catch((error) => {
        notify.error(error instanceof Error ? error.message : '创建 TL 会话失败');
      })
      .finally(() => setCreatingTlSession(false));
  }, [data, selectedKey, tlSessionId, creatingTlSession, teamId]);

  useEffect(() => {
    if (!selectedSessionId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    setMessagesLoading(true);
    api
      .get<TeamChatMessage[]>(
        `/api/enterprise/teams/${teamId}/conversations/${selectedSessionId}/messages?tenant_id=${TENANT_ID}`,
      )
      .then((rows) => {
        if (!cancelled) setMessages(rows);
      })
      .catch((error) => {
        if (!cancelled) {
          setMessages([]);
          notify.error(error instanceof Error ? error.message : '加载消息失败');
        }
      })
      .finally(() => {
        if (!cancelled) setMessagesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [teamId, selectedSessionId]);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages, selectedKey, messagesLoading]);

  const selectConversation = useCallback((key: string) => {
    setSelectedKey(key);
    setDraft('');
  }, []);

  async function sendMessage() {
    const text = draft.trim();
    if (!text || sending || selectedKey !== TL_KEY || !tlSessionId) return;
    setSending(true);
    try {
      const response = await api.post<{ reply?: string; session_id?: string }>('/api/chat/turn', {
        tenant_id: TENANT_ID,
        session_id: tlSessionId,
        message: text,
      });
      const now = new Date().toISOString();
      setMessages((prev) => [
        ...prev,
        { id: `local-user-${now}`, role: 'user', content: text, created_at: now },
        {
          id: `local-assistant-${now}`,
          role: 'assistant',
          content: response.reply || '',
          created_at: new Date().toISOString(),
        },
      ]);
      setDraft('');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '发送失败');
    } finally {
      setSending(false);
    }
  }

  const tlName = data?.tl.agent_name || 'TL';
  const isTlSelected = selectedKey === TL_KEY;
  const canSend = isTlSelected && Boolean(tlSessionId) && !creatingTlSession;

  function conversationItem(options: {
    itemKey: string;
    agentId: string | null;
    title: string;
    preview: string;
    updatedAt: string;
    kind: string;
    isTl?: boolean;
  }) {
    const { itemKey, agentId, title, preview, updatedAt, kind, isTl } = options;
    const selected = selectedKey === itemKey;
    return (
      <button
        key={itemKey}
        type="button"
        aria-current={selected ? 'true' : undefined}
        onClick={() => selectConversation(itemKey)}
        className={cn(
          'flex w-full items-center gap-[10px] rounded-[12px] px-[10px] py-[10px] text-left transition-colors',
          selected ? 'bg-[#e8f0ff]' : 'hover:bg-[#f6f7fa]',
        )}
      >
        <EmployeeAvatar agent={agentById.get(agentId || '')} size={40} radius={12} className="shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-[6px]">
            <span className="truncate text-[14px] font-medium text-[#18181a]" title={title}>
              {title}
            </span>
            <Badge
              variant="secondary"
              className={cn(
                'shrink-0 rounded-full text-[12px] font-normal',
                isTl ? 'bg-[#1a71ff] text-white' : 'bg-[#f2f3f7] text-[#464c5e]',
              )}
            >
              {isTl ? 'TL' : conversationKindLabel(kind)}
            </Badge>
          </span>
          <span className="mt-[2px] flex items-center justify-between gap-[8px]">
            <span className="min-w-0 flex-1 truncate text-[12px] text-[#858b9c]">
              {preview || (isTl ? '与 TL 直接沟通' : '暂无消息')}
            </span>
            {updatedAt && (
              <span className="shrink-0 text-[12px] text-[#a7adbb]">{relativeTimeLabel(updatedAt)}</span>
            )}
          </span>
        </span>
      </button>
    );
  }

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        left={(
          <div className="flex items-center gap-[12px]">
            <EmployeeAvatar agent={agentById.get(data?.tl.agent_id || '')} size={44} radius={12} />
            <div className="min-w-0">
              <h1 className="truncate text-[18px] font-semibold text-[#18181a]">
                {data?.team_name || '团队聊天室'}
              </h1>
              <p className="truncate text-[12px] text-[#858b9c]">{`TL：${tlName}`}</p>
            </div>
          </div>
        )}
      />

      <div className="mt-[16px]">
        <Button
          type="button"
          variant="outline"
          onClick={() => navigate(`/enterprise/teams/${teamId}`)}
          className="h-[32px] rounded-[10px] border-[#e3e7f1] px-[12px] text-[12px] font-normal text-[#464c5e]"
        >
          返回团队
        </Button>
      </div>

      <div className="mt-[16px] grid grid-cols-1 gap-[20px] lg:grid-cols-[320px_1fr]">
        <section
          aria-label="会话列表"
          className="flex flex-col gap-[4px] self-start rounded-[20px] bg-white p-[12px] shadow-[0_0_6px_rgba(0,0,0,0.05)]"
        >
          {data && (
            <>
              {conversationItem({
                itemKey: TL_KEY,
                agentId: data.tl.agent_id,
                title: tlConversation?.title || 'TL 对话',
                preview: tlConversation?.preview || '',
                updatedAt: tlConversation?.updated_at || '',
                kind: 'tl_chat',
                isTl: true,
              })}
              {memberConversations.length > 0 && <div className="mx-[10px] my-[6px] h-px bg-[#eef1f6]" />}
              {memberConversations.map((item) =>
                conversationItem({
                  itemKey: item.session_id,
                  agentId: item.agent_id,
                  title: item.title,
                  preview: item.preview,
                  updatedAt: item.updated_at,
                  kind: item.kind,
                }),
              )}
              {memberConversations.length === 0 && (
                <p className="py-[12px] text-center text-[12px] text-[#a7adbb]">暂无员工会话</p>
              )}
            </>
          )}
          {!data && !loading && (
            <p className="py-[12px] text-center text-[12px] text-[#a7adbb]">暂无会话</p>
          )}
        </section>

        <section
          aria-label="消息区"
          className="flex min-h-[560px] flex-col rounded-[20px] bg-white p-[20px] shadow-[0_0_6px_rgba(0,0,0,0.05)]"
        >
          <div className="mb-[12px] flex items-center gap-[8px] border-b border-[#eef1f6] pb-[12px]">
            <h2 className="min-w-0 flex-1 truncate text-[16px] font-medium text-[#18181a]">
              {selectedConversation?.title || ''}
            </h2>
            {selectedConversation && (
              <Badge
                variant="secondary"
                className="shrink-0 rounded-full bg-[#f2f3f7] text-[12px] font-normal text-[#464c5e]"
              >
                {conversationKindLabel(selectedConversation.kind)}
              </Badge>
            )}
          </div>

          <div ref={listRef} className="flex min-h-0 flex-1 flex-col gap-[12px] overflow-y-auto">
            {messagesLoading && (
              <p className="py-[24px] text-center text-[12px] text-[#a7adbb]">加载中…</p>
            )}
            {!messagesLoading && messages.length === 0 && (
              <div className="flex flex-1 items-center justify-center">
                <p className="text-[13px] text-[#a7adbb]">
                  {creatingTlSession ? '正在创建 TL 会话…' : '暂无消息'}
                </p>
              </div>
            )}
            {!messagesLoading &&
              messages.map((message) => {
                const isUser = message.role === 'user';
                return (
                  <div
                    key={message.id}
                    data-role={isUser ? 'user' : 'agent'}
                    className={cn('flex min-w-0', isUser ? 'justify-end' : 'justify-start')}
                  >
                    {!isUser && (
                      <EmployeeAvatar
                        agent={agentById.get(selectedConversation?.agent_id || '')}
                        size={32}
                        radius={10}
                        className="mt-[2px] mr-[8px] shrink-0"
                      />
                    )}
                    <div
                      className={cn(
                        'relative box-border min-w-0 max-w-[min(680px,92%)] text-[14px] leading-[1.7] wrap-anywhere text-[#18181a]',
                        isUser
                          ? 'rounded-[14px] bg-[#f6f6f6] px-[16px] py-[11px]'
                          : 'w-full max-w-full rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[18px] py-[14px] shadow-[0_1px_2px_rgba(24,24,26,0.03)]',
                      )}
                    >
                      <p className="whitespace-pre-wrap">{message.content}</p>
                      <span className="mt-[4px] block text-[11px] text-[#a7adbb]">
                        {formatClientDateTime(message.created_at, '')}
                      </span>
                    </div>
                  </div>
                );
              })}
          </div>

          <div className="mt-[12px] border-t border-[#eef1f6] pt-[12px]">
            {isTlSelected ? (
              <form
                className="flex items-center gap-[8px]"
                onSubmit={(event) => {
                  event.preventDefault();
                  void sendMessage();
                }}
              >
                <Input
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder="向 TL 发送消息…"
                  aria-label="输入消息"
                  disabled={!canSend || sending}
                  className="h-[40px] flex-1 rounded-[10px] border-[#e3e7f1] text-[14px]"
                />
                <Button
                  type="submit"
                  disabled={!canSend || sending || !draft.trim()}
                  className="h-[40px] shrink-0 rounded-[10px] bg-[#18181a] px-[16px] text-[14px] font-normal text-white hover:bg-[#303030]"
                >
                  {sending ? '发送中…' : '发送'}
                </Button>
              </form>
            ) : (
              <p className="rounded-[10px] bg-[#f8f9fb] px-[14px] py-[10px] text-center text-[13px] text-[#858b9c]">
                任务会话仅可查看
              </p>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
