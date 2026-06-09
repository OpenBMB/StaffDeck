import {
  BranchesOutlined,
  CloudSyncOutlined,
  DeleteOutlined,
  DislikeOutlined,
  DownOutlined,
  EditOutlined,
  LikeOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  PlusOutlined,
  RightOutlined,
  SendOutlined,
  StopOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import { Button, Input, Modal, Typography, message } from 'antd';
import type { MouseEvent, ReactNode } from 'react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { SHOW_DEBUG, TENANT_ID, api, clearAuthSession, getAuthSession, streamChatTurn } from '../api/client';
import type { ChatMessage, ChatSession, ChatTurnResponse, TurnTraceRead, UIConfigRead } from '../types';

type SessionSlot = {
  serverMessages: ChatMessage[];
  realtimeMessages: ChatMessage[];
};

type StreamSlot = {
  loading: boolean;
  phase: string;
  timer: number | null;
  accumulated: string;
  turnId: string | null;
  abortController: AbortController | null;
};

type TraceSkill = {
  skillId: string;
  name?: string;
  stepId?: string;
  state?: string;
};

type TraceTool = {
  toolId: string;
  toolCallId?: string;
  toolName: string;
  rawToolName?: string;
  success?: boolean;
  isError?: boolean;
  content?: unknown;
};

type TraceLine = {
  id: string;
  kind: 'thinking' | 'decision' | 'skill' | 'tool' | 'code';
  text: string;
  detail?: string;
  code?: string;
  language?: string;
  state: 'running' | 'completed' | 'failed';
  collapsible?: boolean;
};

type TurnTrace = {
  lines: TraceLine[];
  startedAt: number;
  completedAt?: number;
};

function createEmptySlot(): SessionSlot {
  return { serverMessages: [], realtimeMessages: [] };
}

function createStreamSlot(): StreamSlot {
  return { loading: false, phase: '', timer: null, accumulated: '', turnId: null, abortController: null };
}

function createTurnTrace(): TurnTrace {
  return { lines: [], startedAt: Date.now() };
}

function normalizeMessageText(value?: string): string {
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]*`|\*\*[^*]+?\*\*|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
  let cursor = 0;
  let index = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }
    const token = match[0];
    const key = `${keyPrefix}-inline-${index}`;
    if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**') && token.endsWith('**')) {
      nodes.push(<strong key={key}>{renderInlineMarkdown(token.slice(2, -2), key)}</strong>);
    } else {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
      if (link) {
        nodes.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer">
            {link[1]}
          </a>,
        );
      } else {
        nodes.push(token);
      }
    }
    cursor = match.index + token.length;
    index += 1;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function renderInlineLines(lines: string[], keyPrefix: string): ReactNode[] {
  return lines.flatMap((line, lineIndex) => {
    const nodes = renderInlineMarkdown(line, `${keyPrefix}-line-${lineIndex}`);
    if (lineIndex === 0) return nodes;
    return [<br key={`${keyPrefix}-br-${lineIndex}`} />, ...nodes];
  });
}

function isBlockBoundary(line: string): boolean {
  const trimmed = line.trim();
  return (
    trimmed.startsWith('```') ||
    /^#{1,6}\s+/.test(trimmed) ||
    /^[-*]\s+/.test(trimmed) ||
    /^\d+[.)]\s+/.test(trimmed)
  );
}

function renderMarkdownBlocks(content: string): ReactNode[] {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let index = 0;
  let blockIndex = 0;

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    const key = `md-${blockIndex}`;
    if (!trimmed) {
      index += 1;
      continue;
    }

    if (trimmed.startsWith('```')) {
      const language = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(
        <pre key={key} className="md-code-block">
          <code data-language={language || undefined}>{codeLines.join('\n')}</code>
        </pre>,
      );
      blockIndex += 1;
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4) as 1 | 2 | 3 | 4;
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key}>{renderInlineMarkdown(heading[2], key)}</Tag>);
      index += 1;
      blockIndex += 1;
      continue;
    }

    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-*]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ul key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-${itemIndex}`)}</li>
          ))}
        </ul>,
      );
      blockIndex += 1;
      continue;
    }

    if (/^\d+[.)]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^\d+[.)]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^\d+[.)]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ol key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-${itemIndex}`)}</li>
          ))}
        </ol>,
      );
      blockIndex += 1;
      continue;
    }

    const paragraphLines: string[] = [];
    while (index < lines.length && lines[index].trim() && !isBlockBoundary(lines[index])) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    blocks.push(<p key={key}>{renderInlineLines(paragraphLines, key)}</p>);
    blockIndex += 1;
  }

  return blocks;
}

function MarkdownMessage({ content }: { content: string }) {
  return <div className="assistant-answer markdown-message">{renderMarkdownBlocks(content)}</div>;
}

function TerminalTraceIcon() {
  return (
    <svg className="trace-terminal-icon" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <rect x="2.5" y="3.5" width="13" height="11" rx="2.2" />
      <path d="M5.4 7.1L7.6 9l-2.2 1.9" />
      <path d="M9.2 11.1h3.4" />
    </svg>
  );
}

function parseMessageTime(value?: string): number {
  if (!value) return 0;
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const time = Date.parse(normalized);
  return Number.isFinite(time) ? time : 0;
}

function hasEquivalentServerMessage(messageItem: ChatMessage, serverMessages: ChatMessage[]): boolean {
  const content = normalizeMessageText(messageItem.content);
  if (!content) return false;
  return serverMessages.some((serverMessage) => {
    if (serverMessage.role !== messageItem.role) return false;
    if (normalizeMessageText(serverMessage.content) !== content) return false;
    if (messageItem.turnId || serverMessage.turnId) {
      return Boolean(messageItem.turnId && serverMessage.turnId && messageItem.turnId === serverMessage.turnId);
    }
    return true;
  });
}

