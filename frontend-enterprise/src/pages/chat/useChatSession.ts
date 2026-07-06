import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
} from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import {
  ApiError,
  SHOW_DEBUG,
  TENANT_ID,
  api,
  isAuthError,
  streamChatTurn,
  uploadChatAttachments,
  type StreamEvent,
} from '@/api/client';
import { clearEnterpriseAuthSession, getEnterpriseAuthSession } from '@/auth';
import {
  agentResourceCount,
  employeeDisplayName,
  employeeProfile,
  visibleChatEmployees,
} from '@/employee';
import { notify } from '@/components/ui/app-toast';
import type {
  AgentProfileRead,
  ChatAttachmentRead,
  ChatMessage,
  ChatSession,
  ChatSessionEventRead,
  ChatTurnResponse,
  HumanHandoffRead,
  KnowledgeCitation,
  ModelConfigRead,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
  TurnTraceRead,
  UIConfigRead,
} from '@/types';

import {
  CHAT_STREAM_IDLE_TIMEOUT_MS,
  HIDDEN_GENERAL_SKILL_TRACE_PHASES,
  RUNNING_EVENT_RECOVERY_WINDOW_MS,
  SELECTED_AGENT_STORAGE_KEY,
  STREAM_TERMINAL_EVENTS,
  attachTurnIdsToServerMessages,
  clipboardContainsComposerImage,
  computeMergedMessages,
  draftConversationKey,
  effectiveMessageTurnId,
  eventTraceTurnId,
  explicitStreamTurnId,
  extractPastedComposerFiles,
  generalSkillTraceDetail,
  generalSkillTraceOutput,
  hasAssistantMessageForTurn,
  hasRecoverableEventProgress,
  hasServerMessageForTurn,
  isDraftConversationKey,
  isKnowledgeTracePhase,
  isMissingChatSessionError,
  isRecoverableRunningTrace,
  isScheduledSession,
  isTerminalSessionEvent,
  knowledgeResultTraceDetail,
  knowledgeTraceDetail,
  knowledgeTraceText,
  latestUserMessageForTurn,
  loadSessionReadTimes,
  mergeTraceLine,
  modelStorageKey,
  normalizeSessionEventForStream,
  normalizeTraceSkill,
  normalizeTraceTool,
  parseMessageTime,
  persistSessionReadTimes,
  publicStreamPhase,
  reflectionTraceDetail,
  routerDecisionTraceLine,
  sameRoleTurn,
  scheduledDraftForMessage,
  shouldKeepRealtimeMessage,
  stepResultTraceLine,
  streamSkillLabel,
  timestampAfterMessage,
  toolTraceDetail,
  toRequestAttachment,
  upsertStreamingTracePlaceholder,
  upsertTraceStatusPlaceholder,
} from './chatHelpers';
import {
  createEmptySlot,
  createStreamSlot,
  createTurnTrace,
  type ComposerAttachment,
  type ComposerInteractionMode,
  type SessionSlot,
  type StreamSlot,
  type TraceLine,
  type TurnTrace,
} from './chatTypes';

const CHAT_BASE_PATH = '/enterprise/chat';
// Shared with the management shell (App.tsx `ENTERPRISE_SIDEBAR_STORAGE_KEY`) so
// the collapse state is preserved when switching between 管理端 and 对话端.
// Stored as '1' (expanded) / '0' (collapsed); unset defaults to expanded.
const ENTERPRISE_SIDEBAR_STORAGE_KEY = 'ultrarag_enterprise_sidebar_expanded';

function chatSessionPath(id: string): string {
  return `${CHAT_BASE_PATH}/${id}`;
}

export type UseChatSession = ReturnType<typeof useChatSession>;

export function useChatSession() {
  const { sessionId, draftAgentId } = useParams<{ sessionId?: string; draftAgentId?: string }>();
  const navigate = useNavigate();
  const [auth] = useState(() => getEnterpriseAuthSession());
  const tenantId = auth?.user.tenant_id || TENANT_ID;
  const userId = auth?.user.id || '';
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionReadTimes, setSessionReadTimes] = useState<Record<string, string>>(() => loadSessionReadTimes(userId));
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(SELECTED_AGENT_STORAGE_KEY) || '');
  const [sessionAgentFilter, setSessionAgentFilter] = useState('all');
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [selectedModelConfigId, setSelectedModelConfigId] = useState(
    () => window.localStorage.getItem(modelStorageKey(tenantId)) || '',
  );
  const [input, setInput] = useState('');
  const [composerAttachments, setComposerAttachments] = useState<ComposerAttachment[]>([]);
  const [composerDragActive, setComposerDragActive] = useState(false);
  const [composerPlusOpen, setComposerPlusOpen] = useState(false);
  const [composerIntent, setComposerIntent] = useState<Exclude<ComposerInteractionMode, 'normal'> | null>(null);
  const [lastTurn, setLastTurn] = useState<ChatTurnResponse | null>(null);
  const [renameSession, setRenameSession] = useState<ChatSession | null>(null);
  const [renameTitle, setRenameTitle] = useState('');
  const [pendingDelete, setPendingDelete] = useState<ChatSession | null>(null);
  const [, setStoreTick] = useState(0);
  const [streamTick, setStreamTick] = useState(0);
  const [traceTick, setTraceTick] = useState(0);
  const [feedbackTick, setFeedbackTick] = useState(0);
  const [expandedTraceIds, setExpandedTraceIds] = useState<string[]>([]);
  const [collapsedTraceIds, setCollapsedTraceIds] = useState<string[]>([]);
  const [scheduledDrafts, setScheduledDrafts] = useState<Record<string, ScheduledTaskDraftRead>>({});
  const [createdScheduledTasks, setCreatedScheduledTasks] = useState<Record<string, ScheduledTaskRead>>({});
  const [dismissedDraftMessageIds, setDismissedDraftMessageIds] = useState<string[]>([]);
  const [activeCitation, setActiveCitation] = useState<KnowledgeCitation | null>(null);
  const [handoffs, setHandoffs] = useState<HumanHandoffRead[]>([]);
  const [handoffsLoading, setHandoffsLoading] = useState(false);
  const [showHandoffInbox, setShowHandoffInbox] = useState(false);
  const [handoffReplies, setHandoffReplies] = useState<Record<string, string>>({});
  const [isComposing, setIsComposing] = useState(false);
  const [runningTurn, setRunningTurn] = useState<{ sessionId: string; turnId: string } | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => (
    window.localStorage.getItem(ENTERPRISE_SIDEBAR_STORAGE_KEY) === '0'
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
  const fileInputRef = useRef<HTMLInputElement>(null);
  const isChatProgrammaticScrollRef = useRef(false);
  const isChatStickyToBottomRef = useRef(true);
  const lastActiveConversationIdRef = useRef<string | null>(null);
  const storeRef = useRef(new Map<string, SessionSlot>());
  const streamRef = useRef(new Map<string, StreamSlot>());
  const turnTraceRef = useRef(new Map<string, TurnTrace>());
  const locallyCancelledSessionIdsRef = useRef(new Set<string>());
  const scheduledEventIdsRef = useRef(new Set<string>());
  const knownSessionIdsRef = useRef(new Set<string>());
  const optimisticSessionIdsRef = useRef(new Set<string>());
  const sessionsInitializedRef = useRef(false);
  const autoOpenedSessionIdsRef = useRef(new Set<string>());
  const loadErrorNoticeRef = useRef<Record<string, number>>({});
  const uploadControllersRef = useRef(new Map<string, AbortController>());

  const notifyStore = useCallback(() => setStoreTick((value) => value + 1), []);
  const notifyStream = useCallback(() => setStreamTick((value) => value + 1), []);
  const notifyTrace = useCallback(() => setTraceTick((value) => value + 1), []);
  const notifyFeedback = useCallback(() => setFeedbackTick((value) => value + 1), []);

  const redirectToLogin = useCallback(() => {
    clearEnterpriseAuthSession();
    window.location.href = '/';
  }, []);

  useEffect(() => () => {
    uploadControllersRef.current.forEach((controller) => controller.abort());
    uploadControllersRef.current.clear();
  }, []);

  const updateChatStickiness = useCallback(() => {
    const element = chatMessagesRef.current;
    if (!element) return;
    const remainingScroll = element.scrollHeight - element.clientHeight - element.scrollTop;
    isChatStickyToBottomRef.current = remainingScroll <= 96;
  }, []);

  const finishProgrammaticChatScroll = useCallback(() => {
    window.requestAnimationFrame(() => {
      updateChatStickiness();
      isChatProgrammaticScrollRef.current = false;
    });
  }, [updateChatStickiness]);

  const handleChatMessagesScroll = useCallback(() => {
    if (isChatProgrammaticScrollRef.current) return;
    updateChatStickiness();
  }, [updateChatStickiness]);

  const notifyRequestError = useCallback((scope: string, error: unknown, fallback: string) => {
    if (isAuthError(error)) {
      redirectToLogin();
      return true;
    }
    const rawMessage = error instanceof Error ? error.message : fallback;
    const isNetworkError = rawMessage === 'Failed to fetch' || rawMessage.includes('NetworkError');
    const noticeKey = isNetworkError ? 'chat-network-error' : `chat-${scope}-error`;
    const now = Date.now();
    const lastShownAt = loadErrorNoticeRef.current[noticeKey] || 0;
    if (now - lastShownAt < 12000) return false;
    loadErrorNoticeRef.current[noticeKey] = now;
    notify.error(isNetworkError ? '接口连接失败，请检查本地服务或稍后重试' : (rawMessage || fallback), {
      id: noticeKey,
      duration: 3000,
    });
    return false;
  }, [redirectToLogin]);

  const scrollChatToBottom = useCallback((options?: { preserveShortContentTop?: boolean; force?: boolean }) => {
    const element = chatMessagesRef.current;
    if (!element) return;
    if (!options?.force && !isChatStickyToBottomRef.current) return;
    const targetScrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
    const shortContentGuard = Math.min(520, element.clientHeight * 0.72);
    isChatProgrammaticScrollRef.current = true;
    if (options?.preserveShortContentTop && targetScrollTop <= shortContentGuard) {
      element.scrollTop = 0;
      finishProgrammaticChatScroll();
      return;
    }
    window.requestAnimationFrame(() => {
      element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
      window.requestAnimationFrame(() => {
        element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
        finishProgrammaticChatScroll();
      });
    });
  }, [finishProgrammaticChatScroll]);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem(ENTERPRISE_SIDEBAR_STORAGE_KEY, next ? '0' : '1');
      return next;
    });
  }, []);

  const markSessionRead = useCallback((id: string, timestamp?: string) => {
    if (!id) return;
    const value = timestamp || new Date().toISOString();
    setSessionReadTimes((current) => {
      const currentTime = Date.parse(current[id] || '');
      const nextTime = Date.parse(value);
      if (Number.isFinite(currentTime) && Number.isFinite(nextTime) && currentTime >= nextTime) {
        return current;
      }
      const next = { ...current, [id]: value };
      persistSessionReadTimes(userId, next);
      return next;
    });
  }, [userId]);

  const activeDraftAgentId = draftAgentId || '';
  const activeConversationId = sessionId || (activeDraftAgentId ? draftConversationKey(activeDraftAgentId) : '');
  const isDraftConversation = Boolean(activeDraftAgentId && !sessionId);
  const currentSession = sessionId ? sessions.find((item) => item.id === sessionId) || null : null;
  const availableAgents = visibleChatEmployees(agents, auth?.user);
  const defaultAgent = availableAgents.find((agent) => agent.id === selectedAgentId) || availableAgents[0] || null;
  const draftAgent = activeDraftAgentId
    ? agents.find((agent) => agent.id === activeDraftAgentId) || null
    : null;
  const sessionAgent = currentSession?.agent_id
    ? agents.find((agent) => agent.id === currentSession.agent_id) || null
    : null;
  const displayedAgent = sessionAgent || draftAgent || defaultAgent;
  const displayedProfile = displayedAgent ? employeeProfile(displayedAgent) : null;
  const emptyProfileTags = displayedProfile?.workStyles.length
    ? displayedProfile.workStyles.slice(0, 3)
    : ['结构化整理', '可追溯', '可追溯'];
  const emptyRoleSummary = displayedProfile
    ? `#角色：${displayedProfile.roleName}「${displayedAgent ? employeeDisplayName(displayedAgent) : '小知'}」一名经验丰富的${displayedProfile.roleName}，`
    : '#角色：知识运营官「小知」一名经验丰富的知识运营官，';
  const emptyStats = displayedAgent
    ? [
      { label: '资料', value: agentResourceCount(displayedAgent, 'knowledge_base') },
      { label: '技能', value: agentResourceCount(displayedAgent, 'general_skill') },
      { label: 'SOP', value: agentResourceCount(displayedAgent, 'skill') },
    ]
    : [
      { label: '资料', value: 0 },
      { label: '技能', value: 0 },
      { label: 'SOP', value: 0 },
    ];
  const sessionFilterOptions = useMemo(() => {
    const counts = new Map<string, number>();
    sessions.forEach((session) => {
      if (!session.agent_id) return;
      counts.set(session.agent_id, (counts.get(session.agent_id) || 0) + 1);
    });
    const rows = Array.from(counts.keys())
      .map((agentId) => availableAgents.find((agent) => agent.id === agentId))
      .filter((agent): agent is AgentProfileRead => Boolean(agent))
      .sort((a, b) => employeeDisplayName(a).localeCompare(employeeDisplayName(b), 'zh-Hans-CN'));
    return [
      { value: 'all', label: `全部会话 · ${sessions.length}` },
      ...rows.map((agent) => ({
        value: agent.id,
        label: `${employeeDisplayName(agent)} · ${counts.get(agent.id) || 0}`,
      })),
    ];
  }, [availableAgents, sessions]);
  const visibleSidebarSessions = useMemo(() => (
    sessionAgentFilter === 'all'
      ? sessions
      : sessions.filter((session) => session.agent_id === sessionAgentFilter)
  ), [sessionAgentFilter, sessions]);
  const enabledModelConfigs = useMemo(() => modelConfigs.filter((item) => item.enabled), [modelConfigs]);
  const selectedModelConfig = (
    enabledModelConfigs.find((item) => item.id === selectedModelConfigId)
    || enabledModelConfigs.find((item) => item.is_default)
    || enabledModelConfigs[0]
    || null
  );

  const changeModelConfig = useCallback((value: string) => {
    setSelectedModelConfigId(value);
    if (value) {
      window.localStorage.setItem(modelStorageKey(tenantId), value);
    } else {
      window.localStorage.removeItem(modelStorageKey(tenantId));
    }
  }, [tenantId]);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/chat/agents?tenant_id=${tenantId}`)
      .then((rows) => {
        setAgents(rows);
        setSelectedAgentId((current) => {
          const employeeRows = visibleChatEmployees(rows, auth?.user);
          if (current && employeeRows.some((item) => item.id === current)) return current;
          const next = employeeRows[0]?.id || '';
          if (next) window.localStorage.setItem(SELECTED_AGENT_STORAGE_KEY, next);
          return next;
        });
      })
      .catch(() => setAgents([]));
  }, [auth?.user, tenantId]);

  useEffect(() => {
    if (!activeDraftAgentId) return;
    setSelectedAgentId(activeDraftAgentId);
    window.localStorage.setItem(SELECTED_AGENT_STORAGE_KEY, activeDraftAgentId);
  }, [activeDraftAgentId]);

  useEffect(() => {
    if (!auth) return;
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${tenantId}`)
      .then((rows) => {
        setModelConfigs(rows);
        setSelectedModelConfigId((current) => {
          const enabledRows = rows.filter((item) => item.enabled);
          const stored = window.localStorage.getItem(modelStorageKey(tenantId)) || '';
          if (current && enabledRows.some((item) => item.id === current)) return current;
          if (stored && enabledRows.some((item) => item.id === stored)) return stored;
          const next = enabledRows.find((item) => item.is_default)?.id || enabledRows[0]?.id || '';
          if (next) {
            window.localStorage.setItem(modelStorageKey(tenantId), next);
          }
          return next;
        });
      })
      .catch((error) => {
        if (isAuthError(error)) {
          redirectToLogin();
          return;
        }
        setModelConfigs([]);
      });
  }, [auth, redirectToLogin, tenantId]);

  const toggleTrace = useCallback((turnId: string, isExpanded = false) => {
    if (isExpanded) {
      setCollapsedTraceIds((current) => (current.includes(turnId) ? current : [...current, turnId]));
      setExpandedTraceIds((current) => current.filter((item) => item !== turnId));
      return;
    }
    setCollapsedTraceIds((current) => current.filter((item) => item !== turnId));
    setExpandedTraceIds((current) => (
      current.includes(turnId) ? current.filter((item) => item !== turnId) : [...current, turnId]
    ));
  }, []);

  const getSlot = useCallback((id: string): SessionSlot => {
    const store = storeRef.current;
    if (!store.has(id)) store.set(id, createEmptySlot());
    return store.get(id)!;
  }, []);

  const getStreamSlot = useCallback((id: string): StreamSlot => {
    const store = streamRef.current;
    if (!store.has(id)) store.set(id, createStreamSlot());
    return store.get(id)!;
  }, []);

  const getTurnTrace = useCallback((id: string): TurnTrace => {
    const store = turnTraceRef.current;
    if (!store.has(id)) store.set(id, createTurnTrace());
    return store.get(id)!;
  }, []);

  const forgetMissingSession = useCallback((id: string) => {
    knownSessionIdsRef.current.delete(id);
    optimisticSessionIdsRef.current.delete(id);
    storeRef.current.delete(id);
    streamRef.current.delete(id);
    locallyCancelledSessionIdsRef.current.delete(id);
    setSessions((current) => current.filter((item) => item.id !== id));
    setScheduledDrafts((current) => {
      if (!current[id]) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
    setCreatedScheduledTasks((current) => {
      const key = `session:${id}`;
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
    notifyStore();
    notifyStream();
  }, [notifyStore, notifyStream]);

  const upsertOptimisticSession = useCallback((session: ChatSession) => {
    optimisticSessionIdsRef.current.add(session.id);
    knownSessionIdsRef.current.add(session.id);
    setSessions((current) => {
      const existing = current.find((item) => item.id === session.id);
      const nextSession = existing
        ? { ...existing, ...session, updated_at: session.updated_at || existing.updated_at }
        : session;
      return [nextSession, ...current.filter((item) => item.id !== session.id)];
    });
  }, []);

  const applySessionTitleSummary = useCallback((targetSessionId: string, rawTitle: unknown) => {
    const title = typeof rawTitle === 'string' ? rawTitle.trim() : '';
    if (!targetSessionId || !title) return;
    setSessions((current) => current.map((item) => (item.id === targetSessionId ? { ...item, title } : item)));
  }, []);

  const upsertTraceLine = useCallback((turnId: string, line: TraceLine) => {
    const trace = getTurnTrace(turnId);
    const nextLine = trace.completedAt && line.state === 'running'
      ? { ...line, state: 'completed' as const }
      : line;
    const index = trace.lines.findIndex((item) => item.id === line.id);
    if (index >= 0) {
      trace.lines = [...trace.lines];
      trace.lines[index] = mergeTraceLine(trace.lines[index], nextLine);
    } else {
      trace.lines = [...trace.lines, nextLine].slice(-80);
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

  const clearStreamSlot = useCallback((id: string, removeStreamingMessage = false) => {
    const stream = getStreamSlot(id);
    if (stream.timer) {
      window.clearTimeout(stream.timer);
      stream.timer = null;
    }
    stream.loading = false;
    stream.phase = '';
    stream.accumulated = '';
    stream.turnId = null;
    stream.abortController = null;
    if (removeStreamingMessage) {
      const slot = getSlot(id);
      const streamId = `__streaming_${id}`;
      const nextRealtime = slot.realtimeMessages.filter((item) => item.id !== streamId);
      if (nextRealtime.length !== slot.realtimeMessages.length) {
        slot.realtimeMessages = nextRealtime;
        notifyStore();
      }
    }
    notifyStream();
  }, [getSlot, getStreamSlot, notifyStore, notifyStream]);

  const rekeyTurnTrace = useCallback((fromTurnId: string, toTurnId: string) => {
    if (!fromTurnId || !toTurnId || fromTurnId === toTurnId) return;
    const source = turnTraceRef.current.get(fromTurnId);
    if (!source) return;
    const target = turnTraceRef.current.get(toTurnId);
    if (!target) {
      turnTraceRef.current.set(toTurnId, source);
    } else {
      const nextLines = [...target.lines];
      source.lines.forEach((line) => {
        const index = nextLines.findIndex((item) => item.id === line.id);
        if (index >= 0) {
          nextLines[index] = mergeTraceLine(nextLines[index], line);
        } else {
          nextLines.push(line);
        }
      });
      target.lines = nextLines.slice(-80);
      target.startedAt = Math.min(target.startedAt, source.startedAt);
      target.completedAt = target.completedAt || source.completedAt;
    }
    turnTraceRef.current.delete(fromTurnId);
    setExpandedTraceIds((current) => {
      if (!current.includes(fromTurnId)) return current;
      const next = current.filter((item) => item !== fromTurnId);
      return next.includes(toTurnId) ? next : [...next, toTurnId];
    });
    setCollapsedTraceIds((current) => (
      current.includes(fromTurnId) ? current.map((item) => (item === fromTurnId ? toTurnId : item)) : current
    ));
    notifyTrace();
  }, [notifyTrace]);

  const bindRealtimeUserToServerMessage = useCallback((id: string, turnId: string, serverMessageId: string) => {
    if (!turnId || !serverMessageId) return;
    const slot = getSlot(id);
    const stream = getStreamSlot(id);
    let changed = false;
    slot.realtimeMessages = slot.realtimeMessages.map((item) => {
      if (item.turnId !== turnId) return item;
      changed = true;
      return {
        ...item,
        id: item.role === 'user' ? serverMessageId : item.id,
        serverMessageId: item.role === 'user' ? serverMessageId : item.serverMessageId,
        turnId: serverMessageId,
      };
    });
    slot.serverMessages = slot.serverMessages.map((item) => {
      if (item.id !== serverMessageId) return item;
      changed = true;
      return { ...item, turnId: serverMessageId };
    });
    if (stream.turnId === turnId) {
      stream.turnId = serverMessageId;
      changed = true;
    }
    if (stream.cancelledTurnId === turnId) {
      stream.cancelledTurnId = serverMessageId;
      changed = true;
    }
    setRunningTurn((current) => (
      current?.sessionId === id && current.turnId === turnId
        ? { sessionId: id, turnId: serverMessageId }
        : current
    ));
    rekeyTurnTrace(turnId, serverMessageId);
    if (changed) notifyStore();
  }, [getSlot, getStreamSlot, notifyStore, rekeyTurnTrace]);

  const displayedMessages = useMemo(() => {
    if (!activeConversationId) return [];
    void feedbackTick;
    void streamTick;
    void traceTick;
    return computeMergedMessages(getSlot(activeConversationId), getStreamSlot(activeConversationId).turnId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeConversationId, feedbackTick, getSlot, getStreamSlot, streamTick]);

  const currentStream = useMemo(() => {
    void streamTick;
    return activeConversationId ? getStreamSlot(activeConversationId) : createStreamSlot();
  }, [activeConversationId, getStreamSlot, streamTick]);
  const currentTraceRunning = Boolean(
    currentStream.loading
    || (activeConversationId && runningTurn?.sessionId === activeConversationId),
  );
  const activeRunningTraceId = currentTraceRunning
    ? (currentStream.turnId || (runningTurn?.sessionId === activeConversationId ? runningTurn.turnId : '') || '')
    : '';
  const hasRunningDisplayedTrace = useMemo(() => {
    void traceTick;
    return displayedMessages.some((item) => {
      if (item.role !== 'assistant' || !item.isStreaming) return false;
      const trace = turnTraceRef.current.get(item.turnId || item.id);
      return Boolean(trace?.lines.some((line) => line.state === 'running'));
    });
  }, [displayedMessages, traceTick]);
  const currentSessionRunning = Boolean(
    currentStream.loading
    || (activeConversationId && runningTurn?.sessionId === activeConversationId)
    || (currentStream.loading && hasRunningDisplayedTrace),
  );
  const readyComposerAttachments = useMemo(
    () => composerAttachments.filter((item) => item.uploadStatus === 'ready'),
    [composerAttachments],
  );
  const uploadingComposerAttachment = composerAttachments.some((item) => item.uploadStatus === 'uploading');
  const composerActive = Boolean(
    input.trim()
    || composerAttachments.length > 0
    || displayedMessages.length > 0
    || currentSessionRunning,
  );
  const showComposerAvatar = Boolean(activeConversationId && displayedProfile);
  const isCurrentStreamingTrace = useCallback((traceTurnId: string, item?: ChatMessage) => Boolean(
    currentTraceRunning
    && traceTurnId
    && (
      traceTurnId === activeRunningTraceId
      || traceTurnId === currentStream.turnId
      || (item?.role === 'assistant' && item.isStreaming)
    )
  ), [activeRunningTraceId, currentStream.turnId, currentTraceRunning]);

  useEffect(() => {
    if (sessionAgentFilter === 'all') return;
    if (!sessionFilterOptions.some((item) => item.value === sessionAgentFilter)) {
      setSessionAgentFilter('all');
    }
  }, [sessionAgentFilter, sessionFilterOptions]);
  const currentScheduledDraft = activeConversationId ? scheduledDrafts[activeConversationId] : undefined;
  const hasVisibleMessageScheduledDraft = displayedMessages.some((item) => (
    item.role === 'assistant'
    && !dismissedDraftMessageIds.includes(item.id)
    && Boolean(scheduledDraftForMessage(item))
  ));

  const loadSessions = useCallback(() => {
    api
      .get<ChatSession[]>(`/api/chat/sessions?tenant_id=${tenantId}`)
      .then((rows) => {
        const previousIds = new Set(knownSessionIdsRef.current);
        const initialized = sessionsInitializedRef.current;
        if (!initialized) {
          const initialReads = loadSessionReadTimes(userId);
          const nextReads = { ...initialReads };
          if (Object.keys(initialReads).length === 0) {
            rows.forEach((row) => {
              nextReads[row.id] = row.updated_at || new Date().toISOString();
            });
          }
          setSessionReadTimes(nextReads);
          persistSessionReadTimes(userId, nextReads);
          sessionsInitializedRef.current = true;
        }
        rows.forEach((row) => {
          knownSessionIdsRef.current.add(row.id);
          optimisticSessionIdsRef.current.delete(row.id);
        });
        const persistedIds = new Set(rows.map((row) => row.id));
        setSessions((current) => [
          ...current.filter((row) => optimisticSessionIdsRef.current.has(row.id) && !persistedIds.has(row.id)),
          ...rows,
        ]);
        if (!initialized) return;
        const newScheduledSession = rows.find((row) => (
          !previousIds.has(row.id)
          && isScheduledSession(row)
          && !autoOpenedSessionIdsRef.current.has(row.id)
        ));
        if (!newScheduledSession) return;
        autoOpenedSessionIdsRef.current.add(newScheduledSession.id);
        if (!input.trim()) {
          getSlot(newScheduledSession.id);
          navigate(chatSessionPath(newScheduledSession.id));
        }
      })
      .catch((error) => {
        notifyRequestError('sessions', error, '会话加载失败');
      })
      .finally(() => {
        setSessionsLoading(false);
      });
  }, [getSlot, input, navigate, notifyRequestError, tenantId, userId]);

  const loadMessages = useCallback((id: string) => {
    return api
      .get<ChatMessage[]>(`/api/chat/sessions/${id}/messages?tenant_id=${tenantId}`)
      .then((rows) => {
        const slot = getSlot(id);
        slot.serverMessages = attachTurnIdsToServerMessages(rows, slot.realtimeMessages);
        const stream = getStreamSlot(id);
        if (stream.loading) {
          const latestUserTime = Math.max(
            0,
            ...slot.serverMessages
              .filter((messageItem) => messageItem.role === 'user')
              .map((messageItem) => parseMessageTime(messageItem.created_at)),
          );
          const hasCompletedAssistant = slot.serverMessages.some((messageItem) => (
            messageItem.role === 'assistant'
            && (
              (stream.turnId && messageItem.turnId === stream.turnId)
              || parseMessageTime(messageItem.created_at) > latestUserTime
            )
          ));
          if (hasCompletedAssistant) {
            clearStreamSlot(id, true);
          }
        }
        pruneRealtime(id);
        notifyStore();
      })
      .catch((error) => {
        if (isMissingChatSessionError(error)) {
          forgetMissingSession(id);
          loadSessions();
          return;
        }
        notifyRequestError('messages', error, '消息加载失败');
      });
  }, [clearStreamSlot, forgetMissingSession, getSlot, getStreamSlot, loadSessions, notifyRequestError, notifyStore, pruneRealtime, tenantId]);

  const loadTraces = useCallback((id: string) => {
    return api
      .get<TurnTraceRead[]>(`/api/chat/sessions/${id}/trace?tenant_id=${tenantId}`)
      .then((rows) => {
        const slot = getSlot(id);
        const stream = getStreamSlot(id);
        const locallyCancelled = locallyCancelledSessionIdsRef.current.has(id);
        let recoveredRunningTurnId = '';
        let storeChanged = false;
        let streamChanged = false;
        rows.forEach((row) => {
          const hasFinalAssistant = hasAssistantMessageForTurn(slot, row.turn_id);
          const recoverableRunningTrace = !locallyCancelled && isRecoverableRunningTrace(row) && !hasFinalAssistant;
          const staleOpenTrace = !row.completed_at && row.lines.length > 0 && !recoverableRunningTrace && !hasFinalAssistant;
          const traceLines = row.lines.map((line) => ({
            id: line.id,
            kind: line.kind,
            text: line.text,
            detail: line.detail || undefined,
            code: line.code || undefined,
            language: line.language || undefined,
            output: line.output || undefined,
            outputLanguage: line.outputLanguage || undefined,
            outputTitle: line.outputTitle || undefined,
            state: staleOpenTrace && line.state === 'running' ? 'completed' as const : line.state,
            collapsible: Boolean(line.collapsible || line.code || line.output),
          }));
          turnTraceRef.current.set(row.turn_id, {
            lines: traceLines,
            startedAt: parseMessageTime(row.started_at) || Date.now(),
            completedAt: row.completed_at
              ? parseMessageTime(row.completed_at)
              : staleOpenTrace
                ? parseMessageTime(row.started_at) || Date.now()
                : undefined,
          });
          if (recoverableRunningTrace) {
            recoveredRunningTurnId = row.turn_id;
          } else if ((row.completed_at || staleOpenTrace) && row.lines.length > 0 && !hasFinalAssistant) {
            storeChanged = upsertTraceStatusPlaceholder(slot, id, row.turn_id) || storeChanged;
            if (staleOpenTrace) {
              setExpandedTraceIds((expanded) => (expanded.includes(row.turn_id) ? expanded : [...expanded, row.turn_id]));
              setCollapsedTraceIds((collapsed) => collapsed.filter((item) => item !== row.turn_id));
            }
          }
        });
        if (recoveredRunningTurnId) {
          streamChanged = streamChanged || stream.turnId !== recoveredRunningTurnId || !stream.loading || !stream.phase;
          stream.turnId = recoveredRunningTurnId;
          stream.loading = true;
          stream.phase = stream.phase || '正在思考';
          storeChanged = upsertStreamingTracePlaceholder(slot, id, recoveredRunningTurnId) || storeChanged;
          setExpandedTraceIds((expanded) => (expanded.includes(recoveredRunningTurnId) ? expanded : [...expanded, recoveredRunningTurnId]));
          setCollapsedTraceIds((collapsed) => collapsed.filter((item) => item !== recoveredRunningTurnId));
        } else if (stream.turnId && !stream.loading) {
          streamChanged = true;
          stream.turnId = null;
          stream.phase = '';
        }
        setRunningTurn((current) => {
          if (recoveredRunningTurnId) {
            const next = { sessionId: id, turnId: recoveredRunningTurnId };
            return current?.sessionId === next.sessionId && current.turnId === next.turnId ? current : next;
          }
          return current?.sessionId === id ? null : current;
        });
        if (storeChanged) notifyStore();
        if (storeChanged || streamChanged) notifyStream();
        notifyTrace();
      })
      .catch((error) => {
        if (isMissingChatSessionError(error)) {
          forgetMissingSession(id);
          loadSessions();
          return;
        }
        notifyRequestError('trace', error, '轨迹加载失败');
      });
  }, [forgetMissingSession, getSlot, getStreamSlot, loadSessions, notifyRequestError, notifyStore, notifyStream, notifyTrace, tenantId]);

  const loadHandoffs = useCallback(() => {
    if (!auth) return Promise.resolve();
    setHandoffsLoading(true);
    return api
      .get<HumanHandoffRead[]>(`/api/chat/handoffs?tenant_id=${tenantId}&status=pending`)
      .then(setHandoffs)
      .catch((error) => {
        notifyRequestError('handoffs', error, '待回答加载失败');
      })
      .finally(() => setHandoffsLoading(false));
  }, [auth, notifyRequestError, tenantId]);

  const submitHandoffReply = useCallback((handoff: HumanHandoffRead) => {
    const reply = (handoffReplies[handoff.id] || '').trim();
    if (!reply) {
      notify.warning('请输入回复内容');
      return;
    }
    api
      .post<HumanHandoffRead>(`/api/chat/handoffs/${handoff.id}/reply`, { tenant_id: tenantId, reply })
      .then(() => {
        notify.success('已回复，原会话会继续执行');
        setHandoffs((rows) => rows.filter((item) => item.id !== handoff.id));
        setHandoffReplies((prev) => {
          const next = { ...prev };
          delete next[handoff.id];
          return next;
        });
        loadSessions();
        getSlot(handoff.session_id);
        void loadMessages(handoff.session_id);
        void loadTraces(handoff.session_id);
      })
      .catch((error) => {
        if (isAuthError(error)) {
          redirectToLogin();
          return;
        }
        notify.error(error instanceof Error ? error.message : '回复失败');
      });
  }, [getSlot, handoffReplies, loadMessages, loadSessions, loadTraces, redirectToLogin, tenantId]);

  const appendRealtime = useCallback((id: string, messageItem: ChatMessage) => {
    const slot = getSlot(id);
    const existingIndex = messageItem.role === 'assistant'
      ? slot.realtimeMessages.findIndex((item) => sameRoleTurn(item, messageItem))
      : -1;
    if (existingIndex >= 0) {
      const nextMessages = [...slot.realtimeMessages];
      nextMessages[existingIndex] = {
        ...nextMessages[existingIndex],
        ...messageItem,
        id: messageItem.id,
        created_at: nextMessages[existingIndex].created_at || messageItem.created_at,
      };
      slot.realtimeMessages = nextMessages.slice(-200);
    } else {
      slot.realtimeMessages = [...slot.realtimeMessages, messageItem].slice(-200);
    }
    notifyStore();
  }, [getSlot, notifyStore]);

  const updateMessageFeedback = useCallback((
    id: string,
    messageId: string,
    rating: ChatMessage['feedback_rating'],
  ) => {
    const slot = getSlot(id);
    const update = (item: ChatMessage) => (item.id === messageId ? { ...item, feedback_rating: rating } : item);
    slot.serverMessages = slot.serverMessages.map(update);
    slot.realtimeMessages = slot.realtimeMessages.map(update);
    notifyFeedback();
  }, [getSlot, notifyFeedback]);

  const updateStreaming = useCallback((id: string, text: string, turnId?: string, allowEmpty = false) => {
    const slot = getSlot(id);
    const stream = getStreamSlot(id);
    const streamId = `__streaming_${id}`;
    const index = slot.realtimeMessages.findIndex((item) => item.id === streamId);
    if (!text && index < 0 && !allowEmpty) return;
    const previousMessage = index >= 0 ? slot.realtimeMessages[index] : undefined;
    const activeTurnId = turnId || stream.turnId || undefined;
    const previousCreatedAt = previousMessage && previousMessage.turnId === activeTurnId
      ? previousMessage.created_at
      : undefined;
    const streamingMessage: ChatMessage = {
      id: streamId,
      turnId: activeTurnId,
      role: 'assistant',
      content: text,
      created_at: previousCreatedAt || timestampAfterMessage(latestUserMessageForTurn(slot, activeTurnId)),
      isStreaming: true,
    };
    if (activeTurnId) {
      slot.realtimeMessages = slot.realtimeMessages.filter((item) => (
        item.id === streamId
        || !(
          item.role === 'assistant'
          && effectiveMessageTurnId(item) === activeTurnId
          && !hasServerMessageForTurn(item, slot.serverMessages)
        )
      ));
    }
    const streamIndex = slot.realtimeMessages.findIndex((item) => item.id === streamId);
    if (streamIndex >= 0) {
      const previous = slot.realtimeMessages[streamIndex];
      if (
        previous.turnId === streamingMessage.turnId
        && previous.content === streamingMessage.content
        && previous.isStreaming === streamingMessage.isStreaming
      ) {
        return;
      }
      slot.realtimeMessages = [...slot.realtimeMessages];
      slot.realtimeMessages[streamIndex] = streamingMessage;
    } else {
      slot.realtimeMessages = [...slot.realtimeMessages, streamingMessage];
    }
    notifyStore();
  }, [getSlot, getStreamSlot, notifyStore]);

  const ensureStreamingTraceMessage = useCallback((id: string, turnId?: string | null) => {
    if (!turnId) return;
    const stream = getStreamSlot(id);
    if (!stream.loading) return;
    updateStreaming(id, stream.accumulated || '', turnId, true);
  }, [getStreamSlot, updateStreaming]);

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
      const streamTurnId = effectiveMessageTurnId(streamMessage);
      if (streamTurnId && hasServerMessageForTurn(streamMessage, slot.serverMessages)) {
        slot.realtimeMessages = slot.realtimeMessages.filter((item) => item.id !== streamId);
      } else {
        const finalMessage: ChatMessage = {
          ...streamMessage,
          id: streamTurnId ? `__final_${id}_${streamTurnId}` : `text_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
          isStreaming: false,
        };
        const nextMessages = slot.realtimeMessages.filter((item, itemIndex) => (
          itemIndex === index
          || !(streamTurnId && item.role === 'assistant' && effectiveMessageTurnId(item) === streamTurnId)
        ));
        const nextIndex = nextMessages.findIndex((item) => item.id === streamId);
        if (nextIndex >= 0) {
          nextMessages[nextIndex] = finalMessage;
        } else {
          nextMessages.push(finalMessage);
        }
        slot.realtimeMessages = nextMessages;
      }
    }
    const stream = getStreamSlot(id);
    stream.accumulated = '';
    stream.turnId = null;
    notifyStore();
  }, [flushStreaming, getSlot, getStreamSlot, notifyStore]);

  useEffect(() => {
    if (!auth) {
      redirectToLogin();
      return;
    }
    loadSessions();
  }, [auth, loadSessions, redirectToLogin]);

  useEffect(() => {
    if (!auth) return;
    const timer = window.setInterval(loadSessions, 2500);
    return () => window.clearInterval(timer);
  }, [auth, loadSessions]);

  useEffect(() => {
    if (!auth) return;
    void loadHandoffs();
    const timer = window.setInterval(() => {
      void loadHandoffs();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [auth, loadHandoffs]);

  useEffect(() => {
    if (!auth) return;
    api
      .get<UIConfigRead>(`/api/chat/ui-config?tenant_id=${tenantId}`)
      .then(setUiConfig)
      .catch(() => undefined);
  }, [auth, tenantId]);

  useEffect(() => {
    if (!sessionId) return;
    void loadMessages(sessionId).finally(() => {
      void loadTraces(sessionId);
    });
  }, [loadMessages, loadTraces, sessionId]);

  useEffect(() => {
    if (!sessionId || runningTurn?.sessionId !== sessionId) return;
    const timer = window.setInterval(() => {
      if (getStreamSlot(sessionId).abortController) return;
      void loadMessages(sessionId).finally(() => {
        void loadTraces(sessionId);
      });
    }, 1500);
    return () => window.clearInterval(timer);
  }, [getStreamSlot, loadMessages, loadTraces, runningTurn?.sessionId, sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const session = sessions.find((item) => item.id === sessionId);
    if (session) {
      markSessionRead(sessionId, session.updated_at);
    }
  }, [markSessionRead, sessionId, sessions]);

  useLayoutEffect(() => {
    const conversationChanged = activeConversationId !== lastActiveConversationIdRef.current;
    lastActiveConversationIdRef.current = activeConversationId;
    if (conversationChanged) {
      isChatStickyToBottomRef.current = true;
      scrollChatToBottom({ preserveShortContentTop: true, force: true });
      return;
    }
    scrollChatToBottom();
  }, [activeConversationId, displayedMessages.length, scrollChatToBottom, traceTick]);

  useEffect(() => {
    if (!currentTraceRunning) return;
    scrollChatToBottom();
  }, [currentTraceRunning, currentStream.phase, scrollChatToBottom, streamTick, traceTick]);

  useEffect(() => () => {
    streamRef.current.forEach((slot) => {
      if (slot.timer) window.clearTimeout(slot.timer);
    });
  }, []);

  const openRename = useCallback((session: ChatSession) => {
    setRenameSession(session);
    setRenameTitle(session.title || session.summary || session.last_agent_question || '');
  }, []);

  const saveRename = useCallback(async () => {
    if (!renameSession) return;
    const title = renameTitle.trim();
    if (!title) {
      notify.warning('请输入会话名称');
      return;
    }
    const updated = await api.put<ChatSession>(`/api/chat/sessions/${renameSession.id}`, {
      tenant_id: tenantId,
      title,
    });
    setSessions((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    setRenameSession(null);
    setRenameTitle('');
    notify.success('已重命名');
  }, [renameSession, renameTitle, tenantId]);

  const requestDelete = useCallback((session: ChatSession) => {
    setPendingDelete(session);
  }, []);

  const confirmDeleteSession = useCallback(async () => {
    const target = pendingDelete;
    if (!target) return;
    setPendingDelete(null);
    const stream = getStreamSlot(target.id);
    stream.abortController?.abort();
    streamRef.current.delete(target.id);
    storeRef.current.delete(target.id);
    try {
      await api.delete(`/api/chat/sessions/${target.id}?tenant_id=${tenantId}`);
      forgetMissingSession(target.id);
      if (target.id === sessionId) {
        navigate(CHAT_BASE_PATH);
      }
      notify.success('已删除');
    } catch (error) {
      if (isAuthError(error)) {
        redirectToLogin();
        return;
      }
      notify.error(error instanceof Error ? error.message : '删除失败');
    }
  }, [forgetMissingSession, getStreamSlot, navigate, pendingDelete, redirectToLogin, sessionId, tenantId]);

  const abortStream = useCallback(() => {
    if (!activeConversationId) return;
    const stream = getStreamSlot(activeConversationId);
    if (!stream.loading && stream.cancelledTurnId) return;
    const cancelledTurnId = stream.turnId || (
      runningTurn?.sessionId === activeConversationId ? runningTurn.turnId : null
    );
    const controller = stream.abortController;
    locallyCancelledSessionIdsRef.current.add(activeConversationId);
    const cancelRequest = cancelledTurnId && !isDraftConversationKey(activeConversationId)
      ? api.postKeepalive(`/api/chat/sessions/${activeConversationId}/cancel`, {
          tenant_id: tenantId,
          turn_id: cancelledTurnId,
        }).catch(() => undefined)
      : Promise.resolve();
    if (cancelledTurnId) {
      upsertTraceLine(cancelledTurnId, { id: 'generation_stopped', kind: 'decision', text: '已停止生成', state: 'failed' });
      finishTrace(cancelledTurnId, true);
    }
    clearStreamSlot(activeConversationId, false);
    if (cancelledTurnId) {
      upsertTraceStatusPlaceholder(getSlot(activeConversationId), activeConversationId, cancelledTurnId);
      notifyStore();
    }
    const stoppedStream = getStreamSlot(activeConversationId);
    stoppedStream.cancelledTurnId = cancelledTurnId;
    setRunningTurn((current) => (current?.sessionId === activeConversationId ? null : current));
    notifyStream();
    const abortAfterCancel = () => {
      controller?.abort();
    };
    if (controller) {
      const cancelDeadline = new Promise<void>((resolve) => {
        window.setTimeout(resolve, 300);
      });
      void Promise.race([cancelRequest, cancelDeadline]).then(abortAfterCancel, abortAfterCancel);
    }
  }, [activeConversationId, clearStreamSlot, finishTrace, getSlot, getStreamSlot, notifyStore, notifyStream, runningTurn, tenantId, upsertTraceLine]);

  const rateMessage = useCallback(async (item: ChatMessage, rating: 'up' | 'down') => {
    if (!sessionId) return;
    const previous = item.feedback_rating || null;
    const next = previous === rating ? null : rating;
    updateMessageFeedback(sessionId, item.id, next);
    try {
      if (next) {
        await api.post(`/api/chat/messages/${item.id}/feedback`, { tenant_id: tenantId, rating: next });
      } else {
        await api.delete(`/api/chat/messages/${item.id}/feedback?tenant_id=${tenantId}`);
      }
    } catch (error) {
      updateMessageFeedback(sessionId, item.id, previous);
      if (isAuthError(error)) {
        redirectToLogin();
        return;
      }
      notify.error(error instanceof Error ? error.message : '反馈提交失败');
    }
  }, [redirectToLogin, sessionId, tenantId, updateMessageFeedback]);

  const confirmScheduledTask = useCallback(async (draft: ScheduledTaskDraftRead, draftKey?: string) => {
    if (!sessionId) return;
    try {
      const saved = await api.post<ScheduledTaskRead>('/api/chat/scheduled-tasks', {
        tenant_id: tenantId,
        agent_id: draft.agent_id,
        title: draft.title,
        prompt: draft.prompt,
        description: draft.description,
        schedule_type: draft.schedule_type,
        schedule: draft.schedule,
        timezone: draft.timezone || 'Asia/Shanghai',
        rrule: draft.rrule,
        status: 'active',
        concurrency_policy: 'forbid',
        misfire_policy: 'coalesce',
        source_session_id: draft.source_session_id || sessionId,
        metadata: {
          created_from: 'chat_confirmation',
          confidence: draft.confidence,
          reason: draft.reason,
        },
      });
      const createdKey = draftKey || `session:${sessionId}`;
      setCreatedScheduledTasks((prev) => ({ ...prev, [createdKey]: saved }));
      setScheduledDrafts((prev) => {
        const next = { ...prev };
        delete next[sessionId];
        return next;
      });
      notify.success(`定时任务「${saved.title}」已启用`);
    } catch (error) {
      if (isAuthError(error)) {
        redirectToLogin();
        return;
      }
      notify.error(error instanceof Error ? error.message : '创建定时任务失败');
    }
  }, [redirectToLogin, sessionId, tenantId]);

  const dismissScheduledTaskDraft = useCallback((messageId?: string) => {
    if (!sessionId) return;
    setScheduledDrafts((prev) => {
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
    if (messageId) {
      setDismissedDraftMessageIds((prev) => (prev.includes(messageId) ? prev : [...prev, messageId]));
    }
  }, [sessionId]);

  const handleStreamEvent = useCallback((item: StreamEvent, baseSessionId: string, turnId: string) => {
    const eventSessionId = String(item.data.sessionId || baseSessionId);
    const traceTurnId = explicitStreamTurnId(item.data, turnId);
    if (item.event === 'session_created') return;
    if (item.event === 'session_title_summarized') {
      applySessionTitleSummary(eventSessionId, item.data.title);
      return;
    }
    if (item.event === 'scheduled_task_draft') {
      const draft = item.data as unknown as ScheduledTaskDraftRead;
      if (draft.should_create) {
        setScheduledDrafts((prev) => ({ ...prev, [eventSessionId]: draft }));
      }
      return;
    }
    if (traceTurnId && !STREAM_TERMINAL_EVENTS.has(item.event)) {
      const eventStream = getStreamSlot(eventSessionId);
      if (eventStream.turnId !== traceTurnId) {
        eventStream.turnId = traceTurnId;
      }
      ensureStreamingTraceMessage(eventSessionId, traceTurnId);
    }
    if (item.event === 'router_decision') {
      upsertTraceLine(traceTurnId, routerDecisionTraceLine(item.data));
      return;
    }
    if (item.event === 'step_result') {
      upsertTraceLine(traceTurnId, stepResultTraceLine(item.data));
      return;
    }
    if (item.event === 'skill_state') {
      const skills = Array.isArray(item.data.currentSkills) ? item.data.currentSkills : [];
      skills
        .map((entry) => normalizeTraceSkill(entry))
        .filter((entry): entry is NonNullable<typeof entry> => Boolean(entry))
        .forEach((skill) => {
          const label = streamSkillLabel(item.data, skill);
          upsertTraceLine(traceTurnId, {
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
      upsertTraceLine(traceTurnId, {
        id: `general_skill_${skillSlug || skillName || 'selected'}`,
        kind: 'skill',
        text: `选择技能 ${skillName || skillSlug || ''}`.trim(),
        detail: skillSlug || undefined,
        state: 'running',
      });
      return;
    }
    if (item.event === 'general_skill_trace') {
      const phase = typeof item.data.phase === 'string' ? item.data.phase : 'trace';
      if (HIDDEN_GENERAL_SKILL_TRACE_PHASES.has(phase)) {
        notifyStream();
        return;
      }
      const text = typeof item.data.message === 'string' ? item.data.message : '执行技能';
      const code = typeof item.data.code === 'string' ? item.data.code : '';
      const runtime = typeof item.data.runtime === 'string' ? item.data.runtime : '';
      const attempt = typeof item.data.attempt === 'number' || typeof item.data.attempt === 'string'
        ? String(item.data.attempt)
        : '';
      const trace = getTurnTrace(traceTurnId);
      const sequence = trace.lines.length;
      const isOutputChunk = phase === 'stdout_chunk' || phase === 'stderr_chunk';
      const id = isOutputChunk
        ? `general_skill_trace_${phase}_${attempt || 'current'}`
        : `general_skill_trace_${phase}_${attempt || sequence}`;
      const rawDetail = generalSkillTraceDetail(item.data, phase);
      const existing = trace.lines.find((line) => line.id === id);
      const previousOutput = existing?.output || existing?.detail || '';
      const detail = isOutputChunk && previousOutput && rawDetail ? `${previousOutput}${rawDetail}` : rawDetail;
      const outputInfo = generalSkillTraceOutput(item.data, phase, detail);
      const codePhases = new Set(['plan_created', 'attempt_started', 'running_code', 'stdout_chunk', 'stderr_chunk', 'code_finished', 'code_timeout']);
      const runningPhases = new Set(['planning', 'repair_planning', 'attempt_started', 'running_code', 'reflection_reviewing', 'replying']);
      upsertTraceLine(traceTurnId, {
        id,
        kind: codePhases.has(phase) ? 'code' : 'decision',
        text,
        detail: outputInfo.output ? undefined : detail,
        code: code || undefined,
        language: code ? (runtime === 'bash' ? 'bash' : 'python') : undefined,
        output: outputInfo.output,
        outputLanguage: outputInfo.language,
        outputTitle: outputInfo.title,
        state: runningPhases.has(phase) ? 'running' : phase.includes('failed') || phase === 'code_timeout' ? 'failed' : 'completed',
        collapsible: Boolean(code || outputInfo.output),
      });
      return;
    }
    if (item.event === 'knowledge_result') {
      upsertTraceLine(traceTurnId, {
        id: 'knowledge_lookup',
        kind: 'knowledge',
        text: '读取知识库',
        detail: knowledgeResultTraceDetail(item.data),
        state: 'completed',
      });
      return;
    }
    if (item.event === 'tool_result') {
      const tool = normalizeTraceTool(item.data);
      if (tool) {
        upsertTraceLine(traceTurnId, {
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
      const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string' ? String(item.data.iteration) : '1';
      const targetTool = typeof item.data.target_tool_name === 'string' ? item.data.target_tool_name : '';
      upsertTraceLine(traceTurnId, {
        id: `decision_stepping_tool_continuation_${iteration}`,
        kind: 'decision',
        text: '重新分析',
        detail: item.event === 'agent_loop_continued'
          ? (targetTool ? `决定继续调用 ${targetTool}` : '决定继续调用工具')
          : '判断无需继续调用工具',
        state: 'completed',
      });
      return;
    }
    if (item.event === 'reflection_decision') {
      const needsRetry = item.data.needs_retry === true;
      const skipped = item.data.skipped === true;
      upsertTraceLine(traceTurnId, {
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
      if (traceTurnId && eventStream.turnId !== traceTurnId) {
        eventStream.turnId = traceTurnId;
      }
      const phase = typeof item.data.phase === 'string' ? item.data.phase : 'thinking';
      if (phase === 'responding') {
        notifyStream();
        return;
      }
      eventStream.phase = publicStreamPhase(item.data);
      if (phase === 'tool' && typeof item.data.tool_name === 'string') {
        const toolCallId = typeof item.data.tool_call_id === 'string' ? item.data.tool_call_id : item.data.tool_name;
        upsertTraceLine(traceTurnId, { id: `tool_${toolCallId}`, kind: 'tool', text: `正在调用 ${item.data.tool_name}`, state: 'running' });
      } else if (phase === 'routing') {
        upsertTraceLine(traceTurnId, { id: 'decision_router', kind: 'decision', text: '判断意图', state: 'running' });
      } else if (isKnowledgeTracePhase(phase)) {
        upsertTraceLine(traceTurnId, {
          id: 'knowledge_lookup',
          kind: 'knowledge',
          text: knowledgeTraceText(item.data),
          detail: knowledgeTraceDetail(item.data),
          state: phase === 'evidence_pack' || phase.startsWith('no_') || phase === 'okf_only' ? 'completed' : 'running',
        });
      } else if (phase === 'stepping') {
        const repairReason = typeof item.data.repair_reason === 'string' ? item.data.repair_reason : 'main';
        const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string' ? `_${item.data.iteration}` : '';
        upsertTraceLine(traceTurnId, {
          id: `decision_stepping_${repairReason}${iteration}`,
          kind: 'decision',
          text: repairReason === 'main' ? '决定下一步' : '重新分析',
          state: 'running',
        });
      } else if (phase === 'reflecting') {
        upsertTraceLine(traceTurnId, { id: 'reflection', kind: 'decision', text: '正在反思', state: 'running' });
      } else if (phase === 'scheduled_task_draft') {
        upsertTraceLine(traceTurnId, {
          id: 'scheduled_task_draft',
          kind: 'decision',
          text: '生成定时任务草案',
          detail: '来自本条消息的定时任务，等待用户确认后启用',
          state: 'completed',
        });
      } else if (phase !== 'received') {
        upsertTraceLine(traceTurnId, { id: `decision_status_${phase}`, kind: 'decision', text: eventStream.phase, state: 'running' });
      }
      notifyStream();
      return;
    }
    if (item.event === 'stream_replace') {
      const next = typeof item.data.content === 'string' ? item.data.content : '';
      const eventStream = getStreamSlot(eventSessionId);
      if (eventStream.timer) {
        window.clearTimeout(eventStream.timer);
        eventStream.timer = null;
      }
      eventStream.accumulated = next;
      updateStreaming(eventSessionId, next, getStreamSlot(eventSessionId).turnId || traceTurnId);
      notifyStream();
      return;
    }
    if (item.event === 'stream_delta' || item.event === 'token') {
      const piece = typeof item.data.content === 'string' ? item.data.content : '';
      if (!piece) return;
      const eventStream = getStreamSlot(eventSessionId);
      const wasEmpty = !eventStream.accumulated;
      eventStream.accumulated += piece;
      if (wasEmpty) {
        updateStreaming(eventSessionId, eventStream.accumulated, eventStream.turnId || traceTurnId);
        notifyStream();
        return;
      }
      if (!eventStream.timer) {
        eventStream.timer = window.setTimeout(() => {
          eventStream.timer = null;
          updateStreaming(eventSessionId, eventStream.accumulated, eventStream.turnId || traceTurnId);
        }, 100);
      }
      return;
    }
    if (item.event === 'stream_end') {
      finishTrace(traceTurnId);
      upsertTraceLine(traceTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
      finalizeStreaming(eventSessionId);
      const eventStream = getStreamSlot(eventSessionId);
      eventStream.loading = false;
      eventStream.phase = '';
      eventStream.abortController = null;
      notifyStream();
      return;
    }
    if (item.event === 'stream_cancelled') {
      const cancelledStreamTurnId = getStreamSlot(eventSessionId).turnId || traceTurnId;
      finishTrace(traceTurnId, true);
      clearStreamSlot(eventSessionId, false);
      upsertTraceStatusPlaceholder(getSlot(eventSessionId), eventSessionId, traceTurnId);
      notifyStore();
      setRunningTurn((current) => (
        current?.sessionId === eventSessionId && (current.turnId === traceTurnId || current.turnId === cancelledStreamTurnId)
          ? null
          : current
      ));
      notifyStream();
      return;
    }
    if (item.event === 'complete' || item.event === 'done') {
      const result = item.data as unknown as ChatTurnResponse;
      const userIntent = typeof result.router_decision?.user_intent === 'string' ? result.router_decision.user_intent : '';
      const decisionReason = typeof result.router_decision?.reason === 'string' ? result.router_decision.reason : '';
      if (userIntent || decisionReason) {
        upsertTraceLine(traceTurnId, {
          id: 'decision_router',
          kind: 'decision',
          text: userIntent ? `判断意图 ${userIntent}` : '完成技能判断',
          detail: decisionReason || undefined,
          state: 'completed',
        });
      }
      finishTrace(traceTurnId);
      upsertTraceLine(traceTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
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
    if (item.event === 'error') {
      const eventStream = getStreamSlot(eventSessionId);
      eventStream.loading = false;
      eventStream.phase = '';
      eventStream.abortController = null;
      finishTrace(eventStream.turnId || traceTurnId, true);
      appendRealtime(eventSessionId, {
        id: `scheduled_error_${Date.now()}`,
        turnId: eventStream.turnId || traceTurnId,
        role: 'assistant',
        content: typeof item.data.message === 'string' ? item.data.message : '定时任务执行失败。',
        created_at: new Date().toISOString(),
        isError: true,
      });
      notifyStream();
    }
  }, [
    appendRealtime,
    applySessionTitleSummary,
    clearStreamSlot,
    finalizeStreaming,
    finishTrace,
    getSlot,
    getStreamSlot,
    getTurnTrace,
    loadMessages,
    loadSessions,
    loadTraces,
    notifyStore,
    notifyStream,
    ensureStreamingTraceMessage,
    updateStreaming,
    upsertTraceLine,
  ]);

  const eventTextPayload = useCallback((event: ChatSessionEventRead): string => {
    const data = event.data || {};
    if (typeof data.content === 'string') return data.content;
    if (typeof data.text === 'string') return data.text;
    return '';
  }, []);

  const isTerminalEvent = useCallback((event: ChatSessionEventRead) => (
    event.event === 'complete'
    || event.event === 'done'
    || event.event === 'stream_end'
    || event.event === 'stream_cancelled'
    || event.event === 'error'
  ), []);

  const eventTime = useCallback((event: ChatSessionEventRead) => parseMessageTime(event.created_at) || 0, []);

  const hydrateRunningSessionFromEvents = useCallback((id: string, events: ChatSessionEventRead[]) => {
    if (locallyCancelledSessionIdsRef.current.has(id)) return false;
    const traceEvents = events.filter((event) => Boolean(eventTraceTurnId(event)));
    if (!traceEvents.length) return false;
    const slot = getSlot(id);
    if (slot.serverMessages.length === 0) return false;
    const stream = getStreamSlot(id);
    if (stream.loading) return false;

    const groups = new Map<string, ChatSessionEventRead[]>();
    traceEvents.forEach((event) => {
      const eventTurnId = eventTraceTurnId(event);
      if (!eventTurnId) return;
      const key = `${id}:${eventTurnId}`;
      const bucket = groups.get(key) || [];
      bucket.push(event);
      groups.set(key, bucket);
    });

    const runningGroup = [...groups.values()]
      .map((group) => [...group].sort((left, right) => eventTime(left) - eventTime(right)))
      .sort((left, right) => eventTime(right[right.length - 1]) - eventTime(left[left.length - 1]))
      .find((group) => !group.some((event) => isTerminalSessionEvent(event, isTerminalEvent)));

    if (!runningGroup?.length) return false;

    const turnId = eventTraceTurnId(runningGroup[0]);
    if (!turnId) return false;
    const runningUserMessage = slot.serverMessages.find((messageItem) => (
      messageItem.role === 'user'
      && (effectiveMessageTurnId(messageItem) === turnId || messageItem.id === turnId)
    ));
    if (!runningUserMessage) return false;
    const latestRunningEventTime = eventTime(runningGroup[runningGroup.length - 1]);
    if (latestRunningEventTime <= 0 || Date.now() - latestRunningEventTime > RUNNING_EVENT_RECOVERY_WINDOW_MS) {
      clearStreamSlot(id, false);
      return false;
    }
    if (!hasRecoverableEventProgress(runningGroup)) {
      clearStreamSlot(id, false);
      return false;
    }
    if (hasAssistantMessageForTurn(slot, turnId)) {
      clearStreamSlot(id, false);
      return false;
    }

    let text = '';
    runningGroup.forEach((event) => {
      const payloadText = eventTextPayload(event);
      if (event.event === 'stream_replace') {
        text = payloadText;
      } else if (event.event === 'stream_delta' || event.event === 'token') {
        text += payloadText;
      }
    });

    stream.turnId = turnId;
    stream.loading = true;
    stream.phase = '执行中';
    stream.accumulated = text;
    updateStreaming(id, text, turnId, true);

    runningGroup.forEach((event) => {
      scheduledEventIdsRef.current.add(event.id);
      const streamEvent = normalizeSessionEventForStream(event);
      if (streamEvent.event === 'stream_replace' || streamEvent.event === 'stream_delta' || streamEvent.event === 'token') return;
      handleStreamEvent(streamEvent, id, turnId);
    });

    notifyStream();
    return true;
  }, [
    clearStreamSlot,
    eventTextPayload,
    eventTime,
    getSlot,
    getStreamSlot,
    handleStreamEvent,
    isTerminalEvent,
    notifyStream,
    updateStreaming,
  ]);

  const pollScheduledSessionEvents = useCallback((id: string) => {
    if (locallyCancelledSessionIdsRef.current.has(id)) return Promise.resolve();
    return api
      .get<ChatSessionEventRead[]>(`/api/chat/sessions/${id}/events?tenant_id=${tenantId}`)
      .then((events) => {
        const traceEvents = events.filter((event) => Boolean(eventTraceTurnId(event)));
        if (!traceEvents.length) return;
        hydrateRunningSessionFromEvents(id, traceEvents);
        const slot = getSlot(id);
        if (slot.serverMessages.length === 0) return;
        const latestLoadedMessageTime = Math.max(
          0,
          ...slot.serverMessages.map((messageItem) => parseMessageTime(messageItem.created_at)),
        );
        const now = Date.now();
        const stream = getStreamSlot(id);
        const unseenEvents = traceEvents.filter((event) => {
          if (scheduledEventIdsRef.current.has(event.id)) return false;
          const timestamp = eventTime(event);
          return timestamp > 0 && (timestamp >= latestLoadedMessageTime || now - timestamp <= RUNNING_EVENT_RECOVERY_WINDOW_MS);
        });
        if (!unseenEvents.length) return;
        const sessionRow = sessions.find((item) => item.id === id);
        const hasTerminalEvent = unseenEvents.some((event) => isTerminalSessionEvent(event, isTerminalEvent));
        if (!stream.loading && Boolean(sessionRow?.summary) && hasTerminalEvent) {
          unseenEvents.forEach((event) => scheduledEventIdsRef.current.add(event.id));
          return;
        }
        const unseenEventsByTurn = new Map<string, ChatSessionEventRead[]>();
        unseenEvents.forEach((event) => {
          const eventTurnId = eventTraceTurnId(event);
          if (!eventTurnId) return;
          const bucket = unseenEventsByTurn.get(eventTurnId) || [];
          bucket.push(event);
          unseenEventsByTurn.set(eventTurnId, bucket);
        });
        unseenEvents.forEach((event) => {
          scheduledEventIdsRef.current.add(event.id);
          const eventTurnId = eventTraceTurnId(event);
          if (!eventTurnId) return;
          if (!stream.turnId) {
            stream.turnId = eventTurnId;
          }
          const streamEvent = normalizeSessionEventForStream(event);
          if (!stream.loading && !isTerminalSessionEvent(event, isTerminalEvent)) {
            const turnEvents = unseenEventsByTurn.get(eventTurnId) || [event];
            if (!hasRecoverableEventProgress(turnEvents)) {
              handleStreamEvent(streamEvent, id, eventTurnId);
              return;
            }
            stream.loading = true;
            stream.phase = '执行中';
            updateStreaming(id, stream.accumulated || '', eventTurnId, true);
            notifyStream();
          }
          handleStreamEvent(streamEvent, id, eventTurnId);
        });
      })
      .catch((error) => {
        if (isAuthError(error)) {
          redirectToLogin();
        }
      });
  }, [
    eventTime,
    getSlot,
    getStreamSlot,
    handleStreamEvent,
    hydrateRunningSessionFromEvents,
    isTerminalEvent,
    notifyStream,
    redirectToLogin,
    sessions,
    tenantId,
    updateStreaming,
  ]);

  const uploadComposerFiles = useCallback((files: File[]) => {
    const validFiles = files.filter((file) => file.size > 0);
    if (!validFiles.length) return;
    if (currentSessionRunning) {
      notify.warning('当前对话正在执行，结束后再上传文件');
      return;
    }
    validFiles.forEach((file) => {
      const uploadKey = `upload_${Date.now()}_${Math.random().toString(16).slice(2)}`;
      const controller = new AbortController();
      uploadControllersRef.current.set(uploadKey, controller);
      setComposerAttachments((current) => [
        ...current,
        {
          id: uploadKey,
          uploadKey,
          filename: file.name || '剪贴板文件',
          content_type: file.type || 'application/octet-stream',
          size: file.size,
          kind: file.type.startsWith('image/') ? 'image' : 'binary',
          uploadStatus: 'uploading',
        },
      ]);
      uploadChatAttachments<ChatAttachmentRead[]>(tenantId, [file], controller.signal)
        .then((items) => {
          const parsed = items[0];
          if (!parsed) throw new Error('文件解析结果为空');
          setComposerAttachments((current) =>
            current.map((item) => (item.uploadKey === uploadKey ? { ...parsed, uploadKey, uploadStatus: 'ready' } : item)),
          );
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          setComposerAttachments((current) =>
            current.map((item) => (
              item.uploadKey === uploadKey
                ? { ...item, uploadStatus: 'error', error: error instanceof Error ? error.message : '上传失败' }
                : item
            )),
          );
        })
        .finally(() => {
          uploadControllersRef.current.delete(uploadKey);
        });
    });
  }, [currentSessionRunning, tenantId]);

  const removeComposerAttachment = useCallback((uploadKey: string) => {
    uploadControllersRef.current.get(uploadKey)?.abort();
    uploadControllersRef.current.delete(uploadKey);
    setComposerAttachments((current) => current.filter((item) => item.uploadKey !== uploadKey));
  }, []);

  const handleComposerFileChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    uploadComposerFiles(files);
    event.target.value = '';
  }, [uploadComposerFiles]);

  const handleComposerDragEnter = useCallback((event: DragEvent<HTMLFormElement>) => {
    if (!event.dataTransfer.types.includes('Files')) return;
    event.preventDefault();
    setComposerDragActive(true);
  }, []);

  const handleComposerDragOver = useCallback((event: DragEvent<HTMLFormElement>) => {
    if (!event.dataTransfer.types.includes('Files')) return;
    event.preventDefault();
    setComposerDragActive(true);
  }, []);

  const handleComposerDragLeave = useCallback((event: DragEvent<HTMLFormElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setComposerDragActive(false);
    }
  }, []);

  const handleComposerDrop = useCallback((event: DragEvent<HTMLFormElement>) => {
    const files = Array.from(event.dataTransfer.files || []);
    if (!files.length) return;
    event.preventDefault();
    setComposerDragActive(false);
    uploadComposerFiles(files);
  }, [uploadComposerFiles]);

  useEffect(() => {
    if (!auth) return;
    const pollBackgroundSessions = () => {
      const ids = new Set<string>();
      const isLiveSseSession = (id: string) => Boolean(streamRef.current.get(id)?.abortController);
      sessions.forEach((session) => {
        const looksRunning = (
          session.status === 'running'
          || session.status === 'executing'
          || (session.summary || '').includes('执行中')
          || (session.last_agent_question || '').includes('执行中')
        );
        if (looksRunning && !isLiveSseSession(session.id)) ids.add(session.id);
      });
      streamRef.current.forEach((slot, id) => {
        if (slot.loading && !slot.abortController && !isDraftConversationKey(id)) ids.add(id);
      });
      Array.from(ids).slice(0, 8).forEach((id) => {
        void pollScheduledSessionEvents(id);
      });
    };
    pollBackgroundSessions();
    const timer = window.setInterval(pollBackgroundSessions, 1800);
    return () => window.clearInterval(timer);
  }, [auth, pollScheduledSessionEvents, sessionId, sessions, streamTick]);

  const send = useCallback(async (interactionMode?: ComposerInteractionMode) => {
    const resolvedInteractionMode = interactionMode || composerIntent || 'normal';
    if (!activeConversationId) return;
    if (resolvedInteractionMode === 'scheduled_task' && !input.trim()) {
      notify.warning('请输入要创建的定时任务内容');
      return;
    }
    if (!input.trim() && readyComposerAttachments.length === 0) return;
    if (uploadingComposerAttachment) {
      notify.warning('文件还在解析中，请稍后发送');
      return;
    }
    const currentConversationId = activeConversationId;
    const activeSession = sessionId ? sessions.find((item) => item.id === sessionId) || null : null;
    if (!isDraftConversation && !activeSession) {
      notify.warning('任务信息还在加载，请稍后再发送');
      return;
    }
    const sessionAgentId = activeSession?.agent_id || activeDraftAgentId || selectedAgentId || displayedAgent?.id || '';
    if (!sessionAgentId) {
      notify.warning('该任务没有绑定数字员工，请新建任务后再发送');
      return;
    }
    const stream = getStreamSlot(currentConversationId);
    if (stream.loading || currentSessionRunning) return;
    const userText = input.trim();
    const outgoingAttachments = readyComposerAttachments.map(toRequestAttachment);
    const turnId = `turn_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    let liveConversationId = currentConversationId;
    let createdSessionId = '';
    locallyCancelledSessionIdsRef.current.delete(currentConversationId);
    setInput('');
    setComposerAttachments([]);
    setComposerIntent(null);
    stream.accumulated = '';
    stream.cancelledTurnId = null;
    stream.turnId = turnId;
    appendRealtime(currentConversationId, {
      id: `local_${turnId}`,
      turnId,
      role: 'user',
      content: userText,
      metadata: {
        ...(outgoingAttachments.length ? { attachments: outgoingAttachments } : {}),
        ...(resolvedInteractionMode === 'scheduled_task' ? { interaction_mode: 'scheduled_task' } : {}),
      },
      created_at: new Date().toISOString(),
    });
    upsertTraceLine(turnId, { id: 'decision_router', kind: 'decision', text: '判断意图', state: 'running' });
    setCollapsedTraceIds((current) => current.filter((item) => item !== turnId));
    setExpandedTraceIds((current) => (current.includes(turnId) ? current : [...current, turnId]));
    stream.loading = true;
    stream.phase = '正在思考';
    updateStreaming(currentConversationId, '', turnId, true);
    setRunningTurn({ sessionId: currentConversationId, turnId });
    notifyStream();

    const controller = new AbortController();
    stream.abortController = controller;
    let receivedTerminalEvent = false;
    let streamWatchdog: number | null = null;

    const clearRunningTurn = (targetId = liveConversationId) => {
      setRunningTurn((current) => (
        current?.turnId === turnId && (current.sessionId === targetId || current.sessionId === currentConversationId)
          ? null
          : current
      ));
    };

    const clearStreamWatchdog = () => {
      if (streamWatchdog !== null) {
        window.clearTimeout(streamWatchdog);
        streamWatchdog = null;
      }
    };

    const appendInterruptedResponse = (reason: string) => {
      const activeTurnId = getStreamSlot(liveConversationId).turnId || turnId;
      clearStreamSlot(liveConversationId, true);
      appendRealtime(liveConversationId, {
        id: `stream_interrupted_${activeTurnId}_${Date.now()}`,
        turnId: activeTurnId,
        role: 'assistant',
        content: reason,
        created_at: new Date().toISOString(),
        isError: true,
      });
      finishTrace(activeTurnId, true);
      clearRunningTurn(liveConversationId);
      notifyStream();
      window.setTimeout(() => {
        loadMessages(liveConversationId);
        loadTraces(liveConversationId);
        loadSessions();
      }, 250);
    };

    const failStalledStream = () => {
      if (receivedTerminalEvent || controller.signal.aborted) return;
      const activeStream = getStreamSlot(liveConversationId);
      if (
        activeStream.abortController !== controller
        || activeStream.cancelledTurnId === turnId
        || (activeStream.turnId && activeStream.cancelledTurnId === activeStream.turnId)
      ) {
        return;
      }
      controller.abort();
      appendInterruptedResponse('本次响应等待时间过长，已停止等待。请重试发送。');
    };

    const armStreamWatchdog = () => {
      clearStreamWatchdog();
      streamWatchdog = window.setTimeout(failStalledStream, CHAT_STREAM_IDLE_TIMEOUT_MS);
    };

    const markStreamTerminal = () => {
      receivedTerminalEvent = true;
      clearStreamWatchdog();
    };

    const promoteDraftConversation = (nextSessionId: string) => {
      if (!isDraftConversation || !nextSessionId || nextSessionId === liveConversationId) return;
      const previousId = liveConversationId;
      const draftSlot = storeRef.current.get(previousId);
      if (draftSlot) {
        const previousStreamMessageId = `__streaming_${previousId}`;
        const nextStreamMessageId = `__streaming_${nextSessionId}`;
        draftSlot.realtimeMessages = draftSlot.realtimeMessages.map((item) => (
          item.id === previousStreamMessageId ? { ...item, id: nextStreamMessageId } : item
        ));
        storeRef.current.set(nextSessionId, draftSlot);
        storeRef.current.delete(previousId);
      }
      const draftStream = streamRef.current.get(previousId);
      if (draftStream) {
        streamRef.current.set(nextSessionId, draftStream);
        streamRef.current.delete(previousId);
      }
      if (locallyCancelledSessionIdsRef.current.has(previousId)) {
        locallyCancelledSessionIdsRef.current.delete(previousId);
        locallyCancelledSessionIdsRef.current.add(nextSessionId);
      }
      setScheduledDrafts((prev) => {
        if (!prev[previousId]) return prev;
        const next = { ...prev, [nextSessionId]: prev[previousId] };
        delete next[previousId];
        return next;
      });
      setCreatedScheduledTasks((prev) => {
        const previousKey = `session:${previousId}`;
        if (!prev[previousKey]) return prev;
        const next = { ...prev, [`session:${nextSessionId}`]: prev[previousKey] };
        delete next[previousKey];
        return next;
      });
      const now = new Date().toISOString();
      upsertOptimisticSession({
        id: nextSessionId,
        tenant_id: tenantId,
        user_id: userId,
        agent_id: sessionAgentId,
        status: 'active',
        summary: userText || undefined,
        last_agent_question: userText || undefined,
        updated_at: now,
      });
      liveConversationId = nextSessionId;
      setRunningTurn((current) => (
        current?.sessionId === previousId && current.turnId === turnId ? { sessionId: nextSessionId, turnId } : current
      ));
      notifyStore();
      notifyStream();
      navigate(chatSessionPath(nextSessionId), { replace: true });
      loadSessions();
    };

    try {
      const requestBody: Record<string, unknown> = {
        tenant_id: tenantId,
        user_id: userId,
        agent_id: sessionAgentId,
        message: userText,
        client_turn_id: turnId,
        attachments: outgoingAttachments,
        channel: 'web',
        interaction_mode: resolvedInteractionMode,
        model_config_id: selectedModelConfig?.id,
      };
      if (!isDraftConversation) {
        requestBody.session_id = currentConversationId;
      }
      armStreamWatchdog();
      await streamChatTurn(requestBody, (item) => {
        if (!controller.signal.aborted) {
          armStreamWatchdog();
        }
        if (item.event === 'session_created') {
          createdSessionId = String(item.data.newSessionId || item.data.sessionId || '');
          return;
        }
        const eventSessionId = isDraftConversation
          ? currentConversationId
          : String(item.data.sessionId || liveConversationId);
        const eventStream = getStreamSlot(eventSessionId);
        const traceTurnId = explicitStreamTurnId(item.data, eventStream.turnId || turnId);
        if (
          controller.signal.aborted
          || eventStream.cancelledTurnId === turnId
          || Boolean(traceTurnId && eventStream.cancelledTurnId === traceTurnId)
        ) {
          return;
        }
        if (item.event === 'user_message_received') {
          const serverMessageId = typeof item.data.message_id === 'string' ? item.data.message_id : '';
          bindRealtimeUserToServerMessage(eventSessionId, turnId, serverMessageId);
          return;
        }
        if (item.event === 'complete' || item.event === 'done') {
          markStreamTerminal();
          const result = item.data as unknown as ChatTurnResponse;
          const completedSessionId = result.session_id || createdSessionId || String(item.data.sessionId || '');
          handleStreamEvent(item, isDraftConversation ? currentConversationId : (String(item.data.sessionId || liveConversationId)), turnId);
          if (isDraftConversation && completedSessionId) {
            promoteDraftConversation(completedSessionId);
          }
          return;
        }
        if (item.event === 'stream_end' || item.event === 'stream_cancelled') {
          markStreamTerminal();
        }
        handleStreamEvent(item, eventSessionId, turnId);
      }, controller.signal);
      clearStreamWatchdog();
      if (!receivedTerminalEvent && !controller.signal.aborted) {
        const activeStream = getStreamSlot(liveConversationId);
        const activeTurnId = activeStream.turnId || turnId;
        if (activeStream.accumulated) {
          finishTrace(activeTurnId);
          upsertTraceLine(activeTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
          finalizeStreaming(liveConversationId);
          activeStream.loading = false;
          activeStream.phase = '';
          activeStream.abortController = null;
          clearRunningTurn(liveConversationId);
          notifyStream();
          loadSessions();
          window.setTimeout(() => {
            loadMessages(liveConversationId);
            loadTraces(liveConversationId);
          }, 250);
        } else {
          appendInterruptedResponse('本次响应中断，未收到模型回复。请重试发送。');
        }
      }
    } catch (error) {
      clearStreamWatchdog();
      if (controller.signal.aborted) {
        return;
      }
      if (isAuthError(error)) {
        finishTrace(getStreamSlot(liveConversationId).turnId || turnId, true);
        clearStreamSlot(liveConversationId, true);
        clearRunningTurn();
        notifyStream();
        redirectToLogin();
        return;
      }
      const activeTurnId = getStreamSlot(liveConversationId).turnId || turnId;
      clearStreamSlot(liveConversationId, true);
      appendRealtime(liveConversationId, {
        id: `error_${Date.now()}`,
        turnId: activeTurnId,
        role: 'assistant',
        content: '发送失败，请稍后重试',
        created_at: new Date().toISOString(),
        isError: true,
      });
      notifyRequestError('send', error, '发送失败');
      finishTrace(activeTurnId, true);
      clearRunningTurn();
      notifyStream();
    } finally {
      clearStreamWatchdog();
      if (stream.abortController === controller) {
        stream.abortController = null;
        stream.loading = false;
        stream.phase = '';
        clearRunningTurn();
        notifyStream();
      }
    }
  }, [
    activeConversationId,
    activeDraftAgentId,
    appendRealtime,
    bindRealtimeUserToServerMessage,
    clearStreamSlot,
    composerIntent,
    currentSessionRunning,
    displayedAgent?.id,
    finalizeStreaming,
    finishTrace,
    getStreamSlot,
    handleStreamEvent,
    input,
    isDraftConversation,
    loadMessages,
    loadSessions,
    loadTraces,
    navigate,
    notifyRequestError,
    notifyStore,
    notifyStream,
    readyComposerAttachments,
    redirectToLogin,
    selectedAgentId,
    selectedModelConfig?.id,
    sessionId,
    sessions,
    tenantId,
    updateStreaming,
    uploadingComposerAttachment,
    upsertOptimisticSession,
    upsertTraceLine,
    userId,
  ]);

  const handleComposerPaste = useCallback((event: ClipboardEvent<HTMLTextAreaElement>) => {
    const clipboardData = event.clipboardData;
    if (!clipboardData || !clipboardContainsComposerImage(clipboardData)) return;
    event.preventDefault();
    void extractPastedComposerFiles(clipboardData).then((files) => {
      if (files.length) uploadComposerFiles(files);
    });
  }, [uploadComposerFiles]);

  const handleComposerPlusAction = useCallback((action: 'upload' | 'scheduled_task') => {
    setComposerPlusOpen(false);
    if (action === 'upload') {
      fileInputRef.current?.click();
      return;
    }
    setComposerIntent('scheduled_task');
  }, []);

  const openSession = useCallback((id: string) => {
    navigate(chatSessionPath(id));
  }, [navigate]);

  const openGallery = useCallback(() => {
    navigate(CHAT_BASE_PATH);
  }, [navigate]);

  return {
    auth,
    SHOW_DEBUG,
    lastTurn,
    // sessions + agents
    sessions,
    sessionsLoading,
    visibleSidebarSessions,
    agents,
    sessionId,
    sessionReadTimes,
    sessionAgentFilter,
    setSessionAgentFilter,
    sessionFilterOptions,
    // active state
    activeConversationId,
    displayedAgent,
    displayedProfile,
    currentSession,
    emptyProfileTags,
    emptyRoleSummary,
    emptyStats,
    // messages / trace
    displayedMessages,
    turnTraceRef,
    uiConfig,
    expandedTraceIds,
    collapsedTraceIds,
    toggleTrace,
    currentStream,
    runningTurn,
    currentSessionRunning,
    isCurrentStreamingTrace,
    // scheduled
    scheduledDrafts,
    createdScheduledTasks,
    dismissedDraftMessageIds,
    currentScheduledDraft,
    hasVisibleMessageScheduledDraft,
    confirmScheduledTask,
    dismissScheduledTaskDraft,
    // composer
    input,
    setInput,
    composerAttachments,
    composerDragActive,
    composerPlusOpen,
    setComposerPlusOpen,
    composerIntent,
    setComposerIntent,
    readyComposerAttachments,
    uploadingComposerAttachment,
    composerActive,
    showComposerAvatar,
    isComposing,
    setIsComposing,
    enabledModelConfigs,
    selectedModelConfig,
    changeModelConfig,
    // models handled in composer
    // refs
    chatMessagesRef,
    fileInputRef,
    // handlers
    handleChatMessagesScroll,
    send,
    abortStream,
    rateMessage,
    setActiveCitation,
    activeCitation,
    // sidebar
    sidebarCollapsed,
    toggleSidebar,
    openSession,
    openGallery,
    openRename,
    requestDelete,
    logout: redirectToLogin,
    openAdmin: () => navigate('/enterprise/dashboard'),
    // rename dialog
    renameSession,
    setRenameSession,
    renameTitle,
    setRenameTitle,
    saveRename,
    // delete dialog
    pendingDelete,
    setPendingDelete,
    confirmDeleteSession,
    // handoff
    handoffs,
    handoffsLoading,
    showHandoffInbox,
    setShowHandoffInbox,
    handoffReplies,
    setHandoffReplies,
    submitHandoffReply,
    // composer actions
    handleComposerFileChange,
    handleComposerDragEnter,
    handleComposerDragOver,
    handleComposerDragLeave,
    handleComposerDrop,
    handleComposerPaste,
    removeComposerAttachment,
    handleComposerPlusAction,
    uploadComposerFiles,
    navigate,
  };
}