function hasServerMessageForTurn(messageItem: ChatMessage, serverMessages: ChatMessage[]): boolean {
  if (!messageItem.turnId) return false;
  return serverMessages.some(
    (serverMessage) => serverMessage.turnId === messageItem.turnId && serverMessage.role === messageItem.role,
  );
}

function attachTurnIdsToServerMessages(
  serverMessages: ChatMessage[],
  realtimeMessages: ChatMessage[],
  previousMessages: ChatMessage[] = [],
): ChatMessage[] {
  const pendingTurns = realtimeMessages.filter((item) => item.turnId && item.role === 'user').reverse();
  const realtimeTurnIds = new Set(
    realtimeMessages
      .map((item) => item.turnId)
      .filter((turnId): turnId is string => Boolean(turnId)),
  );
  const pendingTurnIdsByServerId = new Map<string, string>();
  const matchedServerMessageIds = new Set<string>();
  const previousTurnIds = new Map(
    previousMessages
      .filter((item) => item.turnId && (item.turnId === item.id || realtimeTurnIds.has(item.turnId)))
      .map((item) => [item.id, item.turnId as string]),
  );

  pendingTurns.forEach((pendingTurn) => {
    const pendingContent = normalizeMessageText(pendingTurn.content);
    if (!pendingTurn.turnId || !pendingContent) return;
    for (let index = serverMessages.length - 1; index >= 0; index -= 1) {
      const serverMessage = serverMessages[index];
      if (serverMessage.role !== 'user' || matchedServerMessageIds.has(serverMessage.id)) continue;
      if (normalizeMessageText(serverMessage.content) !== pendingContent) continue;
      pendingTurnIdsByServerId.set(serverMessage.id, pendingTurn.turnId);
      matchedServerMessageIds.add(serverMessage.id);
      return;
    }
  });

  let activeTurnId: string | undefined;

  return serverMessages.map((messageItem) => {
    const previousTurnId = previousTurnIds.get(messageItem.id);
    if (messageItem.role === 'user') {
      activeTurnId = pendingTurnIdsByServerId.get(messageItem.id) || previousTurnId || messageItem.turnId || messageItem.id;
      return { ...messageItem, turnId: activeTurnId };
    }
    if (messageItem.role === 'assistant' && activeTurnId) {
      return { ...messageItem, turnId: previousTurnId || messageItem.turnId || activeTurnId };
    }
    return previousTurnId ? { ...messageItem, turnId: previousTurnId } : messageItem;
  });
}

function shouldKeepRealtimeMessage(
  messageItem: ChatMessage,
  serverMessages: ChatMessage[],
  latestServerTime: number,
  activeTurnId?: string | null,
): boolean {
  if (messageItem.isStreaming) {
    return !messageItem.turnId || !activeTurnId || messageItem.turnId === activeTurnId;
  }
  if (hasEquivalentServerMessage(messageItem, serverMessages)) return false;
  if (hasServerMessageForTurn(messageItem, serverMessages)) return false;
  if (messageItem.turnId && activeTurnId && messageItem.turnId === activeTurnId) return true;
  if (!latestServerTime) return true;
  return parseMessageTime(messageItem.created_at) > latestServerTime;
}

function computeMergedMessages(slot: SessionSlot, activeTurnId?: string | null): ChatMessage[] {
  const serverIds = new Set(slot.serverMessages.map((item) => item.id));
  const latestServerTime = Math.max(0, ...slot.serverMessages.map((item) => parseMessageTime(item.created_at)));
  const extras = slot.realtimeMessages.filter((item) => {
    if (serverIds.has(item.id)) return false;
    return shouldKeepRealtimeMessage(item, slot.serverMessages, latestServerTime, activeTurnId);
  });
  const combined = [
    ...slot.serverMessages.map((messageItem, index) => ({ messageItem, index })),
    ...extras.map((messageItem, index) => ({ messageItem, index: slot.serverMessages.length + index })),
  ];

  return combined
    .sort((left, right) => (
      parseMessageTime(left.messageItem.created_at) - parseMessageTime(right.messageItem.created_at) ||
      left.index - right.index
    ))
    .map((item) => item.messageItem);
}

function publicStreamPhase(data: Record<string, unknown>): string {
  const phase = typeof data.phase === 'string' ? data.phase : '';
  const text = typeof data.text === 'string' ? data.text : '';
  if (phase === 'error') return text || '请求失败';
  return '正在思考';
}

function normalizeTraceSkill(value: unknown): TraceSkill | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const skillId = typeof item.skillId === 'string' ? item.skillId : '';
  if (!skillId) return null;
  return {
    skillId,
    name: typeof item.name === 'string' ? item.name : skillId,
    stepId: typeof item.stepId === 'string' ? item.stepId : undefined,
    state: typeof item.state === 'string' ? item.state : undefined,
  };
}

function streamSkillLabel(data: Record<string, unknown>, skill: TraceSkill): string {
  if (skill.state === 'suspended') return '挂起技能';
  const decision = typeof data.runtimeDecision === 'string' ? data.runtimeDecision : '';
  const fromSkillId = typeof data.fromSkillId === 'string' ? data.fromSkillId : '';
  const toSkillId = typeof data.toSkillId === 'string' ? data.toSkillId : '';
  if (decision === 'start_skill') return '选择技能';
  if (decision === 'suspend_current_and_start_new_skill') return '切换技能';
  if (
    (decision === 'answer_related_question_then_resume' || decision === 'answer_chitchat_then_resume')
    && fromSkillId
    && toSkillId
    && fromSkillId !== toSkillId
  ) return '切换技能';
  if (decision === 'exit_current_skill') return '恢复技能';
  return '推进技能';
}

function normalizeTraceTool(value: unknown): TraceTool | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const toolId = typeof item.toolId === 'string' ? item.toolId : '';
  if (!toolId) return null;
  return {
    toolId,
    toolCallId: typeof item.toolCallId === 'string' ? item.toolCallId : undefined,
    toolName: typeof item.toolName === 'string' ? item.toolName : toolId,
    rawToolName: typeof item.rawToolName === 'string' ? item.rawToolName : toolId,
    success: typeof item.success === 'boolean' ? item.success : undefined,
    isError: typeof item.isError === 'boolean' ? item.isError : undefined,
    content: item.content,
  };
}

function shortTraceValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
}

function toolTraceDetail(tool: TraceTool): string | undefined {
  const content = tool.content && typeof tool.content === 'object' ? tool.content as Record<string, unknown> : null;
  const data = content?.data && typeof content.data === 'object' ? content.data as Record<string, unknown> : null;
  const parts = [
    tool.rawToolName && tool.rawToolName !== tool.toolName ? tool.rawToolName : '',
    shortTraceValue(data?.source),
    data?.found === false ? '未命中' : data?.found === true ? '已命中' : '',
    shortTraceValue(data?.miss_reason),
    shortTraceValue(data?.recommendation),
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

function reflectionTraceDetail(data: Record<string, unknown>): string | undefined {
  const parts = [
    typeof data.reason === 'string' ? data.reason : '',
    typeof data.target_tool_name === 'string' ? `工具 ${data.target_tool_name}` : '',
    typeof data.target_skill_id === 'string' ? `技能 ${data.target_skill_id}` : '',
    typeof data.target_step_id === 'string' ? `步骤 ${data.target_step_id}` : '',
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

function traceLineAllowed(line: TraceLine, config: UIConfigRead): boolean {
  if (line.kind === 'thinking' || line.kind === 'decision') return config.show_thinking_trace;
  if (line.kind === 'code') return config.show_thinking_trace;
  if (line.kind === 'skill') return config.show_skill_trace;
  if (line.kind === 'tool') return config.show_tool_trace;
  return true;
}

function traceSummary(trace: TurnTrace, lines: TraceLine[]): { text: string; state: TraceLine['state'] } {
  if (lines.some((line) => line.state === 'running')) {
    return { text: '正在思考', state: 'running' };
  }
  if (lines.some((line) => line.state === 'failed')) {
    return { text: '思考遇到问题', state: 'failed' };
  }
  return { text: '已完成思考', state: 'completed' };
}

function traceDetails(lines: TraceLine[]): TraceLine[] {
  const hiddenPlaceholders = new Set(['正在思考', '已完成思考']);
  const details = lines.filter((line) => {
    if (line.kind === 'thinking') return false;
    if (hiddenPlaceholders.has(line.text) && !line.detail && !line.code) return false;
    return true;
  });
  return details.length > 0 ? details : lines.filter((line) => line.text !== '已完成思考');
}

function canRateMessage(item: ChatMessage): boolean {
  return (
    item.role === 'assistant'
    && !item.isStreaming
    && !item.isError
    && !item.id.startsWith('__')
    && !item.id.startsWith('text_')
    && !item.id.startsWith('error_')
  );
}

export default function ChatWindowPage() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const [auth] = useState(() => getAuthSession());
  const tenantId = auth?.user.tenant_id || TENANT_ID;
  const userId = auth?.user.id || '';
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [input, setInput] = useState('');
  const [lastTurn, setLastTurn] = useState<ChatTurnResponse | null>(null);
  const [renameSession, setRenameSession] = useState<ChatSession | null>(null);
  const [renameTitle, setRenameTitle] = useState('');
  const [storeTick, setStoreTick] = useState(0);
  const [streamTick, setStreamTick] = useState(0);
  const [traceTick, setTraceTick] = useState(0);
  const [expandedTraceIds, setExpandedTraceIds] = useState<string[]>([]);
  const [isComposing, setIsComposing] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => (
    window.localStorage.getItem('skill_agent_sidebar_collapsed') === 'true'
  ));
  const [uiConfig, setUiConfig] = useState<UIConfigRead>({
    tenant_id: tenantId,
    show_thinking_trace: true,
    show_skill_trace: true,
    show_tool_trace: true,
    reflection_max_rounds: 1,
    agent_loop_max_actions: 6,
    updated_at: '',
  });
  const chatMessagesRef = useRef<HTMLDivElement>(null);
  const storeRef = useRef(new Map<string, SessionSlot>());
  const streamRef = useRef(new Map<string, StreamSlot>());
  const turnTraceRef = useRef(new Map<string, TurnTrace>());

  const notifyStore = useCallback(() => setStoreTick((value) => value + 1), []);
  const notifyStream = useCallback(() => setStreamTick((value) => value + 1), []);
  const notifyTrace = useCallback(() => setTraceTick((value) => value + 1), []);
  const scrollChatToBottom = useCallback(() => {
    const element = chatMessagesRef.current;
    if (!element) return;
    window.requestAnimationFrame(() => {
      element.scrollTop = element.scrollHeight;
      window.requestAnimationFrame(() => {
        element.scrollTop = element.scrollHeight;
      });
    });
  }, []);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem('skill_agent_sidebar_collapsed', String(next));
      return next;
    });
  }, []);
  const toggleTrace = useCallback((turnId: string) => {
    setExpandedTraceIds((current) => (
      current.includes(turnId)
        ? current.filter((item) => item !== turnId)
        : [...current, turnId]
    ));
  }, []);

  const getSlot = useCallback((id: string): SessionSlot => {
    const store = storeRef.current;
    if (!store.has(id)) {
      store.set(id, createEmptySlot());
    }
    return store.get(id)!;
  }, []);

  const getStreamSlot = useCallback((id: string): StreamSlot => {
    const store = streamRef.current;
    if (!store.has(id)) {
      store.set(id, createStreamSlot());
    }
    return store.get(id)!;
  }, []);

  const getTurnTrace = useCallback((id: string): TurnTrace => {
    const store = turnTraceRef.current;
    if (!store.has(id)) {
      store.set(id, createTurnTrace());
    }
    return store.get(id)!;
  }, []);

  const upsertTraceLine = useCallback((turnId: string, line: TraceLine) => {
    const trace = getTurnTrace(turnId);
    const index = trace.lines.findIndex((item) => item.id === line.id);
    if (index >= 0) {
      trace.lines = [...trace.lines];
      trace.lines[index] = line;
    } else {
      trace.lines = [...trace.lines, line].slice(-80);
    }
    notifyTrace();
  }, [getTurnTrace, notifyTrace]);

  const finishTrace = useCallback((turnId: string, failed = false) => {
    const trace = getTurnTrace(turnId);
    trace.completedAt = Date.now();
    trace.lines = trace.lines.map((line) => ({
      ...line,
      state: failed && line.state === 'running' ? 'failed' : line.state === 'running' ? 'completed' : line.state,
    }));
    notifyTrace();
  }, [getTurnTrace, notifyTrace]);

  const pruneRealtime = useCallback((id: string) => {
    const slot = getSlot(id);
    const stream = getStreamSlot(id);
    const latestServerTime = Math.max(0, ...slot.serverMessages.map((item) => parseMessageTime(item.created_at)));
    slot.realtimeMessages = slot.realtimeMessages.filter((item) => {
      if (slot.serverMessages.some((serverMessage) => serverMessage.id === item.id)) return false;
      return shouldKeepRealtimeMessage(item, slot.serverMessages, latestServerTime, stream.turnId);
    });
  }, [getSlot, getStreamSlot]);

  const displayedMessages = useMemo(() => {
    if (!sessionId) return [];
    void storeTick;
    void streamTick;
    return computeMergedMessages(getSlot(sessionId), getStreamSlot(sessionId).turnId);
  }, [getSlot, getStreamSlot, sessionId, storeTick, streamTick]);

  const currentStream = useMemo(() => {
    void streamTick;
    return sessionId ? getStreamSlot(sessionId) : createStreamSlot();
  }, [getStreamSlot, sessionId, streamTick]);

  const loadSessions = useCallback(() => {
    api
      .get<ChatSession[]>(`/api/chat/sessions?tenant_id=${tenantId}`)
      .then(setSessions)
      .catch((error) => {
        if (error.message.includes('Not authenticated') || error.message.includes('401')) {
          clearAuthSession();
          navigate('/login', { replace: true });
          return;
        }
        message.error(error.message);
      });
  }, [navigate, tenantId]);

  const loadMessages = useCallback((id: string) => {
    return api
      .get<ChatMessage[]>(`/api/chat/sessions/${id}/messages?tenant_id=${tenantId}`)
      .then((rows) => {
        const slot = getSlot(id);
        slot.serverMessages = attachTurnIdsToServerMessages(rows, slot.realtimeMessages, slot.serverMessages);
        pruneRealtime(id);
        notifyStore();
      })
      .catch((error) => message.error(error.message));
  }, [getSlot, notifyStore, pruneRealtime, tenantId]);

  const loadTraces = useCallback((id: string) => {
    return api
      .get<TurnTraceRead[]>(`/api/chat/sessions/${id}/trace?tenant_id=${tenantId}`)
      .then((rows) => {
        rows.forEach((row) => {
          turnTraceRef.current.set(row.turn_id, {
            lines: row.lines.map((line) => ({
              id: line.id,
              kind: line.kind,
              text: line.text,
              detail: line.detail || undefined,
              state: line.state,
            })),
            startedAt: Date.parse(row.started_at) || Date.now(),
            completedAt: row.completed_at ? Date.parse(row.completed_at) : undefined,
          });
        });
        notifyTrace();
      })
      .catch((error) => message.error(error.message));
  }, [notifyTrace, tenantId]);

  const appendRealtime = useCallback((id: string, messageItem: ChatMessage) => {
    const slot = getSlot(id);
    slot.realtimeMessages = [...slot.realtimeMessages, messageItem].slice(-200);
    notifyStore();
  }, [getSlot, notifyStore]);

  const updateMessageFeedback = useCallback((
    id: string,
    messageId: string,
    rating: ChatMessage['feedback_rating'],
  ) => {
    const slot = getSlot(id);
    const update = (item: ChatMessage) => (
      item.id === messageId ? { ...item, feedback_rating: rating } : item
    );
    slot.serverMessages = slot.serverMessages.map(update);
    slot.realtimeMessages = slot.realtimeMessages.map(update);
    notifyStore();
  }, [getSlot, notifyStore]);

  const updateStreaming = useCallback((id: string, text: string, turnId?: string) => {
    const slot = getSlot(id);
    const stream = getStreamSlot(id);
    const streamId = `__streaming_${id}`;
    const streamingMessage: ChatMessage = {
      id: streamId,
      turnId: turnId || stream.turnId || undefined,
      role: 'assistant',
      content: text,
      created_at: new Date().toISOString(),
      isStreaming: true,
    };
    const index = slot.realtimeMessages.findIndex((item) => item.id === streamId);
    if (index >= 0) {
      slot.realtimeMessages = [...slot.realtimeMessages];
      slot.realtimeMessages[index] = streamingMessage;
    } else {
      slot.realtimeMessages = [...slot.realtimeMessages, streamingMessage];
    }
    notifyStore();
  }, [getSlot, getStreamSlot, notifyStore]);

  const flushStreaming = useCallback((id: string) => {
    const stream = getStreamSlot(id);
    if (stream.timer) {
      window.clearTimeout(stream.timer);
      stream.timer = null;
    }
    if (stream.accumulated) {
      updateStreaming(id, stream.accumulated);
    }
  }, [getStreamSlot, updateStreaming]);

  const finalizeStreaming = useCallback((id: string) => {
    flushStreaming(id);
    const slot = getSlot(id);
    const streamId = `__streaming_${id}`;
    const index = slot.realtimeMessages.findIndex((item) => item.id === streamId);
    if (index >= 0) {
      const streamMessage = slot.realtimeMessages[index];
      slot.realtimeMessages = [...slot.realtimeMessages];
      slot.realtimeMessages[index] = {
        ...streamMessage,
        id: `text_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        isStreaming: false,
      };
    }
    const stream = getStreamSlot(id);
    stream.accumulated = '';
    stream.turnId = null;
    notifyStore();
  }, [flushStreaming, getSlot, getStreamSlot, notifyStore]);

  useEffect(() => {
    if (!auth) {
      navigate('/login', { replace: true });
      return;
    }
    loadSessions();
  }, [auth, loadSessions, navigate]);

  useEffect(() => {
    if (!auth) return;
    api
      .get<UIConfigRead>(`/api/chat/ui-config?tenant_id=${tenantId}`)
      .then(setUiConfig)
      .catch(() => undefined);
  }, [auth, tenantId]);

  useEffect(() => {
    if (!sessionId) return;
    loadMessages(sessionId);
    loadTraces(sessionId);
  }, [loadMessages, loadTraces, sessionId]);

  useLayoutEffect(() => {
    scrollChatToBottom();
  }, [displayedMessages.length, scrollChatToBottom, sessionId, storeTick, traceTick]);

  useEffect(() => {
    if (!currentStream.loading) return;
    scrollChatToBottom();
  }, [currentStream.loading, currentStream.phase, scrollChatToBottom, storeTick, streamTick, traceTick]);

  useEffect(() => {
    return () => {
      streamRef.current.forEach((slot) => {
        if (slot.timer) {
          window.clearTimeout(slot.timer);
        }
        slot.abortController?.abort();
      });
    };
  }, []);

  async function createSession() {
    const session = await api.post<ChatSession>('/api/chat/sessions', { tenant_id: tenantId });
    getSlot(session.id);
    loadSessions();
    navigate(`/chat/${session.id}`);
  }

  function openRename(event: MouseEvent<HTMLElement>, session: ChatSession) {
    event.stopPropagation();
    setRenameSession(session);
    setRenameTitle(session.title || session.id);
  }

  async function saveRename() {
    if (!renameSession) return;
    const title = renameTitle.trim();
    if (!title) {
      message.warning('请输入会话名称');
      return;
    }
    const updated = await api.put<ChatSession>(`/api/chat/sessions/${renameSession.id}`, {
      tenant_id: tenantId,
      title,
    });
    setSessions((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    setRenameSession(null);
    setRenameTitle('');
    message.success('已重命名');
  }

  function confirmDelete(event: MouseEvent<HTMLElement>, target: ChatSession) {
    event.stopPropagation();
    Modal.confirm({
      title: '删除会话',
      content: `确定删除「${target.title || target.id}」吗？此操作会同时删除该会话的消息记录。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        const stream = getStreamSlot(target.id);
        stream.abortController?.abort();
        streamRef.current.delete(target.id);
        storeRef.current.delete(target.id);
        await api.delete(`/api/chat/sessions/${target.id}?tenant_id=${tenantId}`);
        setSessions((items) => items.filter((item) => item.id !== target.id));
        if (target.id === sessionId) {
          navigate('/chat');
        }
        message.success('已删除');
      },
    });
  }

  function abortStream() {
    if (!sessionId) return;
    const stream = getStreamSlot(sessionId);
    stream.abortController?.abort();
    stream.abortController = null;
    finalizeStreaming(sessionId);
    appendRealtime(sessionId, {
      id: `local_interrupt_${Date.now()}`,
      role: 'system',
      content: '已停止本次生成。',
      created_at: new Date().toISOString(),
    });
    stream.loading = false;
    stream.phase = '';
    notifyStream();
  }

  async function rateMessage(item: ChatMessage, rating: 'up' | 'down') {
    if (!sessionId || !canRateMessage(item)) return;
    const previous = item.feedback_rating || null;
    const next = previous === rating ? null : rating;
    updateMessageFeedback(sessionId, item.id, next);
    try {
      if (next) {
        await api.post(`/api/chat/messages/${item.id}/feedback`, {
          tenant_id: tenantId,
          rating: next,
        });
      } else {
        await api.delete(`/api/chat/messages/${item.id}/feedback?tenant_id=${tenantId}`);
      }
    } catch (error) {
      updateMessageFeedback(sessionId, item.id, previous);
      message.error(error instanceof Error ? error.message : '反馈提交失败');
    }
  }

  async function send() {
    if (!input.trim() || !sessionId) return;
    const currentSessionId = sessionId;
    const stream = getStreamSlot(currentSessionId);
    if (stream.loading) return;
    const userText = input.trim();
    const turnId = `turn_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    setInput('');
    stream.accumulated = '';
    stream.turnId = turnId;
    appendRealtime(currentSessionId, {
      id: `local_${turnId}`,
      turnId,
      role: 'user',
      content: userText,
      created_at: new Date().toISOString(),
    });
    upsertTraceLine(turnId, { id: 'thinking', kind: 'thinking', text: '正在思考', state: 'running' });
    updateStreaming(currentSessionId, '', turnId);
    stream.loading = true;
    stream.phase = '正在思考';
    notifyStream();

    const controller = new AbortController();
    stream.abortController = controller;

    try {
      await streamChatTurn({
        tenant_id: tenantId,
        session_id: currentSessionId,
        user_id: userId,
        message: userText,
        channel: 'web',
      }, (item) => {
        const eventSessionId = String(item.data.sessionId || currentSessionId);
        if (item.event === 'session_created') {
          return;
        }
        if (item.event === 'skill_state') {
          const skills = Array.isArray(item.data.currentSkills) ? item.data.currentSkills : [];
          skills
            .map((entry) => normalizeTraceSkill(entry))
            .filter((entry): entry is TraceSkill => Boolean(entry))
            .forEach((skill) => {
              const label = streamSkillLabel(item.data, skill);
              upsertTraceLine(turnId, {
                id: `skill_${skill.skillId}_${skill.state || 'active'}`,
                kind: 'skill',
                text: `${label} ${skill.name || skill.skillId}`,
                detail: skill.stepId ? `当前步骤 ${skill.stepId}` : undefined,
                state: skill.state === 'suspended' ? 'completed' : 'running',
              });
          });
          return;
        }
        if (item.event === 'general_skill_state') {
          const skillName = typeof item.data.skillName === 'string' ? item.data.skillName : '';
          const skillSlug = typeof item.data.skillSlug === 'string' ? item.data.skillSlug : '';
          upsertTraceLine(turnId, {
            id: `general_skill_${skillSlug || skillName || 'selected'}`,
            kind: 'skill',
            text: `选择通用技能 ${skillName || skillSlug || ''}`.trim(),
            detail: skillSlug || undefined,
            state: 'running',
          });
          return;
        }
        if (item.event === 'general_skill_trace') {
          const phase = typeof item.data.phase === 'string' ? item.data.phase : 'trace';
          const text = typeof item.data.message === 'string' ? item.data.message : '执行通用技能';
          const code = typeof item.data.code === 'string' ? item.data.code : '';
          const attempt = typeof item.data.attempt === 'number' || typeof item.data.attempt === 'string'
            ? String(item.data.attempt)
            : '';
          const trace = getTurnTrace(turnId);
          const sequence = trace.lines.length;
          const isOutputChunk = phase === 'stdout_chunk' || phase === 'stderr_chunk';
          const id = isOutputChunk
            ? `general_skill_trace_${phase}_${attempt || 'current'}`
            : `general_skill_trace_${phase}_${attempt || sequence}`;
          const rawDetail = typeof item.data.rationale === 'string'
            ? item.data.rationale
            : typeof item.data.text === 'string'
              ? item.data.text
              : typeof item.data.stdout_preview === 'string'
                ? item.data.stdout_preview
                : typeof item.data.stderr_preview === 'string'
                  ? item.data.stderr_preview
                  : undefined;
          const existing = trace.lines.find((line) => line.id === id);
          const detail = isOutputChunk && existing?.detail && rawDetail
            ? `${existing.detail}${rawDetail}`
            : rawDetail;
          const codePhases = new Set([
            'plan_created',
            'attempt_started',
            'running_code',
            'stdout_chunk',
            'stderr_chunk',
            'code_finished',
            'code_timeout',
          ]);
          const runningPhases = new Set([
            'planning',
            'repair_planning',
            'attempt_started',
            'running_code',
            'reflection_reviewing',
            'replying',
          ]);
          upsertTraceLine(turnId, {
            id,
            kind: codePhases.has(phase) ? 'code' : 'decision',
            text,
            detail,
            code: code || undefined,
            language: code ? 'python' : undefined,
            state: runningPhases.has(phase) ? 'running' : phase.includes('failed') || phase === 'code_timeout' ? 'failed' : 'completed',
            collapsible: Boolean(code),
          });
          return;
        }
        if (item.event === 'tool_result') {
          const tool = normalizeTraceTool(item.data);
          if (tool) {
            upsertTraceLine(turnId, {
              id: `tool_${tool.toolCallId || tool.rawToolName || tool.toolId}`,
              kind: 'tool',
              text: `${tool.isError ? '工具调用失败' : '调用工具'} ${tool.toolName}`,
              detail: toolTraceDetail(tool),
              state: tool.isError ? 'failed' : 'completed',
            });
          }
          return;
        }
        if (item.event === 'agent_loop_continued' || item.event === 'agent_loop_completed') {
          const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string'
            ? String(item.data.iteration)
            : '1';
          const targetTool = typeof item.data.target_tool_name === 'string' ? item.data.target_tool_name : '';
          upsertTraceLine(turnId, {
            id: `decision_stepping_tool_continuation_${iteration}`,
            kind: 'decision',
            text: '重新分析执行动作',
            detail: item.event === 'agent_loop_continued'
              ? (targetTool ? `决定继续调用工具 ${targetTool}` : '决定继续调用工具')
              : '判断无需继续调用工具',
            state: 'completed',
          });
          if (item.event === 'agent_loop_completed') {
            upsertTraceLine(turnId, {
              id: 'decision_responding',
              kind: 'decision',
              text: '组织回复',
              state: 'running',
            });
          }
          return;
        }
        if (item.event === 'reflection_decision') {
          const needsRetry = item.data.needs_retry === true;
          const skipped = item.data.skipped === true;
          upsertTraceLine(turnId, {
            id: 'reflection',
            kind: 'decision',
            text: skipped ? '反思已关闭' : needsRetry ? '反思后继续尝试' : '反思通过',
            detail: reflectionTraceDetail(item.data),
            state: 'completed',
          });
          return;
        }
        if (item.event === 'status') {
          const eventStream = getStreamSlot(eventSessionId);
          const phase = typeof item.data.phase === 'string' ? item.data.phase : 'thinking';
          eventStream.phase = publicStreamPhase(item.data);
          if (phase === 'tool' && typeof item.data.tool_name === 'string') {
            const toolCallId = typeof item.data.tool_call_id === 'string' ? item.data.tool_call_id : item.data.tool_name;
            upsertTraceLine(turnId, {
              id: `tool_${toolCallId}`,
              kind: 'tool',
              text: `正在调用工具 ${item.data.tool_name}`,
              state: 'running',
            });
          } else if (phase === 'routing') {
            upsertTraceLine(turnId, { id: 'decision_router', kind: 'decision', text: '判断意图', state: 'running' });
          } else if (phase === 'stepping') {
            const repairReason = typeof item.data.repair_reason === 'string' ? item.data.repair_reason : 'main';
            const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string'
              ? `_${item.data.iteration}`
              : '';
            upsertTraceLine(turnId, {
              id: `decision_stepping_${repairReason}${iteration}`,
              kind: 'decision',
              text: repairReason === 'main' ? '分析执行动作' : '重新分析执行动作',
              state: 'running',
            });
          } else if (phase === 'reflecting') {
            upsertTraceLine(turnId, { id: 'reflection', kind: 'decision', text: '正在反思', state: 'running' });
          } else if (phase === 'responding') {
            upsertTraceLine(turnId, { id: 'decision_responding', kind: 'decision', text: '组织回复', state: 'running' });
          } else if (phase !== 'received') {
            upsertTraceLine(turnId, {
              id: `decision_status_${phase}`,
              kind: 'decision',
              text: eventStream.phase,
              state: 'running',
            });
          } else {
            upsertTraceLine(turnId, { id: 'thinking', kind: 'thinking', text: '正在思考', state: 'running' });
          }
          notifyStream();
          return;
        }
        if (item.event === 'stream_delta' || item.event === 'token') {
          const piece = typeof item.data.content === 'string' ? item.data.content : '';
          if (!piece) return;
          const eventStream = getStreamSlot(eventSessionId);
          eventStream.accumulated += piece;
          if (!eventStream.timer) {
            eventStream.timer = window.setTimeout(() => {
              eventStream.timer = null;
              updateStreaming(eventSessionId, eventStream.accumulated);
            }, 100);
          }
          return;
        }
        if (item.event === 'stream_end') {
          finishTrace(turnId);
          upsertTraceLine(turnId, { id: 'thinking', kind: 'thinking', text: '已完成思考', state: 'completed' });
          finalizeStreaming(eventSessionId);
          return;
        }
        if (item.event === 'complete' || item.event === 'done') {
          const result = item.data as ChatTurnResponse;
          const userIntent = typeof result.router_decision?.user_intent === 'string' ? result.router_decision.user_intent : '';
          const decisionReason = typeof result.router_decision?.reason === 'string' ? result.router_decision.reason : '';
          if (userIntent || decisionReason) {
            upsertTraceLine(turnId, {
              id: 'decision_router',
              kind: 'decision',
              text: userIntent ? `判断意图 ${userIntent}` : '完成技能判断',
              detail: decisionReason || undefined,
              state: 'completed',
            });
          }
          finishTrace(turnId);
          upsertTraceLine(turnId, { id: 'thinking', kind: 'thinking', text: '已完成思考', state: 'completed' });
          finalizeStreaming(eventSessionId);
          setLastTurn(result);
          const eventStream = getStreamSlot(eventSessionId);
          eventStream.loading = false;
          eventStream.phase = '';
          eventStream.abortController = null;
          notifyStream();
          loadSessions();
          window.setTimeout(() => {
            loadMessages(eventSessionId);
            loadTraces(eventSessionId);
          }, 250);
        }
      }, controller.signal);
    } catch (error) {
      if (controller.signal.aborted) {
        return;
      }
      appendRealtime(currentSessionId, {
        id: `error_${Date.now()}`,
        role: 'assistant',
        content: '发送失败，请检查后端服务是否已启动。',
        created_at: new Date().toISOString(),
        isError: true,
      });
      message.error(error instanceof Error ? error.message : '发送失败');
      finishTrace(turnId, true);
      stream.loading = false;
      stream.phase = '';
      notifyStream();
    } finally {
      if (stream.abortController === controller) {
        stream.abortController = null;
        stream.loading = false;
        stream.phase = '';
        notifyStream();
      }
    }
  }

  return (
    <div className={`chat-layout ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      <aside className="session-pane">
        <div className="sidebar-head">
          <Button
            className="icon-button"
            icon={sidebarCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            aria-label={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
            onClick={toggleSidebar}
          />
          <div className="brand-block">
            <span className="brand-mark">UR</span>
            <div>
              <div className="brand-title">UltraRAG4</div>
              <div className="brand-subtitle">{auth?.user.display_name || auth?.user.username}</div>
            </div>
          </div>
          <div className="sidebar-actions">
            <Button className="icon-button primary" icon={<PlusOutlined />} onClick={createSession} />
            <Button
              className="icon-button sidebar-logout"
              icon={<LogoutOutlined />}
              onClick={() => {
                clearAuthSession();
                navigate('/login', { replace: true });
              }}
            />
          </div>
        </div>
        <div className="session-section-label">Sessions</div>
        {sessions.map((session) => {
          const itemStream = getStreamSlot(session.id);
          const sessionTitle = session.title || session.id;
          const sessionSummary = itemStream.loading
            ? itemStream.phase || '正在思考'
            : session.summary || session.last_agent_question || '新会话';
          return (
            <div
              key={session.id}
              role="button"
              tabIndex={0}
              className={`session-card ${session.id === sessionId ? 'active' : ''}`}
              onClick={() => navigate(`/chat/${session.id}`)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  navigate(`/chat/${session.id}`);
                }
              }}
            >
              <div className="session-card-content">
                <div className="session-meta">
                  <div className="session-title" title={sessionTitle}>
                    <MessageOutlined /> {sessionTitle}
                  </div>
                  <div className="session-summary" title={sessionSummary}>
                    {sessionSummary}
                  </div>
                </div>
                <div className="session-actions">
                  <Button
                    className="session-action"
                    size="small"
                    type="text"
                    icon={<EditOutlined />}
                    aria-label="重命名会话"
                    onClick={(event) => openRename(event, session)}
                  />
                  <Button
                    className="session-action danger"
                    size="small"
                    type="text"
                    icon={<DeleteOutlined />}
                    aria-label="删除会话"
                    onClick={(event) => confirmDelete(event, session)}
                  />
                </div>
              </div>
            </div>
          );
        })}
      </aside>
      <main className="chat-main">
        <div className="chat-header">
          <div>
            <Typography.Text strong>在线客服</Typography.Text>
            <div className="header-subtitle">{sessionId}</div>
          </div>
        </div>
        <div className="chat-messages" ref={chatMessagesRef}>
          {displayedMessages.length === 0 && <div className="chat-empty-state">暂无消息，发送一句开始</div>}
          <div className="message-stack">
            {displayedMessages.map((item) => {
              const turnId = item.turnId || item.id;
              const trace = item.role === 'assistant' ? turnTraceRef.current.get(turnId) : undefined;
              const visibleTrace = trace?.lines.filter((line) => traceLineAllowed(line, uiConfig)) || [];
              const summary = trace && visibleTrace.length > 0 ? traceSummary(trace, visibleTrace) : null;
              const details = traceDetails(visibleTrace);
              const expanded = expandedTraceIds.includes(turnId);
              void traceTick;
              return (
                <div key={item.id} className="message-item">
                  <div className={`message-row ${item.role} ${item.isError ? 'error' : ''}`}>
                    <div className={`bubble ${summary ? 'has-trace' : ''}`}>
                      {summary && (
                        <div className="assistant-trace">
                          <button
                            type="button"
                            className={`turn-trace-summary ${summary.state}`}
                            onClick={() => toggleTrace(turnId)}
                          >
                            <CloudSyncOutlined />
                            <span className="trace-primary-text" data-text={summary.text}>{summary.text}</span>
                            {details.length > 0 && (expanded ? <DownOutlined /> : <RightOutlined />)}
                          </button>
                          {expanded && details.length > 0 && (
                            <div className="turn-trace-details">
                              {details.map((line) => (
                                <div key={line.id} className={`turn-trace-line ${line.kind} ${line.state}`}>
                                  {line.kind === 'skill' ? (
                                    <BranchesOutlined />
                                  ) : line.kind === 'tool' ? (
                                    <ToolOutlined />
                                  ) : line.kind === 'code' ? (
                                    <TerminalTraceIcon />
                                  ) : (
                                    <CloudSyncOutlined />
                                  )}
                                    <span className="turn-trace-content">
                                      <span className="trace-primary-text" data-text={line.text}>{line.text}</span>
                                      {line.detail && <span className="turn-trace-detail">{line.detail}</span>}
                                      {line.code && (
                                        <details className="turn-trace-code-wrap" open>
                                          <summary>查看代码</summary>
                                          <pre className="turn-trace-code">
                                            <code data-language={line.language || undefined}>{line.code}</code>
                                          </pre>
                                        </details>
                                      )}
                                    </span>
                                  </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                      {item.content ? (
                        item.role === 'assistant' ? (
                          <MarkdownMessage content={item.content} />
                        ) : (
                          <div className="plain-answer">{item.content}</div>
                        )
                      ) : item.role === 'assistant' && item.isStreaming && !summary ? (
                        <span className="typing-caret" />
                      ) : null}
                      {canRateMessage(item) && (
                        <div className="message-feedback">
                          <Button
                            type="text"
                            size="small"
                            className={item.feedback_rating === 'up' ? 'active' : ''}
                            icon={<LikeOutlined />}
                            aria-label="点赞"
                            onClick={() => rateMessage(item, 'up')}
                          />
                          <Button
                            type="text"
                            size="small"
                            className={item.feedback_rating === 'down' ? 'active danger' : ''}
                            icon={<DislikeOutlined />}
                            aria-label="点踩"
                            onClick={() => rateMessage(item, 'down')}
                          />
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          {SHOW_DEBUG && lastTurn && <pre className="debug-panel">{JSON.stringify(lastTurn.session_state, null, 2)}</pre>}
        </div>
        <div className="chat-input">
          <form
            className="composer-v2"
            onSubmit={(event) => {
              event.preventDefault();
              send();
            }}
          >
            <Input.TextArea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onPressEnter={(event) => {
                const nativeEvent = event.nativeEvent as KeyboardEvent & { isComposing?: boolean };
                if (!event.shiftKey && !isComposing && !nativeEvent.isComposing && nativeEvent.keyCode !== 229) {
                  event.preventDefault();
                  send();
                }
              }}
              onCompositionStart={() => setIsComposing(true)}
              onCompositionEnd={() => window.setTimeout(() => setIsComposing(false), 0)}
              autoSize={{ minRows: 2, maxRows: 8 }}
              placeholder="告诉 Skill Agent 你想办理什么..."
            />
            <div className="composer-toolbar">
              <div className="composer-hint">Enter 发送 / Shift+Enter 换行</div>
              <Button
                type="primary"
                htmlType={currentStream.loading ? 'button' : 'submit'}
                icon={currentStream.loading ? <StopOutlined /> : <SendOutlined />}
                onClick={currentStream.loading ? abortStream : undefined}
                className={currentStream.loading ? 'stop-button' : undefined}
              />
            </div>
          </form>
        </div>
      </main>
      <Modal
        title="重命名会话"
        open={Boolean(renameSession)}
        okText="保存"
        cancelText="取消"
        onOk={saveRename}
        onCancel={() => {
          setRenameSession(null);
          setRenameTitle('');
        }}
      >
        <Input
          autoFocus
          maxLength={80}
          value={renameTitle}
          onChange={(event) => setRenameTitle(event.target.value)}
          onPressEnter={saveRename}
          placeholder="输入会话名称"
        />
      </Modal>
    </div>
  );
}
