import {
  ApiOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CheckOutlined,
  CodeOutlined,
  CloseOutlined,
  CloseCircleOutlined,
  DownOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  RightOutlined,
  SaveOutlined,
  SendOutlined,
  StopOutlined,
  UploadOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { Button, Card, Empty, Input, Modal, Space, Tag, Tooltip, Typography, Upload, message } from 'antd';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type CSSProperties,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, streamGet, streamPost, TENANT_ID } from '../api/client';
import type { SkillCard, SkillRead, ToolProbeResponse, ToolRead, ToolSuggestion } from '../types';

type ChatItem = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  attachments?: ChatAttachment[];
  outgoingText?: string;
  createdAt?: string;
  thinking?: 'running' | 'done';
  thinkingDetails?: string[];
  thinkingOpen?: boolean;
  warnings?: string[];
  toolSuggestions?: ToolSuggestionItem[];
  actionState?: 'pending' | 'confirmed' | 'rejected';
  snapshotBefore?: DistillHistorySnapshot;
  operations?: DistillHistoryOperation[];
};

type ChatAttachment = {
  id: string;
  name: string;
  type: string;
};

type ToolSuggestionItem = ToolSuggestion & {
  status?: 'pending' | 'accepted' | 'created' | 'rejected';
  probeStatus?: 'idle' | 'probing' | 'success' | 'error';
};

type ProbeToolOptions = {
  sampleArguments?: Record<string, unknown>;
  silent?: boolean;
  allowWhileLoading?: boolean;
};

type UploadAttachment = {
  id: string;
  name: string;
  status: 'uploading' | 'ready' | 'error';
  text?: string;
  error?: string;
};

type TargetSelection = {
  path: string;
  label: string;
};
type ToolDescriptionMap = Record<string, string>;
type ToolActionStatus = 'existing' | 'pending' | 'accepted' | 'created' | 'rejected' | 'incomplete';
type ToolStatusMap = Record<string, ToolActionStatus>;

type ViewMode = 'source' | 'flow';
type TeacherPraiseStage = 'idle' | 'praised';
type PendingChange = {
  assistantId: string;
  previousDraft: SkillCard;
  nextDraft: SkillCard;
  changedPaths: string[];
};
type TextDiffPhase = 'mark' | 'type' | 'settled';
type TextDiffAnimation = {
  key: string;
  path: string;
  field: string;
  prefix: string;
  removed: string;
  inserted: string;
  suffix: string;
  phase: TextDiffPhase;
  progress: number;
};

type ActiveDistillJob = {
  jobId: string;
  kind: 'distill' | 'rewrite';
  assistantId: string;
  lastSeq: number;
  status?: string;
  createPayload?: { title: string; raw_content: string };
  previousDraft?: SkillCard;
  targets?: string[];
};

const DEFAULT_TARGET_PATHS: string[] = [];
const DEFAULT_DISTILL_MESSAGES: ChatItem[] = [
  {
    id: 'welcome',
    role: 'assistant',
    content: '请粘贴原始技能说明，或点击右侧某一块后告诉我需要怎样改写。',
  },
];
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type DistillCacheSnapshot = {
  draft: SkillCard | null;
  loadedSkill: SkillRead | null;
  lastSavedDraft: SkillCard | null;
  messages: ChatItem[];
  input: string;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  dirtyPaths: string[];
  textDiffs: TextDiffAnimation[];
  pendingChange: PendingChange | null;
  viewMode: ViewMode;
  attachments: UploadAttachment[];
  streamStatus: string;
  activeJob: ActiveDistillJob | null;
  manualSourceEdited: boolean;
  teacherPraiseStage: TeacherPraiseStage;
};

type DistillHistoryOperationKind = 'skill_change' | 'version_save' | 'tool_add';

type DistillHistoryOperation = {
  kind: DistillHistoryOperationKind;
  label: string;
  skillId?: string;
  version?: string;
  toolId?: string;
  toolName?: string;
};

type DistillHistorySnapshot = {
  draft: SkillCard | null;
  loadedSkill: SkillRead | null;
  lastSavedDraft: SkillCard | null;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  dirtyPaths: string[];
  textDiffs: TextDiffAnimation[];
  pendingChange: PendingChange | null;
  viewMode: ViewMode;
  tools: ToolRead[];
  attachments: UploadAttachment[];
  streamStatus: string;
};

type EditingMessage = {
  id: string;
  text: string;
};

type DistillPageProps = {
  active?: boolean;
  searchParamsOverride?: URLSearchParams;
};

export default function DistillPage({ active = true, searchParamsOverride }: DistillPageProps = {}) {
  const [routerSearchParams] = useSearchParams();
  const searchParams = searchParamsOverride || routerSearchParams;
  const skillId = searchParams.get('skill_id');
  const mode = searchParams.get('mode') || '';
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const activeAgentId = searchParams.get('agent_id') || selectedAgentId;
  const agentQuery = activeAgentId ? `&agent_id=${encodeURIComponent(activeAgentId)}` : '';
  const agentSearchParam = activeAgentId ? `agent_id=${encodeURIComponent(activeAgentId)}` : '';
  const agentOnlyQuery = agentSearchParam ? `?${agentSearchParam}` : '';
  const cacheKey = `skill-distill:${TENANT_ID}:${activeAgentId || 'default'}:${skillId || mode || 'new'}`;
  const [draft, setDraft] = useState<SkillCard | null>(null);
  const [loadedSkill, setLoadedSkill] = useState<SkillRead | null>(null);
  const [lastSavedDraft, setLastSavedDraft] = useState<SkillCard | null>(null);
  const [messages, setMessages] = useState<ChatItem[]>(DEFAULT_DISTILL_MESSAGES);
  const [input, setInput] = useState('');
  const [selectedPaths, setSelectedPaths] = useState<string[]>(DEFAULT_TARGET_PATHS);
  const [highlightedPaths, setHighlightedPaths] = useState<string[]>([]);
  const [updatingPaths, setUpdatingPaths] = useState<string[]>([]);
  const [dirtyPaths, setDirtyPaths] = useState<string[]>([]);
  const [textDiffs, setTextDiffs] = useState<TextDiffAnimation[]>([]);
  const [pendingChange, setPendingChange] = useState<PendingChange | null>(null);
  const [saveReviewOpen, setSaveReviewOpen] = useState(false);
  const [saveDraftSnapshot, setSaveDraftSnapshot] = useState<SkillCard | null>(null);
  const [saveName, setSaveName] = useState('');
  const [saveDomain, setSaveDomain] = useState('');
  const [saveVersion, setSaveVersion] = useState('');
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const [clearAfterSave, setClearAfterSave] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>('source');
  const [loading, setLoading] = useState(false);
  const [attachments, setAttachments] = useState<UploadAttachment[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [toolDetail, setToolDetail] = useState<ToolSuggestionItem | null>(null);
  const [toolDetailMessageId, setToolDetailMessageId] = useState<string | null>(null);
  const [probeArgsText, setProbeArgsText] = useState('');
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [streamStatus, setStreamStatus] = useState('');
  const [activeJob, setActiveJob] = useState<ActiveDistillJob | null>(null);
  const [manualSourceEdited, setManualSourceEdited] = useState(false);
  const [teacherPraiseStage, setTeacherPraiseStage] = useState<TeacherPraiseStage>('idle');
  const [editingMessage, setEditingMessage] = useState<EditingMessage | null>(null);
  const [sourceAutoScroll, setSourceAutoScroll] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const manualStopRef = useRef(false);
  const uploadControllersRef = useRef<Record<string, AbortController>>({});
  const dragDepthRef = useRef(0);
  const animationTimersRef = useRef<number[]>([]);
  const sourceScrollRef = useRef<HTMLDivElement | null>(null);
  const chatMessagesRef = useRef<HTMLDivElement | null>(null);
  const [cacheReady, setCacheReady] = useState(false);
  const [hydratedCacheKey, setHydratedCacheKey] = useState('');

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const agentId = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      setSelectedAgentId(agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    setCacheReady(false);
    setHydratedCacheKey('');
    const cached = readDistillCache(cacheKey);
    if (cached) {
      setDraft(cached.draft);
      setLoadedSkill(cached.loadedSkill);
      setLastSavedDraft(cached.lastSavedDraft);
      setMessages(cached.messages.length > 0 ? cached.messages : DEFAULT_DISTILL_MESSAGES);
      setInput(cached.input);
      setSelectedPaths(normalizeInitialSelectedPaths(cached.selectedPaths));
      setHighlightedPaths(cached.highlightedPaths);
      setUpdatingPaths(cached.updatingPaths);
      setDirtyPaths(cached.dirtyPaths);
      setTextDiffs(cached.textDiffs);
      setPendingChange(cached.pendingChange);
      setViewMode(cached.viewMode || 'source');
      setAttachments(cached.attachments.filter((item) => item.status !== 'uploading'));
      setStreamStatus(cached.streamStatus);
      setActiveJob(cached.activeJob || null);
      setManualSourceEdited(cached.manualSourceEdited);
      setTeacherPraiseStage(cached.teacherPraiseStage);
      if (cached.activeJob && cached.activeJob.status !== 'succeeded' && cached.activeJob.status !== 'failed') {
        setLoading(true);
      }
      setSaveDraftSnapshot(null);
      setHydratedCacheKey(cacheKey);
      setCacheReady(true);
      return;
    }

    if (!skillId) {
      setDraft(null);
      setLoadedSkill(null);
      setLastSavedDraft(null);
      setMessages(DEFAULT_DISTILL_MESSAGES);
      setInput('');
      setSelectedPaths(DEFAULT_TARGET_PATHS);
      setPendingChange(null);
      setHighlightedPaths([]);
      setUpdatingPaths([]);
      setDirtyPaths([]);
      setTextDiffs([]);
      setAttachments([]);
      setStreamStatus('');
      setManualSourceEdited(false);
      setTeacherPraiseStage('idle');
      setSaveDraftSnapshot(null);
      setHydratedCacheKey(cacheKey);
      setCacheReady(true);
      return;
    }

    api
      .get<SkillRead>(`/api/enterprise/skills/${encodeURIComponent(skillId)}?tenant_id=${TENANT_ID}${agentQuery}`)
      .then((result) => {
        setDraft(result.content);
        setLoadedSkill(result);
        setLastSavedDraft(result.content);
        setSelectedPaths(DEFAULT_TARGET_PATHS);
        setPendingChange(null);
        setHighlightedPaths([]);
        setUpdatingPaths([]);
        setDirtyPaths([]);
        setTextDiffs([]);
        setAttachments([]);
        setStreamStatus('');
        setManualSourceEdited(false);
        setTeacherPraiseStage('idle');
        setSaveDraftSnapshot(null);
        setMessages([
          {
            id: 'loaded',
            role: 'assistant',
            content: `已加载「${result.name}」。你可以在右侧选择一个或多个区域，然后在这里描述需要怎样改写。`,
          },
        ]);
        setHydratedCacheKey(cacheKey);
        setCacheReady(true);
      })
      .catch((error) => {
        message.error(error instanceof Error ? error.message : '加载技能失败');
        setHydratedCacheKey(cacheKey);
        setCacheReady(true);
      });
  }, [agentQuery, cacheKey, skillId]);

  useEffect(() => {
    if (!cacheReady || hydratedCacheKey !== cacheKey) return;
    writeDistillCache(cacheKey, {
      draft,
      loadedSkill,
      lastSavedDraft,
      messages,
      input,
      selectedPaths,
      highlightedPaths,
      updatingPaths,
      dirtyPaths,
      textDiffs,
      pendingChange,
      viewMode,
      attachments: attachments.filter((item) => item.status !== 'uploading'),
      streamStatus,
      activeJob,
      manualSourceEdited,
      teacherPraiseStage,
    });
  }, [
    attachments,
    cacheKey,
    cacheReady,
    dirtyPaths,
    draft,
    highlightedPaths,
    hydratedCacheKey,
    input,
    lastSavedDraft,
    loadedSkill,
    loading,
    manualSourceEdited,
    messages,
    pendingChange,
    selectedPaths,
    streamStatus,
    activeJob,
    teacherPraiseStage,
    textDiffs,
    updatingPaths,
    viewMode,
  ]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      Object.values(uploadControllersRef.current).forEach((controller) => controller.abort());
      clearAnimationTimers();
    };
  }, []);

  useEffect(() => {
    if (!cacheReady || hydratedCacheKey !== cacheKey || !activeJob) return;
    if (activeJob.status === 'succeeded' || activeJob.status === 'failed') return;
    if (abortRef.current) return;
    const controller = new AbortController();
    manualStopRef.current = false;
    abortRef.current = controller;
    setLoading(true);
    void streamGet(
      `/api/enterprise/skills/jobs/${encodeURIComponent(activeJob.jobId)}/stream?after_seq=${activeJob.lastSeq || 0}`,
      (item) => handleResumedJobEvent(activeJob, item),
      controller.signal,
    )
      .catch((error) => {
        if (controller.signal.aborted) return;
        updateMessage(activeJob.assistantId, '生成连接已断开，后端任务仍可继续。', { thinking: 'done' });
        message.error(error instanceof Error ? error.message : '恢复生成失败');
      })
      .finally(() => finishStream(controller));
  }, [activeJob, cacheKey, cacheReady, hydratedCacheKey]);

  useEffect(() => {
    if (!active) {
      document.body.classList.remove('skill-distill-fixed');
      return;
    }
    document.body.classList.add('skill-distill-fixed');
    return () => {
      document.body.classList.remove('skill-distill-fixed');
    };
  }, [active]);

  useEffect(() => {
    api
      .get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}`)
      .then(setTools)
      .catch(() => setTools([]));
  }, []);

  useEffect(() => {
    if (!chatMessagesRef.current) return;
    chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight;
  }, [attachments, loading, messages]);

  useEffect(() => {
    if (!loading || !sourceAutoScroll || !sourceScrollRef.current) return;
    sourceScrollRef.current.scrollTop = sourceScrollRef.current.scrollHeight;
  }, [draft, loading, sourceAutoScroll, textDiffs, viewMode]);

  const allPaths = useMemo(() => (draft ? allTargetPaths(draft) : DEFAULT_TARGET_PATHS), [draft]);
  const uploadingFile = attachments.some((item) => item.status === 'uploading');
  const readyAttachments = attachments.filter((item) => item.status === 'ready' && item.text?.trim());
  const allSelected = draft ? selectedPaths.length > 0 && allPaths.every((path) => selectedPaths.includes(path)) : false;
  const toolDescriptions = useMemo(() => buildToolDescriptionMap(tools, messages), [messages, tools]);
  const toolStatuses = useMemo(() => buildToolStatusMap(tools, messages), [messages, tools]);
  const saveReviewDraft = useMemo(() => {
    const sourceDraft = saveDraftSnapshot || draft;
    if (!sourceDraft) return null;
    return {
      ...cloneSkill(sourceDraft),
      name: saveName.trim() || sourceDraft.name,
      business_domain: saveDomain.trim() || undefined,
      version: saveVersion.trim() || sourceDraft.version,
    };
  }, [draft, saveDomain, saveDraftSnapshot, saveName, saveVersion]);
  const saveReviewDiffs = useMemo(() => {
    if (!saveReviewDraft) return [];
    const baseDraft = lastSavedDraft || blankSkillForAnimation(saveReviewDraft);
    const changedPaths = diffTargetPaths(baseDraft, saveReviewDraft, allTargetPaths(saveReviewDraft));
    return collectTextDiffs(baseDraft, saveReviewDraft, changedPaths).filter((diff) => diff.field !== 'version');
  }, [lastSavedDraft, saveReviewDraft]);
  const hasSaveableDraftChanges = useMemo(
    () => hasSkillContentChanges(pendingChange?.nextDraft || draft, lastSavedDraft),
    [draft, lastSavedDraft, pendingChange],
  );
  const saveReviewHasContentChanges = useMemo(
    () => hasSkillContentChanges(saveReviewDraft, lastSavedDraft),
    [lastSavedDraft, saveReviewDraft],
  );

  async function send() {
    const text = buildOutgoingText(input, readyAttachments);
    if (!text || loading || uploadingFile) return;
    const displayText = input.trim();
    const displayAttachments = buildDisplayAttachments(readyAttachments);
    const snapshotBefore = createHistorySnapshot();
    const confirmedDraft = pendingChange?.nextDraft || draft;
    confirmPendingChange(false);
    setInput('');
    setAttachments([]);
    pushMessage('user', displayText, { attachments: displayAttachments, outgoingText: text, snapshotBefore });
    const teacherPraiseReply = skillEditTeacherPraiseReply(displayText, manualSourceEdited, teacherPraiseStage);
    if (teacherPraiseReply) {
      pushMessage('assistant', teacherPraiseReply);
      setTeacherPraiseStage('praised');
      return;
    }
    if (!confirmedDraft) {
      await createDraftFromText(text);
      return;
    }
    await rewriteSelectedTarget(text, confirmedDraft);
  }

  async function createDraftFromText(text: string) {
    const payload = parseInitialSkillPrompt(text);
    setLoading(true);
    setSourceAutoScroll(true);
    setStreamStatus('正在生成技能草稿');
    let streamBuffer = '';
    let latestPreview = createStreamingDraftSeed(payload);
    let latestPreviewSignature = JSON.stringify(latestPreview);
    setDraft(latestPreview);
    setSelectedPaths(DEFAULT_TARGET_PATHS);
    setHighlightedPaths([]);
    setUpdatingPaths([]);
    setTextDiffs([]);
    const assistantId = pushMessage('assistant', '', {
      thinking: 'running',
      thinkingDetails: ['正在理解技能目标与输入信息'],
      thinkingOpen: false,
    });
    const baseJob: ActiveDistillJob = {
      jobId: '',
      kind: 'distill',
      assistantId,
      lastSeq: 0,
      status: 'queued',
      createPayload: payload,
    };
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamPost(
        '/api/enterprise/skills/distill/stream',
        { tenant_id: TENANT_ID, ...payload },
        (item) => {
          trackActiveJobEvent(item, baseJob);
          if (item.event === 'status') {
            appendThinkingDetail(assistantId, String(item.data.text || '正在处理'));
            return;
          }
          if (item.event === 'chunk_reset') {
            streamBuffer = '';
            latestPreview = createStreamingDraftSeed(payload);
            latestPreviewSignature = JSON.stringify(latestPreview);
            setDraft(latestPreview);
            return;
          }
          if (item.event === 'chunk') {
            const content = typeof item.data.content === 'string' ? item.data.content : '';
            if (!content) return;
            streamBuffer += content;
            const preview = previewSkillFromStream(streamBuffer, latestPreview, payload);
            const previewSignature = JSON.stringify(preview);
            if (previewSignature !== latestPreviewSignature) {
              latestPreview = preview;
              latestPreviewSignature = previewSignature;
              setDraft(preview);
              setStreamStatus('正在解码技能结构');
            }
            return;
          }
          if (item.event === 'complete') {
            const draftSkill = item.data.draft_skill as SkillCard;
            const nextWarnings = Array.isArray(item.data.warnings) ? item.data.warnings.map(String) : [];
            const nextToolSuggestions = normalizeToolSuggestions(item.data.tool_suggestions);
            appendThinkingDetail(assistantId, `已生成技能草稿：${draftSkill.name}`);
            clearAnimationTimers();
            setDraft(draftSkill);
            setHighlightedPaths([]);
            setUpdatingPaths([]);
            setTextDiffs([]);
            setSelectedPaths(DEFAULT_TARGET_PATHS);
            updateMessage(
              assistantId,
              `已生成「${draftSkill.name}」草稿。你可以在右侧选择一个或多个区域继续改写。`,
              {
                thinking: 'done',
                warnings: nextWarnings,
                toolSuggestions: nextToolSuggestions,
                operations: [{ kind: 'skill_change', label: `生成技能草稿：${draftSkill.name}`, skillId: draftSkill.skill_id }],
              },
            );
            setStreamStatus('生成完成');
            if (nextToolSuggestions.length > 0) {
              void autoProbeToolSuggestions(assistantId, nextToolSuggestions);
            }
            setActiveJob(null);
            return;
          }
          if (item.event === 'error') {
            updateMessage(assistantId, String(item.data.message || '生成失败，当前草稿未变更。'), { thinking: 'done' });
            setActiveJob(null);
          }
        },
        controller.signal,
      );
    } catch (error) {
      if (controller.signal.aborted && !manualStopRef.current) return;
      appendThinkingDetail(assistantId, '生成失败，已保留当前草稿');
      updateMessage(assistantId, '生成失败，当前草稿未变更。', { thinking: 'done' });
      if (controller.signal.aborted) {
        message.info('已停止生成');
      } else {
        message.error(error instanceof Error ? error.message : '生成失败');
      }
    } finally {
      finishStream(controller);
    }
  }

  async function rewriteSelectedTarget(
    text: string,
    currentDraft: SkillCard | null = draft,
    targetPathsOverride?: string[],
    initialThinkingDetails?: string[],
    conversationOverride?: ChatItem[],
  ) {
    if (!currentDraft) return;
    setSourceAutoScroll(false);
    const previousDraft = cloneSkill(currentDraft);
    const targets = targetPathsOverride?.length
      ? targetPathsOverride
      : selectedPaths.length > 0
        ? selectedPaths
        : allTargetPaths(currentDraft);
    const scopeLabel = targetLabel(targets, currentDraft);
    setLoading(true);
    setStreamStatus('正在改写选中内容');
    const assistantId = pushMessage('assistant', '', {
      thinking: 'running',
      thinkingDetails: initialThinkingDetails || [`改写范围：${scopeLabel}`],
      thinkingOpen: false,
    });
    const baseJob: ActiveDistillJob = {
      jobId: '',
      kind: 'rewrite',
      assistantId,
      lastSeq: 0,
      status: 'queued',
      previousDraft,
      targets,
    };
    const controller = new AbortController();
    let receivedMessageChunk = false;
    manualStopRef.current = false;
    abortRef.current = controller;
    try {
      await streamPost(
        `/api/enterprise/skills/${encodeURIComponent(currentDraft.skill_id)}/rewrite/stream`,
        {
          tenant_id: TENANT_ID,
          current_skill: currentDraft,
          instruction: text,
          target_path: targets[0],
          target_paths: targets,
          target_label: scopeLabel,
          conversation: (conversationOverride || messages).map((item) => ({ role: item.role, content: item.content })),
        },
        (item) => {
          trackActiveJobEvent(item, baseJob);
          if (item.event === 'status') {
            appendThinkingDetail(assistantId, String(item.data.text || '正在处理'));
            return;
          }
          if (item.event === 'message_chunk') {
            const content = typeof item.data.content === 'string' ? item.data.content : '';
            if (content) {
              receivedMessageChunk = true;
              appendMessage(assistantId, content);
            }
            return;
          }
          if (item.event === 'complete') {
            const nextDraft = item.data.draft_skill as SkillCard;
            const nextWarnings = Array.isArray(item.data.warnings) ? item.data.warnings.map(String) : [];
            const nextToolSuggestions = normalizeToolSuggestions(item.data.tool_suggestions);
            const changedPaths = diffTargetPaths(previousDraft, nextDraft, targets);
            const changedLabel = changedPaths.length > 0 ? targetLabel(changedPaths, nextDraft) : '未检测到结构变化';
            appendThinkingDetail(assistantId, `模型返回改写结果：${changedLabel}`);
            appendThinkingDetail(assistantId, '右侧已更新预览，等待确认或拒绝');
            animateDraftChange(previousDraft, nextDraft, changedPaths);
            setPendingChange({ assistantId, previousDraft, nextDraft, changedPaths });
            setSelectedPaths((current) => reconcileSelectedPaths(current, nextDraft));
            setStreamStatus('改写完成');
            if (!receivedMessageChunk) {
              updateMessage(
                assistantId,
                String(item.data.assistant_message || '已完成局部改写。'),
                {
                  thinking: 'done',
                  warnings: nextWarnings,
                  toolSuggestions: nextToolSuggestions,
                  actionState: 'pending',
                  operations: changedPaths.length
                    ? [{ kind: 'skill_change', label: `改写：${changedLabel}`, skillId: nextDraft.skill_id }]
                    : [],
                },
              );
            } else {
              updateMessage(assistantId, undefined, {
                thinking: 'done',
                warnings: nextWarnings,
                toolSuggestions: nextToolSuggestions,
                actionState: 'pending',
                operations: changedPaths.length
                  ? [{ kind: 'skill_change', label: `改写：${changedLabel}`, skillId: nextDraft.skill_id }]
                  : [],
              });
            }
            if (nextToolSuggestions.length > 0) {
              void autoProbeToolSuggestions(assistantId, nextToolSuggestions);
            }
            setActiveJob(null);
            return;
          }
          if (item.event === 'error') {
            updateMessage(assistantId, String(item.data.message || '改写失败，当前草稿未变更。'), { thinking: 'done' });
            setActiveJob(null);
          }
        },
        controller.signal,
      );
    } catch (error) {
      if (controller.signal.aborted && !manualStopRef.current) return;
      appendThinkingDetail(assistantId, '改写失败，已保留当前草稿');
      updateMessage(assistantId, '改写失败，当前草稿未变更。', { thinking: 'done' });
      if (controller.signal.aborted) {
        message.info('已停止改写');
      } else {
        message.error(error instanceof Error ? error.message : '改写失败');
      }
    } finally {
      finishStream(controller);
    }
  }

  function openSaveReview(options: { clearAfterSave?: boolean } = {}) {
    const targetDraft = pendingChange?.nextDraft || draft;
    if (!targetDraft) return;
    if (!hasSkillContentChanges(targetDraft, lastSavedDraft)) {
      message.info('当前没有内容变化，无需保存草稿。');
      return;
    }
    confirmPendingChange(false);
    setClearAfterSave(Boolean(options.clearAfterSave));
    setSaveDraftSnapshot(targetDraft);
    setSaveName(targetDraft.name);
    setSaveDomain(targetDraft.business_domain || '');
    setSaveVersion(loadedSkill ? bumpSkillVersion(loadedSkill.version || targetDraft.version) : '1.0.0');
    setSaveReviewOpen(true);
  }

  async function saveDraft() {
    if (!saveReviewDraft) return;
    if (!hasSkillContentChanges(saveReviewDraft, lastSavedDraft)) {
      message.info('当前没有内容变化，无需保存草稿。');
      return;
    }
    const finalDraft = saveReviewDraft;
    try {
      let savedSkill: SkillRead;
      if (loadedSkill) {
        savedSkill = await api.put<SkillRead>(`/api/enterprise/skills/${loadedSkill.skill_id}${agentOnlyQuery}`, {
          tenant_id: TENANT_ID,
          content: finalDraft,
          status: loadedSkill.status,
        });
      } else {
        try {
          savedSkill = await api.post<SkillRead>(`/api/enterprise/skills${agentOnlyQuery}`, { tenant_id: TENANT_ID, content: finalDraft, status: 'draft' });
        } catch (error) {
          if (!(error instanceof Error) || !error.message.includes('409')) throw error;
          savedSkill = await api.put<SkillRead>(`/api/enterprise/skills/${finalDraft.skill_id}${agentOnlyQuery}`, {
            tenant_id: TENANT_ID,
            content: finalDraft,
            status: 'draft',
          });
        }
      }
      setLoadedSkill(savedSkill);
      setDraft(savedSkill.content);
      setLastSavedDraft(savedSkill.content);
      setSaveDraftSnapshot(null);
      setHighlightedPaths([]);
      setDirtyPaths([]);
      setSaveReviewOpen(false);
      appendOperationToLatestMessage({
        kind: 'version_save',
        label: `保存版本 ${savedSkill.version}`,
        skillId: savedSkill.skill_id,
        version: savedSkill.version,
      });
      if (clearAfterSave) {
        setClearAfterSave(false);
        clearDistillWorkspace();
        message.success('草稿已保存，当前改写已清空');
      } else {
        message.success('草稿已保存');
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存失败');
    }
  }

  function stopStream() {
    const jobId = activeJob?.jobId;
    manualStopRef.current = true;
    if (jobId) {
      void api.post(`/api/enterprise/skills/jobs/${encodeURIComponent(jobId)}/cancel`);
    }
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setActiveJob(null);
    setStreamStatus('已停止');
  }

  function trackActiveJobEvent(item: { event: string; data: Record<string, unknown> }, baseJob: ActiveDistillJob) {
    const jobId = typeof item.data.job_id === 'string' ? item.data.job_id : baseJob.jobId;
    if (!jobId) return;
    const seq = typeof item.data.seq === 'number' ? item.data.seq : baseJob.lastSeq;
    const status = typeof item.data.status === 'string' ? item.data.status : baseJob.status;
    setActiveJob((current) => ({
      ...baseJob,
      ...(current?.jobId === jobId ? current : {}),
      jobId,
      lastSeq: Math.max(current?.jobId === jobId ? current.lastSeq : 0, seq || 0),
      status,
    }));
  }

  function handleResumedJobEvent(job: ActiveDistillJob, item: { event: string; data: Record<string, unknown> }) {
    trackActiveJobEvent(item, job);
    if (item.event === 'status') {
      appendThinkingDetail(job.assistantId, String(item.data.text || '正在处理'));
      return;
    }
    if (item.event === 'message_chunk') {
      const content = typeof item.data.content === 'string' ? item.data.content : '';
      if (content) appendMessage(job.assistantId, content);
      return;
    }
    if (item.event === 'complete') {
      if (job.kind === 'distill') {
        completeResumedDistillJob(job, item.data);
      } else {
        completeResumedRewriteJob(job, item.data);
      }
      setActiveJob(null);
      return;
    }
    if (item.event === 'error') {
      updateMessage(job.assistantId, String(item.data.message || '生成失败'), { thinking: 'done' });
      setActiveJob(null);
      setLoading(false);
      return;
    }
    if (item.event === 'job_complete') {
      const status = String(item.data.status || '');
      if (status === 'failed') {
        updateMessage(job.assistantId, String(item.data.error || '生成失败'), { thinking: 'done' });
        setActiveJob(null);
      }
    }
  }

  function completeResumedDistillJob(job: ActiveDistillJob, data: Record<string, unknown>) {
    const draftSkill = data.draft_skill as SkillCard | undefined;
    if (!draftSkill) return;
    const nextWarnings = Array.isArray(data.warnings) ? data.warnings.map(String) : [];
    const nextToolSuggestions = normalizeToolSuggestions(data.tool_suggestions);
    clearAnimationTimers();
    setDraft(draftSkill);
    setHighlightedPaths([]);
    setUpdatingPaths([]);
    setTextDiffs([]);
    setSelectedPaths(DEFAULT_TARGET_PATHS);
    appendThinkingDetail(job.assistantId, `已生成技能草稿：${draftSkill.name}`);
    updateMessage(
      job.assistantId,
      `已生成「${draftSkill.name}」草稿。你可以在右侧选择一个或多个区域继续改写。`,
      {
        thinking: 'done',
        warnings: nextWarnings,
        toolSuggestions: nextToolSuggestions,
        operations: [{ kind: 'skill_change', label: `生成技能草稿：${draftSkill.name}`, skillId: draftSkill.skill_id }],
      },
    );
    setStreamStatus('生成完成');
    if (nextToolSuggestions.length > 0) {
      void autoProbeToolSuggestions(job.assistantId, nextToolSuggestions);
    }
  }

  function completeResumedRewriteJob(job: ActiveDistillJob, data: Record<string, unknown>) {
    const nextDraft = data.draft_skill as SkillCard | undefined;
    if (!nextDraft) return;
    const previousDraft = job.previousDraft || draft;
    if (!previousDraft) {
      setDraft(nextDraft);
      updateMessage(job.assistantId, String(data.assistant_message || '已完成改写。'), { thinking: 'done' });
      return;
    }
    const targets = job.targets?.length ? job.targets : allTargetPaths(previousDraft);
    const nextWarnings = Array.isArray(data.warnings) ? data.warnings.map(String) : [];
    const nextToolSuggestions = normalizeToolSuggestions(data.tool_suggestions);
    const changedPaths = diffTargetPaths(previousDraft, nextDraft, targets);
    const changedLabel = changedPaths.length > 0 ? targetLabel(changedPaths, nextDraft) : '未检测到结构变化';
    appendThinkingDetail(job.assistantId, `模型返回改写结果：${changedLabel}`);
    appendThinkingDetail(job.assistantId, '右侧已更新预览，等待确认或拒绝');
    animateDraftChange(previousDraft, nextDraft, changedPaths);
    setPendingChange({ assistantId: job.assistantId, previousDraft, nextDraft, changedPaths });
    setSelectedPaths((current) => reconcileSelectedPaths(current, nextDraft));
    setStreamStatus('改写完成');
    updateMessage(job.assistantId, String(data.assistant_message || '已完成局部改写。'), {
      thinking: 'done',
      warnings: nextWarnings,
      toolSuggestions: nextToolSuggestions,
      actionState: 'pending',
      operations: changedPaths.length
        ? [{ kind: 'skill_change', label: `改写：${changedLabel}`, skillId: nextDraft.skill_id }]
        : [],
    });
    if (nextToolSuggestions.length > 0) {
      void autoProbeToolSuggestions(job.assistantId, nextToolSuggestions);
    }
  }

  async function stageFileUpload(file: File) {
    if (loading) return;
    const suffix = file.name.toLowerCase().split('.').pop() || '';
    if (!['md', 'txt', 'doc', 'docx'].includes(suffix)) {
      message.error('仅支持 .md、.doc、.docx、.txt 文件');
      return;
    }
    const id = `file_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    const controller = new AbortController();
    uploadControllersRef.current[id] = controller;
    setAttachments((current) => [...current, { id, name: file.name, status: 'uploading' }]);
    try {
      const contentBase64 = await fileToBase64(file);
      if (controller.signal.aborted) return;
      const result = await api.postWithSignal<{ filename: string; text: string }>(
        '/api/enterprise/skills/files/extract',
        {
          filename: file.name,
          content_base64: contentBase64,
        },
        controller.signal,
      );
      setAttachments((current) =>
        current.map((item) =>
          item.id === id ? { id, name: result.filename, status: 'ready', text: result.text } : item,
        ),
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      setAttachments((current) =>
        current.map((item) =>
          item.id === id
            ? { ...item, status: 'error', error: error instanceof Error ? error.message : '读取文件失败' }
            : item,
        ),
      );
    } finally {
      delete uploadControllersRef.current[id];
    }
  }

  function uploadFiles(files: File[]) {
    files.forEach((file) => {
      void stageFileUpload(file);
    });
  }

  function cancelAttachment(id: string) {
    uploadControllersRef.current[id]?.abort();
    delete uploadControllersRef.current[id];
    setAttachments((current) => current.filter((item) => item.id !== id));
  }

  function handleComposerPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.files || []);
    if (files.length === 0) return;
    event.preventDefault();
    uploadFiles(files);
  }

  function handleDragEnter(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepthRef.current += 1;
    if (event.dataTransfer.types.includes('Files')) setDragActive(true);
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
  }

  function handleDragLeave(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragActive(false);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepthRef.current = 0;
    setDragActive(false);
    uploadFiles(Array.from(event.dataTransfer.files || []));
  }

  function openToolDetail(messageId: string, suggestion: ToolSuggestionItem) {
    setToolDetailMessageId(messageId);
    setToolDetail(suggestion);
    setProbeArgsText(JSON.stringify(suggestion.sample_arguments || {}, null, 2));
  }

  async function autoProbeToolSuggestions(messageId: string, suggestions: ToolSuggestionItem[]) {
    const extractedSuggestions = suggestions.filter((suggestion) => suggestion.resolution_status !== 'incomplete');
    const pendingSuggestions = suggestions.filter(
      (suggestion) => toolSuggestionResolution(suggestion) === 'new_candidate' && !suggestion.probe_result,
    );
    if (extractedSuggestions.length === 0) return;

    appendThinkingDetail(
      messageId,
      `正在抽取工具：${extractedSuggestions.map((item) => item.display_name || item.name).join('、')}`,
    );
    const existingSuggestions = suggestions.filter((suggestion) => toolSuggestionResolution(suggestion) === 'existing');
    existingSuggestions.forEach((suggestion) => {
      appendThinkingDetail(messageId, `已匹配现有工具：${suggestion.matched_tool_display_name || suggestion.display_name || suggestion.name}`);
    });
    if (pendingSuggestions.length === 0) return;
    appendThinkingDetail(messageId, '正在测试工具接口');
    setStreamStatus('正在测试工具接口');

    let successCount = 0;
    let failureCount = 0;
    for (const suggestion of pendingSuggestions) {
      const result = await probeToolSuggestion(messageId, suggestion, {
        silent: true,
        allowWhileLoading: true,
      });
      if (!result) {
        failureCount += 1;
        appendThinkingDetail(messageId, `工具测试失败：${suggestion.display_name || suggestion.name}`);
        continue;
      }
      if (result.success) {
        successCount += 1;
        appendThinkingDetail(messageId, `工具测试成功：${suggestion.display_name || suggestion.name}`);
      } else {
        failureCount += 1;
        const reason = result.error?.message ? `，${result.error.message}` : '';
        appendThinkingDetail(messageId, `工具测试失败：${suggestion.display_name || suggestion.name}${reason}`);
      }
    }

    appendThinkingDetail(messageId, `工具测试完成：${successCount} 个成功，${failureCount} 个失败`);
    setStreamStatus('工具测试完成');
  }

  async function probeToolSuggestion(
    messageId: string,
    suggestion: ToolSuggestionItem,
    options: ProbeToolOptions = {},
  ): Promise<ToolProbeResponse | null> {
    if (toolSuggestionResolution(suggestion) !== 'new_candidate') return null;
    if ((!options.allowWhileLoading && loading) || suggestion.probeStatus === 'probing') return null;
    const args = options.sampleArguments || suggestion.sample_arguments || {};
    if (Object.keys(args).length === 0) {
      if (!options.silent) message.warning('缺少样例参数，无法测试接口');
      const result: ToolProbeResponse = {
        success: false,
        inferred_output_schema: {},
        error: { code: 'MISSING_SAMPLE_ARGUMENTS', message: '缺少样例参数，无法测试接口' },
      };
      setToolSuggestionPatch(messageId, suggestion.name, { probeStatus: 'error', probe_result: result });
      return result;
    }
    setToolSuggestionPatch(messageId, suggestion.name, { probeStatus: 'probing' });
    try {
      const payload = {
        ...toolPayloadFromSuggestion(suggestion, (pendingChange?.nextDraft || draft)?.skill_id),
        sample_arguments: args,
      };
      const result = await api.post<ToolProbeResponse>('/api/enterprise/tools/probe', payload);
      const nextOutputSchema = result.success && result.inferred_output_schema
        ? result.inferred_output_schema
        : suggestion.output_schema;
      setToolSuggestionPatch(messageId, suggestion.name, {
        probeStatus: result.success ? 'success' : 'error',
        probe_result: result,
        sample_arguments: args,
        output_schema: nextOutputSchema || {},
      });
      if (result.success) {
        if (!options.silent) message.success('接口测试成功');
      } else {
        if (!options.silent) message.error(result.error?.message || '接口测试失败');
      }
      return result;
    } catch (error) {
      const result: ToolProbeResponse = {
        success: false,
        inferred_output_schema: {},
        error: { code: 'CLIENT_ERROR', message: error instanceof Error ? error.message : '接口测试失败' },
      };
      setToolSuggestionPatch(messageId, suggestion.name, {
        probeStatus: 'error',
        probe_result: result,
      });
      if (!options.silent) message.error(result.error?.message || '接口测试失败');
      return result;
    }
  }

  function applyProbeArgumentsFromDetail() {
    if (!toolDetail || !toolDetailMessageId) return;
    const parsed = parseJsonObject(probeArgsText);
    if (!parsed) {
      message.error('样例参数必须是 JSON 对象');
      return;
    }
    setToolSuggestionPatch(toolDetailMessageId, toolDetail.name, { sample_arguments: parsed });
    setToolDetail({ ...toolDetail, sample_arguments: parsed });
    message.success('样例参数已更新');
  }

  function probeToolDetail() {
    if (!toolDetail || !toolDetailMessageId) return;
    const parsed = parseJsonObject(probeArgsText);
    if (!parsed) {
      message.error('样例参数必须是 JSON 对象');
      return;
    }
    void probeToolSuggestion(toolDetailMessageId, { ...toolDetail, sample_arguments: parsed }, { sampleArguments: parsed });
  }

  async function confirmToolSuggestion(messageId: string, suggestion: ToolSuggestionItem) {
    if (loading) return;
    if (toolSuggestionResolution(suggestion) !== 'new_candidate') {
      message.warning('该工具不是可新增候选');
      return;
    }
    if (!suggestion.probe_result?.success) {
      message.warning('请先测试接口成功后再新增工具');
      return;
    }
    const nextSuggestions = nextToolSuggestionsWithPatch(messageId, suggestion.name, { status: 'accepted' });
    setToolSuggestionStatus(messageId, suggestion.name, 'accepted');
    const shouldCommit = toolSuggestionSelectionsComplete(nextSuggestions);
    if (!shouldCommit) {
      message.success('已确认，等待其他工具建议处理完成后统一更新技能');
      return;
    }
    await commitToolSuggestionSelections(messageId, nextSuggestions);
  }

  async function commitToolSuggestionSelections(messageId: string, suggestions: ToolSuggestionItem[]) {
    const activeDraft = pendingChange?.nextDraft || draft;
    const acceptedSuggestions = suggestions.filter(
      (item) => toolSuggestionResolution(item) === 'new_candidate' && item.status === 'accepted',
    );
    if (acceptedSuggestions.length === 0) {
      message.info('所有工具建议已拒绝，技能草稿未变更');
      return;
    }
    try {
      const createdTools: ToolRead[] = [];
      const createdNewTools: ToolRead[] = [];
      for (const suggestion of acceptedSuggestions) {
        if (!suggestion.probe_result?.success) {
          throw new Error(`工具「${suggestion.display_name || suggestion.name}」尚未测试通过`);
        }
        const payload = toolPayloadFromSuggestion(suggestion, activeDraft?.skill_id);
        let createdTool: ToolRead;
        let createdNewTool = false;
        try {
          createdTool = await api.post<ToolRead>('/api/enterprise/tools', payload);
          createdNewTool = true;
        } catch (error) {
          if (!(error instanceof Error) || !error.message.includes('409')) throw error;
          createdTool = toolReadFromSuggestion(suggestion, activeDraft?.skill_id);
        }
        createdTools.push(createdTool);
        if (createdNewTool) createdNewTools.push(createdTool);
        setToolSuggestionStatus(messageId, suggestion.name, 'created');
      }
      setTools((current) => createdTools.reduce((nextTools, tool) => upsertToolRead(nextTools, tool), current));
      createdNewTools.forEach((createdTool) => {
        appendOperationToMessage(messageId, {
          kind: 'tool_add',
          label: `新增工具：${createdTool.display_name || createdTool.name}`,
          toolId: createdTool.id,
          toolName: createdTool.name,
        });
      });
      if (!activeDraft) return;
      message.success(`已确认 ${acceptedSuggestions.length} 个工具，正在统一更新技能`);
      confirmPendingChange(false);
      await rewriteSelectedTarget(
        buildToolIntegrationInstruction(acceptedSuggestions),
        activeDraft,
        allTargetPaths(activeDraft),
        [
          `已新增工具：${acceptedSuggestions.map((item) => item.display_name || item.name).join('、')}`,
          '正在统一判断这些工具应接入哪些节点',
        ],
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : '新增工具或更新技能失败');
    }
  }

  function rejectToolSuggestion(messageId: string, toolName: string) {
    const nextSuggestions = nextToolSuggestionsWithPatch(messageId, toolName, { status: 'rejected' });
    setToolSuggestionStatus(messageId, toolName, 'rejected');
    removeToolActionFromDraft(toolName);
    if (toolSuggestionSelectionsComplete(nextSuggestions)) {
      void commitToolSuggestionSelections(messageId, nextSuggestions);
    }
  }

  function removeToolActionFromDraft(toolName: string) {
    setDraft((current) => (current ? removeToolActionFromSkill(current, toolName) : current));
    setPendingChange((current) =>
      current
        ? {
            ...current,
            nextDraft: removeToolActionFromSkill(current.nextDraft, toolName),
          }
        : current,
    );
  }

  function setToolSuggestionStatus(messageId: string, toolName: string, status: ToolSuggestionItem['status']) {
    setToolSuggestionPatch(messageId, toolName, { status });
  }

  function nextToolSuggestionsWithPatch(
    messageId: string,
    toolName: string,
    patch: Partial<ToolSuggestionItem>,
  ): ToolSuggestionItem[] {
    const targetMessage = messages.find((item) => item.id === messageId);
    return (targetMessage?.toolSuggestions || []).map((suggestion) =>
      suggestion.name === toolName ? { ...suggestion, ...patch } : suggestion,
    );
  }

  function setToolSuggestionPatch(messageId: string, toolName: string, patch: Partial<ToolSuggestionItem>) {
    setMessages((current) =>
      current.map((item) =>
        item.id === messageId
          ? {
              ...item,
              toolSuggestions: (item.toolSuggestions || []).map((suggestion) =>
                suggestion.name === toolName ? { ...suggestion, ...patch } : suggestion,
              ),
            }
          : item,
      ),
    );
    setToolDetail((current) => (current?.name === toolName ? { ...current, ...patch } : current));
  }

  function closeSaveReview() {
    setSaveReviewOpen(false);
    setSaveDraftSnapshot(null);
    setClearAfterSave(false);
  }

  function handleClearClick() {
    if (loading) return;
    if (!hasUnsavedSkillChanges()) {
      Modal.confirm({
        title: '清空当前改写？',
        content: '当前技能没有未保存变更，确认清空当前改写内容和对话记录？',
        okText: '清空',
        cancelText: '取消',
        onOk: clearDistillWorkspace,
      });
      return;
    }
    setClearConfirmOpen(true);
  }

  function hasUnsavedSkillChanges() {
    const targetDraft = pendingChange?.nextDraft || draft;
    if (!targetDraft) return false;
    return hasSkillContentChanges(targetDraft, lastSavedDraft);
  }

  function clearDistillWorkspace() {
    clearAnimationTimers();
    abortRef.current?.abort();
    Object.values(uploadControllersRef.current).forEach((controller) => controller.abort());
    uploadControllersRef.current = {};
    setDraft(null);
    setLoadedSkill(null);
    setLastSavedDraft(null);
    setMessages(DEFAULT_DISTILL_MESSAGES);
    setInput('');
    setSelectedPaths(DEFAULT_TARGET_PATHS);
    setHighlightedPaths([]);
    setUpdatingPaths([]);
    setDirtyPaths([]);
    setTextDiffs([]);
    setPendingChange(null);
    setSaveDraftSnapshot(null);
    setSaveReviewOpen(false);
    setClearConfirmOpen(false);
    setClearAfterSave(false);
    setAttachments([]);
    setStreamStatus('');
    setActiveJob(null);
    setManualSourceEdited(false);
    setTeacherPraiseStage('idle');
  }

  function toggleTarget(target: TargetSelection) {
    setSelectedPaths((current) => {
      if (current.includes(target.path)) {
        return current.filter((path) => path !== target.path);
      }
      return [...current, target.path];
    });
  }

  function handleSourceEdit(nextDraft: SkillCard, path: string) {
    setDraft(nextDraft);
    setManualSourceEdited(true);
    setDirtyPaths((current) => mergePaths(current, [path]));
    setHighlightedPaths((current) => mergePaths(current, [path]));
  }

  function toggleAllTargets() {
    setSelectedPaths(allSelected ? [] : allPaths);
  }

  function pushMessage(role: ChatItem['role'], content: string, extra: Partial<ChatItem> = {}) {
    const id = `${role}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    setMessages((current) => [...current, { id, role, content, createdAt: new Date().toISOString(), ...extra }]);
    return id;
  }

  function updateMessage(id: string, content?: string, extra: Partial<ChatItem> = {}) {
    setMessages((current) =>
      current.map((item) => (item.id === id ? { ...item, ...(content === undefined ? {} : { content }), ...extra } : item)),
    );
  }

  function appendOperationToMessage(id: string, operation: DistillHistoryOperation) {
    setMessages((current) =>
      current.map((item) =>
        item.id === id ? { ...item, operations: [...(item.operations || []), operation] } : item,
      ),
    );
  }

  function appendOperationToLatestMessage(operation: DistillHistoryOperation) {
    setMessages((current) => {
      const index = [...current].reverse().findIndex((item) => item.role === 'assistant' || item.role === 'user');
      if (index < 0) return current;
      const targetIndex = current.length - 1 - index;
      return current.map((item, currentIndex) =>
        currentIndex === targetIndex ? { ...item, operations: [...(item.operations || []), operation] } : item,
      );
    });
  }

  function appendMessage(id: string, content: string) {
    setMessages((current) =>
      current.map((item) => (item.id === id ? { ...item, content: `${item.content}${content}` } : item)),
    );
  }

  function appendThinkingDetail(id: string, detail: string) {
    const nextDetail = detail.trim();
    if (!nextDetail) return;
    setMessages((current) =>
      current.map((item) => {
        if (item.id !== id) return item;
        const previous = item.thinkingDetails || [];
        if (previous[previous.length - 1] === nextDetail) return item;
        return { ...item, thinkingDetails: [...previous, nextDetail] };
      }),
    );
  }

  function toggleThinking(id: string) {
    setMessages((current) =>
      current.map((item) => (item.id === id ? { ...item, thinkingOpen: !item.thinkingOpen } : item)),
    );
  }

  function finishStream(controller: AbortController) {
    if (abortRef.current === controller) abortRef.current = null;
    setLoading(false);
  }

  function createHistorySnapshot(): DistillHistorySnapshot {
    return {
      draft: draft ? cloneSkill(draft) : null,
      loadedSkill: loadedSkill ? cloneSkillRead(loadedSkill) : null,
      lastSavedDraft: lastSavedDraft ? cloneSkill(lastSavedDraft) : null,
      selectedPaths: [...selectedPaths],
      highlightedPaths: [...highlightedPaths],
      updatingPaths: [...updatingPaths],
      dirtyPaths: [...dirtyPaths],
      textDiffs: textDiffs.map((item) => ({ ...item })),
      pendingChange: pendingChange
        ? {
            assistantId: pendingChange.assistantId,
            previousDraft: cloneSkill(pendingChange.previousDraft),
            nextDraft: cloneSkill(pendingChange.nextDraft),
            changedPaths: [...pendingChange.changedPaths],
          }
        : null,
      viewMode,
      tools: tools.map((tool) => ({ ...tool })),
      attachments: attachments.map((item) => ({ ...item })),
      streamStatus,
    };
  }

  function restoreHistorySnapshot(snapshot: DistillHistorySnapshot) {
    clearAnimationTimers();
    abortRef.current?.abort();
    setDraft(snapshot.draft ? cloneSkill(snapshot.draft) : null);
    setLoadedSkill(snapshot.loadedSkill ? cloneSkillRead(snapshot.loadedSkill) : null);
    setLastSavedDraft(snapshot.lastSavedDraft ? cloneSkill(snapshot.lastSavedDraft) : null);
    setSelectedPaths([...snapshot.selectedPaths]);
    setHighlightedPaths([...snapshot.highlightedPaths]);
    setUpdatingPaths([...snapshot.updatingPaths]);
    setDirtyPaths([...snapshot.dirtyPaths]);
    setTextDiffs(snapshot.textDiffs.map((item) => ({ ...item })));
    setPendingChange(
      snapshot.pendingChange
        ? {
            assistantId: snapshot.pendingChange.assistantId,
            previousDraft: cloneSkill(snapshot.pendingChange.previousDraft),
            nextDraft: cloneSkill(snapshot.pendingChange.nextDraft),
            changedPaths: [...snapshot.pendingChange.changedPaths],
          }
        : null,
    );
    setViewMode(snapshot.viewMode);
    setTools(snapshot.tools.map((tool) => ({ ...tool })));
    setAttachments(snapshot.attachments.filter((item) => item.status !== 'uploading').map((item) => ({ ...item })));
    setStreamStatus(snapshot.streamStatus);
    setActiveJob(null);
    setLoading(false);
  }

  function confirmPendingChange(showToast = true) {
    if (!pendingChange) return;
    clearAnimationTimers();
    setDraft(pendingChange.nextDraft);
    setUpdatingPaths([]);
    setTextDiffs([]);
    updateMessage(pendingChange.assistantId, undefined, { actionState: 'confirmed' });
    setPendingChange(null);
    if (showToast) message.success('已确认改写');
  }

  function rejectPendingChange() {
    if (!pendingChange) return;
    clearAnimationTimers();
    setDraft(pendingChange.previousDraft);
    setHighlightedPaths([]);
    setUpdatingPaths([]);
    setTextDiffs([]);
    updateMessage(pendingChange.assistantId, undefined, { actionState: 'rejected' });
    setPendingChange(null);
    message.info('已拒绝改写并还原');
  }

  function requestEditHistoryMessage(item: ChatItem, index: number) {
    if (loading || item.role !== 'user') return;
    setEditingMessage({ id: item.id, text: visibleChatContent(item) });
  }

  async function copyHistoryMessage(item: ChatItem) {
    const text = visibleChatContent(item);
    try {
      await navigator.clipboard.writeText(text);
      message.success('已复制');
    } catch {
      message.error('复制失败');
    }
  }

  function cancelEditingMessage() {
    setEditingMessage(null);
  }

  function submitEditingMessage() {
    if (!editingMessage || loading) return;
    const text = editingMessage.text.trim();
    if (!text) return;
    const index = messages.findIndex((item) => item.id === editingMessage.id);
    const item = index >= 0 ? messages[index] : null;
    if (!item || item.role !== 'user') {
      setEditingMessage(null);
      return;
    }
    const outgoingText = buildEditedOutgoingText(item, text);
    const snapshot = item.snapshotBefore;
    if (!snapshot) {
      updateMessage(item.id, text, { outgoingText });
      setEditingMessage(null);
      return;
    }
    const rollbackOperations = collectRollbackOperations(messages.slice(index + 1));
    if (rollbackOperations.length === 0) {
      void rerunEditedMessage(index, snapshot, rollbackOperations, text, outgoingText);
      return;
    }
    Modal.confirm({
      title: '重新编辑这条消息？',
      content: (
        <div>
          <Typography.Paragraph>
            重新编辑会回到这条消息发送前的技能草稿，并截断之后的推理记录。
          </Typography.Paragraph>
          <div className="rollback-operation-list">
            {rollbackOperations.map((operation, operationIndex) => (
              <Tag key={`${operation.kind}_${operationIndex}`}>{operation.label}</Tag>
            ))}
          </div>
        </div>
      ),
      okText: '确认回退',
      cancelText: '取消',
      onOk: () => {
        void rerunEditedMessage(index, snapshot, rollbackOperations, text, outgoingText);
      },
    });
  }

  async function rerunEditedMessage(
    index: number,
    snapshot: DistillHistorySnapshot,
    operations: DistillHistoryOperation[],
    displayText: string,
    outgoingText: string,
  ) {
    try {
      await rollbackPersistedOperations(snapshot, operations);
      const confirmedDraft = snapshot.pendingChange?.nextDraft || snapshot.draft;
      restoreHistorySnapshot({
        ...snapshot,
        draft: confirmedDraft ? cloneSkill(confirmedDraft) : null,
        pendingChange: null,
        updatingPaths: [],
        textDiffs: [],
      });
      const editedUser: ChatItem = {
        id: `user_${Date.now()}_${Math.random().toString(16).slice(2)}`,
        role: 'user',
        content: displayText,
        outgoingText,
        createdAt: new Date().toISOString(),
        snapshotBefore: snapshot,
      };
      const previousMessages = messages.slice(0, index);
      const nextMessages = [...previousMessages, editedUser];
      setMessages(nextMessages);
      setEditingMessage(null);
      if (!confirmedDraft) {
        await createDraftFromText(outgoingText);
        return;
      }
      await rewriteSelectedTarget(outgoingText, confirmedDraft, undefined, undefined, nextMessages);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '回退失败');
    }
  }

  async function rollbackPersistedOperations(
    snapshot: DistillHistorySnapshot,
    operations: DistillHistoryOperation[],
  ) {
    const toolOps = operations.filter((operation) => operation.kind === 'tool_add' && operation.toolId);
    for (const operation of toolOps) {
      try {
        await api.delete(`/api/enterprise/tools/${encodeURIComponent(String(operation.toolId))}?tenant_id=${TENANT_ID}${agentQuery}`);
      } catch {
        // Tool may already have been removed. Local state is restored from the snapshot below.
      }
    }

    const versionOps = operations.filter((operation) => operation.kind === 'version_save' && operation.skillId);
    for (const operation of versionOps) {
      const skillId = String(operation.skillId);
      if (snapshot.loadedSkill) {
        await api.put<SkillRead>(`/api/enterprise/skills/${encodeURIComponent(snapshot.loadedSkill.skill_id)}${agentOnlyQuery}`, {
          tenant_id: TENANT_ID,
          content: snapshot.loadedSkill.content,
          status: snapshot.loadedSkill.status,
        });
        if (operation.version && operation.version !== snapshot.loadedSkill.version) {
          try {
            await api.delete(
              `/api/enterprise/skills/${encodeURIComponent(skillId)}/versions/${encodeURIComponent(operation.version)}?tenant_id=${TENANT_ID}${agentQuery}`,
            );
          } catch {
            // A saved version may be shared with current state or already removed. The active draft has been restored.
          }
        }
      } else {
        try {
          await api.delete(`/api/enterprise/skills/${encodeURIComponent(skillId)}?tenant_id=${TENANT_ID}${agentQuery}`);
        } catch {
          // If the skill was not persisted, there is nothing else to roll back.
        }
      }
    }
  }

  function animateDraftChange(
    previousDraft: SkillCard,
    nextDraft: SkillCard,
    changedPaths: string[],
    markDelay = 520,
  ) {
    clearAnimationTimers();
    const paths = changedPaths;
    if (paths.length === 0) {
      setDraft(nextDraft);
      setHighlightedPaths([]);
      setUpdatingPaths([]);
      setTextDiffs([]);
      return;
    }
    const nextTextDiffs = collectTextDiffs(previousDraft, nextDraft, paths);
    setHighlightedPaths(paths);
    setUpdatingPaths(paths);
    setTextDiffs(nextTextDiffs);
    setDraft(previousDraft);
    const startTimer = window.setTimeout(() => {
      setTextDiffs((current) => current.map((diff) => ({ ...diff, phase: 'type', progress: 0 })));
      const steps = 24;
      let tick = 0;
      const interval = window.setInterval(() => {
        tick += 1;
        const progress = Math.min(tick / steps, 1);
        setTextDiffs((current) => current.map((diff) => ({ ...diff, phase: 'type', progress })));
        setDraft(typedDraft(previousDraft, nextDraft, nextTextDiffs, progress));
        if (progress >= 1) {
          window.clearInterval(interval);
          animationTimersRef.current = animationTimersRef.current.filter((timer) => timer !== interval);
          setTextDiffs((current) => current.map((diff) => ({ ...diff, phase: 'settled', progress: 1 })));
          setDraft(nextDraft);
          setUpdatingPaths([]);
          setDirtyPaths((current) => mergePaths(current, paths));
        }
      }, 38);
      animationTimersRef.current.push(interval);
    }, markDelay);
    animationTimersRef.current.push(startTimer);
  }

  function clearAnimationTimers() {
    animationTimersRef.current.forEach((timer) => {
      window.clearTimeout(timer);
      window.clearInterval(timer);
    });
    animationTimersRef.current = [];
  }

  return (
    <div className="skill-distill-page">
      <div className="page-title">
        <Typography.Title level={3}>SOP 学习</Typography.Title>
      </div>
      <div className="skill-workbench">
        <Card
          className={`skill-chat-card ${dragActive ? 'dragging' : ''}`}
          onDragEnter={handleDragEnter}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="skill-chat-panel">
            {dragActive && <div className="skill-upload-drop-hint">松开上传文档</div>}
            <div className="skill-chat-messages" ref={chatMessagesRef}>
              {messages.map((item, index) => (
                <div key={item.id} className={`skill-chat-row ${item.role}`}>
                  <div
                    className={`skill-chat-bubble ${editingMessage?.id === item.id ? 'editing' : ''} ${
                      item.role === 'user' && item.attachments?.length ? 'has-attachments' : ''
                    }`}
                  >
                    {item.role === 'assistant' && item.thinking && (
                      <div className={`skill-chat-thinking-block ${item.thinking}`}>
                        <button
                          type="button"
                          className="skill-chat-thinking"
                          onClick={() => toggleThinking(item.id)}
                        >
                          {item.thinking === 'running' ? <LoadingOutlined /> : <CheckOutlined />}
                          <span>{item.thinking === 'running' ? '正在学习' : '学习记录'}</span>
                          {item.thinkingOpen ? <DownOutlined /> : <RightOutlined />}
                        </button>
                        {item.thinkingOpen && (
                          <div className="skill-chat-thinking-details">
                            {(item.thinkingDetails || []).map((detail, index) => (
                              <div key={`${item.id}_detail_${index}`} className="skill-chat-thinking-detail">
                                {detail}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                    {item.role === 'user' && item.attachments && item.attachments.length > 0 && (
                      <div className="skill-chat-attachments">
                        {item.attachments.map((attachment) => (
                          <div className="skill-chat-attachment" key={attachment.id} title={attachment.name}>
                            <span className="skill-chat-attachment-icon">
                              <FileTextOutlined />
                            </span>
                            <span className="skill-chat-attachment-main">
                              <span className="skill-chat-attachment-name">{attachment.name}</span>
                              <span className="skill-chat-attachment-type">{attachment.type}</span>
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    {item.role === 'user' && editingMessage?.id === item.id ? (
                      <div className="skill-chat-edit-panel">
                        <Input.TextArea
                          value={editingMessage.text}
                          autoSize={{ minRows: 2, maxRows: 8 }}
                          autoFocus
                          onChange={(event) => setEditingMessage({ id: item.id, text: event.target.value })}
                          onPressEnter={(event) => {
                            if (!event.shiftKey && !event.nativeEvent.isComposing) {
                              event.preventDefault();
                              submitEditingMessage();
                            }
                          }}
                        />
                        <div className="skill-chat-edit-actions">
                          <Button onClick={cancelEditingMessage}>取消</Button>
                          <Button type="primary" onClick={submitEditingMessage} disabled={!(editingMessage?.text || '').trim()}>
                            发送
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <>
                        {item.content ? (
                          <div className="skill-chat-content">{visibleChatContent(item)}</div>
                        ) : item.role === 'assistant' && item.thinking === 'running' ? null : item.role === 'assistant' ? (
                          '正在处理...'
                        ) : null}
                        {item.role === 'user' && (
                          <div className="skill-chat-hover-actions">
                            <span className="skill-chat-time">{formatMessageTime(item.createdAt)}</span>
                            <button type="button" title="复制" onClick={() => void copyHistoryMessage(item)}>
                              <CopyGlyph />
                            </button>
                            <button
                              type="button"
                              title="修改"
                              onClick={() => requestEditHistoryMessage(item, index)}
                              disabled={loading}
                            >
                              <PencilGlyph />
                            </button>
                          </div>
                        )}
                      </>
                    )}
                    {item.warnings && item.warnings.length > 0 && (() => {
                      const warnings = compactWarningItems(item.warnings || [], item.toolSuggestions);
                      if (warnings.length === 0) return null;
                      return (
                        <div className="skill-chat-warning">
                          <div className="skill-chat-warning-title">
                            <WarningOutlined />
                            <span>提示</span>
                          </div>
                          {warnings.map((warning, index) => (
                            <div key={`${item.id}_warning_${index}`} className="skill-chat-warning-item" title={warning.title}>
                              {warning.text}
                            </div>
                          ))}
                        </div>
                      );
                    })()}
                    {item.toolSuggestions && item.toolSuggestions.length > 0 && (
                      <div className="skill-tool-suggestions">
                        {item.toolSuggestions.map((suggestion) => {
                          const canResolveSuggestion =
                            toolSuggestionResolution(suggestion) === 'new_candidate' &&
                            suggestion.status !== 'accepted' &&
                            suggestion.status !== 'created' &&
                            suggestion.status !== 'rejected';
                          return (
                            <div className="skill-tool-suggestion" key={`${item.id}_${suggestion.name}`}>
                              <div className="skill-tool-suggestion-main">
                                <div className="skill-tool-suggestion-head">
                                  <div className="skill-tool-suggestion-title">{toolSuggestionTitle(suggestion)}</div>
                                  <span className={`skill-tool-status ${toolSuggestionStatusClass(suggestion)}`}>
                                    {toolSuggestionStatusText(suggestion)}
                                  </span>
                                </div>
                                <div className="skill-tool-suggestion-desc">
                                  {suggestion.reason || suggestion.description || suggestion.name}
                                </div>
                                <div className="skill-tool-suggestion-meta">
                                  <span className="skill-tool-method">{suggestion.method || 'POST'}</span>
                                  <span>{suggestion.url || '-'}</span>
                                </div>
                              </div>
                              <div className="skill-tool-suggestion-actions top">
                                <span className="skill-tool-action-group detail">
                                  <Tooltip title="查看详情">
                                    <Button
                                      className="skill-tool-action"
                                      size="small"
                                      type="text"
                                      icon={<InfoCircleOutlined />}
                                      onClick={() => openToolDetail(item.id, suggestion)}
                                    />
                                  </Tooltip>
                                </span>
                                {canResolveSuggestion && (
                                  <span className="skill-tool-action-group decision">
                                    <Tooltip title="确认新增">
                                      <Button
                                        className="skill-tool-action confirm"
                                        size="small"
                                        type="text"
                                        disabled={!suggestion.probe_result?.success}
                                        icon={<CheckCircleOutlined />}
                                        onClick={() => void confirmToolSuggestion(item.id, suggestion)}
                                      />
                                    </Tooltip>
                                    <Tooltip title="拒绝">
                                      <Button
                                        className="skill-tool-action reject"
                                        size="small"
                                        type="text"
                                        icon={<CloseCircleOutlined />}
                                        onClick={() => rejectToolSuggestion(item.id, suggestion.name)}
                                      />
                                    </Tooltip>
                                  </span>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                    {item.actionState === 'pending' && (
                      <div className="skill-chat-confirm">
                        <Button size="small" type="primary" onClick={() => confirmPendingChange()}>
                          确认
                        </Button>
                        <Button size="small" onClick={rejectPendingChange}>
                          拒绝
                        </Button>
                      </div>
                    )}
                    {item.actionState === 'confirmed' && <div className="skill-chat-decision">已确认</div>}
                    {item.actionState === 'rejected' && <div className="skill-chat-decision">已拒绝</div>}
                  </div>
                </div>
              ))}
            </div>
            <div
              className="skill-chat-composer"
            >
              {attachments.length > 0 && (
                <div className="skill-upload-list">
                  {attachments.map((attachment) => (
                    <div className={`skill-upload-item ${attachment.status}`} key={attachment.id}>
                      <FileTextOutlined />
                      <span className="skill-upload-name">{attachment.name}</span>
                      <span className="skill-upload-status">
                        {attachment.status === 'uploading' && '读取中'}
                        {attachment.status === 'ready' && '已读取'}
                        {attachment.status === 'error' && (attachment.error || '读取失败')}
                      </span>
                      <Button
                        size="small"
                        type="text"
                        icon={<CloseOutlined />}
                        onClick={() => cancelAttachment(attachment.id)}
                      />
                    </div>
                  ))}
                </div>
              )}
              <Input.TextArea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onPaste={handleComposerPaste}
                onPressEnter={(event) => {
                  if (!event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void send();
                  }
                }}
                rows={4}
                placeholder={
                  draft
                    ? '说明你要如何改写右侧选中的部分'
                    : '输入“标题：... 原始SOP文本：...”或直接粘贴流程说明'
                }
              />
              <div className="skill-chat-actions">
                <Typography.Text type="secondary">{streamStatus}</Typography.Text>
                <Space>
                  <Upload
                    accept=".md,.txt,.doc,.docx"
                    multiple
                    showUploadList={false}
                    beforeUpload={(file) => {
                      void stageFileUpload(file as File);
                      return false;
                    }}
                  >
                    <Button icon={<UploadOutlined />} loading={uploadingFile} disabled={loading}>
                      上传文件
                    </Button>
                  </Upload>
                  {loading && (
                    <Button icon={<StopOutlined />} onClick={stopStream}>
                      停止
                    </Button>
                  )}
                  <Button
                    type="primary"
                    icon={<SendOutlined />}
                    loading={loading}
                    disabled={uploadingFile || (!input.trim() && readyAttachments.length === 0)}
                    onClick={() => void send()}
                  >
                    发送
                  </Button>
                </Space>
              </div>
            </div>
          </div>
        </Card>
        <Card
          className="skill-source-card"
          title={viewMode === 'source' ? '源码' : '流程图'}
          extra={
            <Space>
              <Button disabled={loading} onClick={handleClearClick}>
                清空
              </Button>
              <Tooltip title={draft && !hasSaveableDraftChanges ? '当前没有内容变化' : ''}>
                <Button disabled={!draft || loading || !hasSaveableDraftChanges} icon={<SaveOutlined />} onClick={() => openSaveReview()}>
                  保存草稿
                </Button>
              </Tooltip>
            </Space>
          }
        >
          <div className="skill-source-toolbar">
            <Space>
              <Button
                icon={viewMode === 'source' ? <BranchesOutlined /> : <CodeOutlined />}
                onClick={() => setViewMode(viewMode === 'source' ? 'flow' : 'source')}
              >
                {viewMode === 'source' ? '显示流程' : '显示源码'}
              </Button>
              <Button disabled={!draft} onClick={toggleAllTargets}>
                {allSelected ? '清空选择' : '全选'}
              </Button>
            </Space>
          </div>
          {!draft ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无技能草稿" />
          ) : viewMode === 'source' ? (
            <SkillSource
              skill={draft}
              selectedPaths={selectedPaths}
              highlightedPaths={highlightedPaths}
              updatingPaths={updatingPaths}
              dirtyPaths={dirtyPaths}
              textDiffs={textDiffs}
              toolDescriptions={toolDescriptions}
              toolStatuses={toolStatuses}
              containerRef={sourceScrollRef}
              onToggle={toggleTarget}
              onEdit={handleSourceEdit}
            />
          ) : (
            <SkillFlow
              skill={draft}
              selectedPaths={selectedPaths}
              highlightedPaths={highlightedPaths}
              updatingPaths={updatingPaths}
              dirtyPaths={dirtyPaths}
              textDiffs={textDiffs}
              toolDescriptions={toolDescriptions}
              toolStatuses={toolStatuses}
              containerRef={sourceScrollRef}
              onToggle={toggleTarget}
            />
          )}
        </Card>
      </div>
      <Modal
        open={clearConfirmOpen}
        title="清空前是否保存？"
        width={520}
        onCancel={() => setClearConfirmOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setClearConfirmOpen(false)}>取消</Button>
            <Button
              onClick={() => {
                setClearConfirmOpen(false);
                clearDistillWorkspace();
              }}
            >
              不保存清空
            </Button>
            <Button
              type="primary"
              onClick={() => {
                setClearConfirmOpen(false);
                openSaveReview({ clearAfterSave: true });
              }}
            >
              保存并清空
            </Button>
          </Space>
        }
      >
        <Typography.Paragraph>
          检测到当前技能有未保存变更。你可以先保存当前技能版本，保存成功后会自动清空当前改写内容。
        </Typography.Paragraph>
      </Modal>
      <Modal
        open={saveReviewOpen}
        title="保存技能版本"
        okText="保存"
        cancelText="取消"
        width={820}
        okButtonProps={{ disabled: !saveReviewHasContentChanges }}
        onOk={() => void saveDraft()}
        onCancel={closeSaveReview}
      >
        <div className="save-review-form">
          <label>
            <span>技能名称</span>
            <Input value={saveName} onChange={(event) => setSaveName(event.target.value)} />
          </label>
          <label>
            <span>业务域</span>
            <Input value={saveDomain} onChange={(event) => setSaveDomain(event.target.value)} />
          </label>
          <label>
            <span>版本号</span>
            <Input value={saveVersion} disabled={!saveReviewHasContentChanges} onChange={(event) => setSaveVersion(event.target.value)} />
          </label>
        </div>
        <div className="save-review-diff">
          <Typography.Text strong>本轮修改 diff</Typography.Text>
          {saveReviewDiffs.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无结构差异" />
          ) : (
            saveReviewDiffs.map((diff) => (
              <div key={diff.key} className="save-review-diff-row">
                <div className="save-review-diff-path">{diffTargetLabel(diff.path, saveReviewDraft)} / {fieldLabel(diff.field)}</div>
                <SaveReviewDiffValue diff={diff} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
              </div>
            ))
          )}
        </div>
      </Modal>
      <Modal
        open={Boolean(toolDetail)}
        title="工具详情"
        footer={
          <Space className="tool-suggestion-detail-footer">
            <Button onClick={() => setToolDetail(null)}>关闭</Button>
            {toolDetail && toolSuggestionResolution(toolDetail) === 'new_candidate' && (
              <>
                <Button onClick={applyProbeArgumentsFromDetail}>应用样例参数</Button>
                <Button
                  type="primary"
                  icon={<ApiOutlined />}
                  loading={toolDetail?.probeStatus === 'probing'}
                  onClick={probeToolDetail}
                >
                  {toolDetail?.probe_result ? '再次测试' : '测试接口'}
                </Button>
              </>
            )}
          </Space>
        }
        width={1040}
        onCancel={() => setToolDetail(null)}
      >
        {toolDetail && (
          <div className="tool-suggestion-detail">
            <div><strong>解析状态：</strong>{toolSuggestionResolutionLabel(toolDetail)}</div>
            {toolDetail.matched_tool_name && (
              <div><strong>匹配工具：</strong>{toolDetail.matched_tool_display_name || toolDetail.matched_tool_name}</div>
            )}
            <div><strong>工具名：</strong>{toolDetail.name}</div>
            <div><strong>显示名：</strong>{toolDetail.display_name || '-'}</div>
            <div><strong>说明：</strong>{toolDetail.description || '-'}</div>
            <div><strong>方法：</strong>{toolDetail.method}</div>
            <div><strong>URL：</strong>{toolDetail.url}</div>
            {toolDetail.missing_reason && <div><strong>缺失原因：</strong>{toolDetail.missing_reason}</div>}
            <div><strong>原因：</strong>{toolDetail.reason || '-'}</div>
            <div><strong>来源：</strong>{toolDetail.source_excerpt || '-'}</div>
            <Typography.Text strong>样例参数</Typography.Text>
            <Input.TextArea
              value={probeArgsText}
              autoSize={{ minRows: 4, maxRows: 10 }}
              onChange={(event) => setProbeArgsText(event.target.value)}
            />
            <Typography.Text strong>输入 Schema</Typography.Text>
            <pre>{JSON.stringify(toolDetail.input_schema || {}, null, 2)}</pre>
            <Typography.Text strong>输出 Schema</Typography.Text>
            <pre>{JSON.stringify(toolDetail.output_schema || {}, null, 2)}</pre>
            {toolDetail.probe_result && (
              <>
                <Typography.Text strong>测试结果</Typography.Text>
                <pre>{JSON.stringify(toolDetail.probe_result, null, 2)}</pre>
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

function SkillSource({
  skill,
  selectedPaths,
  highlightedPaths,
  updatingPaths,
  dirtyPaths,
  textDiffs,
  toolDescriptions,
  toolStatuses,
  containerRef,
  onToggle,
  onEdit,
}: {
  skill: SkillCard;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  dirtyPaths: string[];
  textDiffs: TextDiffAnimation[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  containerRef: RefObject<HTMLDivElement>;
  onToggle: (target: TargetSelection) => void;
  onEdit: (nextDraft: SkillCard, path: string) => void;
}) {
  function editBasic(field: keyof SkillCard, value: string | string[]) {
    const next = cloneSkill(skill);
    if (field === 'trigger_intents' || field === 'user_utterance_examples' || field === 'goal' || field === 'required_info' || field === 'response_rules') {
      next[field] = Array.isArray(value) ? value : splitEditableList(value);
    } else if (field === 'skill_id' || field === 'name' || field === 'version' || field === 'business_domain' || field === 'description') {
      next[field] = String(value);
    }
    onEdit(next, 'basic');
  }

  function editStep(index: number, field: string, value: string | string[]) {
    const next = cloneSkill(skill);
    const listValue = field === 'expected_user_info' || field === 'allowed_actions'
      ? Array.isArray(value)
        ? value
        : splitEditableList(value)
      : value;
    next.nodes = Array.isArray(next.nodes) ? [...next.nodes] : [];
    const currentNode = { ...(next.nodes[index] || {}) };
    const nodeField = field === 'step_id' ? 'node_id' : field;
    currentNode[nodeField] = listValue;
    next.nodes[index] = currentNode;
    onEdit(next, stepTargetPath(index));
  }

  return (
    <div className="skill-source-md" ref={containerRef}>
      <div className="skill-source-group-title">基础信息</div>
      <SelectableTarget
        className={targetClass('skill-source-section', 'basic', selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
        target={{ path: 'basic', label: '基础信息' }}
        onToggle={onToggle}
      >
        {selectedPaths.includes('basic') && <span className="selection-mark"><CheckOutlined /></span>}
        <div className="skill-source-rendered">
          <EditableSourceHeading value={skill.name} onChange={(value) => editBasic('name', value)} />
          <div className="skill-source-meta-list">
            <EditableSourceTextLine label={fieldLabel('skill_id')} value={skill.skill_id} onChange={(value) => editBasic('skill_id', value)} />
            <EditableSourceTextLine label={fieldLabel('version')} value={skill.version} onChange={(value) => editBasic('version', value)} />
            <EditableSourceTextLine label={fieldLabel('business_domain')} value={skill.business_domain || ''} onChange={(value) => editBasic('business_domain', value)} />
            <EditableSourceTextLine label={fieldLabel('description')} value={skill.description || ''} multiline onChange={(value) => editBasic('description', value)} />
            <EditableSourceListLine label={fieldLabel('trigger_intents')} values={skill.trigger_intents} onChange={(value) => editBasic('trigger_intents', value)} />
            <EditableSourceListLine label={fieldLabel('user_utterance_examples')} values={skill.user_utterance_examples} onChange={(value) => editBasic('user_utterance_examples', value)} />
            <EditableSourceListLine label={fieldLabel('goal')} values={skill.goal} onChange={(value) => editBasic('goal', value)} />
            <EditableSourceListLine label={fieldLabel('required_info')} values={skill.required_info} onChange={(value) => editBasic('required_info', value)} />
            <EditableSourceListLine label={fieldLabel('response_rules')} values={skill.response_rules} onChange={(value) => editBasic('response_rules', value)} />
          </div>
        </div>
      </SelectableTarget>
      <div className="skill-source-group-title">详细节点</div>
      <div className="skill-source-steps">
        {skillGraphSteps(skill).map((step, index) => {
          const stepId = String(step.step_id || `step_${index + 1}`);
          const path = stepTargetPath(index);
          return (
            <SelectableTarget
              key={path}
              className={targetClass('skill-source-section', path, selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
              target={{ path, label: `节点 ${index + 1}：${step.name || stepId}` }}
              onToggle={onToggle}
            >
              {selectedPaths.includes(path) && <span className="selection-mark"><CheckOutlined /></span>}
              <div className="skill-source-rendered">
                <EditableSourceStepHeading
                  index={index}
                  value={String(step.name || '')}
                  fallback={stepId}
                  onChange={(value) => editStep(index, 'name', value)}
                />
                <div className="skill-source-meta-list">
                  <EditableSourceTextLine label={fieldLabel('step_id')} value={stepId} onChange={(value) => editStep(index, 'step_id', value)} />
                  <EditableSourceTextLine label={fieldLabel('type')} value={String(step.type || 'collect_info')} onChange={(value) => editStep(index, 'type', value)} />
                  <EditableSourceTextLine label={fieldLabel('condition')} value={String(step.condition || '')} onChange={(value) => editStep(index, 'condition', value)} />
                  <EditableSourceTextLine label={fieldLabel('instruction')} value={String(step.instruction || '')} multiline onChange={(value) => editStep(index, 'instruction', value)} />
                  <EditableSourceListLine label={fieldLabel('expected_user_info')} values={asStringList(step.expected_user_info)} onChange={(value) => editStep(index, 'expected_user_info', value)} />
                  <EditableSourceActionLine
                    values={asStringList(step.allowed_actions)}
                    toolDescriptions={toolDescriptions}
                    toolStatuses={toolStatuses}
                    onChange={(value) => editStep(index, 'allowed_actions', value)}
                  />
                </div>
              </div>
            </SelectableTarget>
          );
        })}
      </div>
    </div>
  );
}

function SkillFlow({
  skill,
  selectedPaths,
  highlightedPaths,
  updatingPaths,
  dirtyPaths,
  textDiffs,
  toolDescriptions,
  toolStatuses,
  containerRef,
  onToggle,
}: {
  skill: SkillCard;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  dirtyPaths: string[];
  textDiffs: TextDiffAnimation[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  containerRef: RefObject<HTMLDivElement>;
  onToggle: (target: TargetSelection) => void;
}) {
  const [flowZoom, setFlowZoom] = useState(0.64);
  const nodes = skillGraphSteps(skill);
  const edgeMap = skillGraphEdgeMap(skill);
  const terminalSet = new Set(asStringList(skill.terminal_node_ids));
  const nodeNameMap = Object.fromEntries(
    nodes.map((node, index) => {
      const nodeId = String(node.node_id || node.step_id || `node_${index + 1}`);
      return [nodeId, String(node.name || nodeId)];
    }),
  );
  const graphKey = `${skill.skill_id || 'skill'}:${skill.version || 'draft'}:${nodes.length}:${skill.start_node_id || ''}`;
  const centeredGraphKey = useRef('');
  const graphLayout = buildSkillFlowCanvasLayout(skill, nodes, nodeNameMap);
  const zoomedWidth = graphLayout.width * flowZoom;
  const zoomedHeight = graphLayout.height * flowZoom;
  const updateZoom = (nextZoom: number) => {
    const next = Math.min(1.18, Math.max(0.54, Math.round(nextZoom * 100) / 100));
    const container = containerRef.current;
    if (!container) {
      setFlowZoom(next);
      return;
    }
    const centerX = (container.scrollLeft + container.clientWidth / 2) / flowZoom;
    const centerY = (container.scrollTop + container.clientHeight / 2) / flowZoom;
    setFlowZoom(next);
    window.requestAnimationFrame(() => {
      container.scrollLeft = Math.max(0, centerX * next - container.clientWidth / 2);
      container.scrollTop = Math.max(0, centerY * next - container.clientHeight / 2);
    });
  };
  useEffect(() => {
    const container = containerRef.current;
    if (!container || centeredGraphKey.current === graphKey) return undefined;
    centeredGraphKey.current = graphKey;
    const frame = window.requestAnimationFrame(() => {
      const rootCenterX = (graphLayout.root.x + graphLayout.root.width / 2) * flowZoom;
      const targetScrollLeft = Math.max(0, rootCenterX - container.clientWidth / 2);
      container.scrollLeft = targetScrollLeft;
      container.scrollTop = 0;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [containerRef, flowZoom, graphKey, graphLayout.root.x, graphLayout.root.width]);
  return (
    <>
      <div className="skill-flow-zoom-toolbar" aria-label="流程图缩放">
        <span>缩放</span>
        <Button size="small" onClick={() => updateZoom(flowZoom - 0.08)}>-</Button>
        <span className="skill-flow-zoom-value">{Math.round(flowZoom * 100)}%</span>
        <Button size="small" onClick={() => updateZoom(flowZoom + 0.08)}>+</Button>
        <Button size="small" onClick={() => updateZoom(0.64)}>适配</Button>
        <Button size="small" onClick={() => updateZoom(1)}>100%</Button>
      </div>
      <div className="skill-flow" ref={containerRef}>
        <div
          className="skill-flow-zoom-shell"
          style={{ width: zoomedWidth, height: zoomedHeight }}
        >
          <div
            className="skill-flow-graph-canvas"
            style={{
              width: graphLayout.width,
              height: graphLayout.height,
              transform: `scale(${flowZoom})`,
            }}
          >
            <svg
              className="skill-flow-edges"
              width={graphLayout.width}
              height={graphLayout.height}
              viewBox={`0 0 ${graphLayout.width} ${graphLayout.height}`}
              aria-hidden="true"
            >
              <defs>
                <marker id="skill-flow-arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" />
                </marker>
              </defs>
              {graphLayout.edges.map((edge) => (
                <path
                  className="skill-flow-edge-path"
                  d={edge.path}
                  key={edge.id}
                  markerEnd="url(#skill-flow-arrow)"
                  strokeDasharray="6 14"
                >
                  <title>{edge.title}</title>
                </path>
              ))}
            </svg>
            {graphLayout.edges.map((edge) => (
              <span
                className={['skill-flow-edge-label', edge.labelTone || edge.kind].filter(Boolean).join(' ')}
                key={`${edge.id}_label`}
                style={{ left: edge.labelX, top: edge.labelY }}
                title={edge.title}
              >
                {edge.label}
              </span>
            ))}
            <div
              className="skill-flow-root-position"
              style={{ left: graphLayout.root.x, top: graphLayout.root.y, width: graphLayout.root.width, height: graphLayout.root.height }}
            >
              <SelectableTarget
                className={targetClass('skill-flow-node root', 'basic', selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
                target={{ path: 'basic', label: '基础信息' }}
                onToggle={onToggle}
              >
                {selectedPaths.includes('basic') && <span className="selection-mark"><CheckOutlined /></span>}
                <span>基础信息</span>
                <strong><InlineDiffText path="basic" field="name" value={skill.name} diffs={textDiffs} /></strong>
                <small>{skill.skill_id}</small>
                <p><InlineDiffText path="basic" field="description" value={skill.description || '暂无描述'} diffs={textDiffs} /></p>
                <div className="skill-flow-meta">
                  <FlowMetaRow label="业务域">
                    <span className="skill-flow-chip">{skill.business_domain || '-'}</span>
                  </FlowMetaRow>
                  <FlowMetaRow label="必填信息">
                    <PlainChipList values={skill.required_info} />
                  </FlowMetaRow>
                  <FlowMetaRow label="触发意图">
                    <PlainChipList values={skill.trigger_intents} />
                  </FlowMetaRow>
                </div>
              </SelectableTarget>
            </div>
            {graphLayout.nodes.map((item) => (
              <div
                className="skill-flow-node-position"
                key={item.nodeId}
                style={{ left: item.x, top: item.y, width: item.width, height: item.height }}
              >
                <SkillFlowNodeCard
                  index={item.index}
                  step={item.step}
                  terminal={terminalSet.has(item.nodeId)}
                  outgoingEdges={edgeMap[item.nodeId] || []}
                  selectedPaths={selectedPaths}
                  highlightedPaths={highlightedPaths}
                  updatingPaths={updatingPaths}
                  dirtyPaths={dirtyPaths}
                  textDiffs={textDiffs}
                  toolDescriptions={toolDescriptions}
                  toolStatuses={toolStatuses}
                  onToggle={onToggle}
                />
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

function SkillFlowNodeCard({
  index,
  step,
  terminal,
  outgoingEdges,
  selectedPaths,
  highlightedPaths,
  updatingPaths,
  dirtyPaths,
  textDiffs,
  toolDescriptions,
  toolStatuses,
  onToggle,
}: {
  index: number;
  step: Record<string, unknown>;
  terminal: boolean;
  outgoingEdges: Array<Record<string, unknown>>;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  dirtyPaths: string[];
  textDiffs: TextDiffAnimation[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  onToggle: (target: TargetSelection) => void;
}) {
  const nodeId = String(step.node_id || step.step_id || `node_${index + 1}`);
  const path = stepTargetPath(index);
  const expectedInfo = asStringList(step.expected_user_info);
  const actionList = asStringList(step.allowed_actions);
  const instruction = String(step.instruction || '暂无说明');
  return (
    <div className="skill-flow-node-shell">
      <SelectableTarget
        className={targetClass('skill-flow-node', path, selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
        target={{ path, label: `节点 ${index + 1}：${step.name || nodeId}` }}
        onToggle={onToggle}
      >
        {selectedPaths.includes(path) && <span className="selection-mark"><CheckOutlined /></span>}
        <span>节点 {index + 1}</span>
        <strong><InlineDiffText path={path} field="name" value={String(step.name || nodeId)} diffs={textDiffs} /></strong>
        <small>{nodeId}</small>
        <div className="skill-flow-node-badges">
          <span className="skill-flow-chip">{nodeTypeLabel(String(step.type || 'collect_info'))}</span>
          {Boolean(step.optional) && <span className="skill-flow-chip">可选</span>}
          {terminal && <span className="skill-flow-chip terminal">终止</span>}
        </div>
        <p className="skill-flow-node-summary" title={instruction}>
          <InlineDiffText path={path} field="instruction" value={instruction} diffs={textDiffs} />
        </p>
        <div className="skill-flow-compact-meta">
          {expectedInfo.length > 0 && (
            <div className="skill-flow-compact-row">
              <span>字段</span>
              <PlainChipList values={expectedInfo.slice(0, 4)} />
            </div>
          )}
          {actionList.length > 0 && (
            <div className="skill-flow-compact-row">
              <span>动作</span>
              <ActionList actions={actionList.slice(0, 4)} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
            </div>
          )}
          {outgoingEdges.length > 0 && <span className="skill-flow-route-count">{outgoingEdges.length} 条流转</span>}
        </div>
      </SelectableTarget>
    </div>
  );
}

function FlowMetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="skill-flow-meta-row">
      <span className="skill-flow-meta-label">{label}</span>
      {children}
    </div>
  );
}

function PlainChipList({ values }: { values: unknown }) {
  const items = asStringList(values);
  if (items.length === 0) return <span className="skill-flow-chip muted">-</span>;
  return (
    <div className="skill-flow-chip-list">
      {items.map((item, index) => (
        <span className="skill-flow-chip" key={`${item}_${index}`}>
          {item}
        </span>
      ))}
    </div>
  );
}

function skillGraphSteps(skill: SkillCard): Array<Record<string, unknown>> {
  if (Array.isArray(skill.nodes) && skill.nodes.length > 0) {
    return skill.nodes.map((node, index) => ({
      step_id: node.node_id || `node_${index + 1}`,
      node_id: node.node_id || `node_${index + 1}`,
      type: node.type || 'collect_info',
      name: node.name || node.node_id || `节点 ${index + 1}`,
      instruction: node.instruction || '',
      optional: Boolean(node.optional),
      condition: node.condition || '',
      expected_user_info: asStringList(node.expected_user_info),
      allowed_actions: asStringList(node.allowed_actions),
      knowledge_scope: isRecord(node.knowledge_scope) ? node.knowledge_scope : {},
      retry_policy: isRecord(node.retry_policy) ? node.retry_policy : {},
      metadata: isRecord(node.metadata) ? node.metadata : {},
    }));
  }
  return [];
}

function skillGraphEdgeMap(skill: SkillCard): Record<string, Array<Record<string, unknown>>> {
  const map: Record<string, Array<Record<string, unknown>>> = {};
  (Array.isArray(skill.edges) ? skill.edges : []).forEach((edge) => {
    const source = String(edge.source_node_id || '');
    if (!source) return;
    if (!map[source]) map[source] = [];
    map[source].push(edge);
  });
  return map;
}

type SkillFlowCanvasNode = {
  nodeId: string;
  step: Record<string, unknown>;
  index: number;
  rank: number;
  x: number;
  y: number;
  width: number;
  height: number;
};

type SkillFlowCanvasEdge = {
  id: string;
  kind: 'root' | 'edge';
  labelTone?: 'root' | 'branch' | 'return';
  label: string;
  title: string;
  path: string;
  labelX: number;
  labelY: number;
};

function buildSkillFlowCanvasLayout(
  skill: SkillCard,
  nodes: Array<Record<string, unknown>>,
  nodeNameMap: Record<string, string>,
) {
  const layerLayout = buildSkillFlowLayout(skill, nodes);
  const cardWidth = 360;
  const cardHeight = 324;
  const rootWidth = 500;
  const rootHeight = 270;
  const columnGap = 188;
  const rowGap = 236;
  const rootGap = 126;
  const paddingX = 144;
  const paddingY = 66;
  const layerWidths = layerLayout.layers.map((layer) => (
    layer.length * cardWidth + Math.max(0, layer.length - 1) * columnGap
  ));
  const maxContentWidth = Math.max(rootWidth, ...layerWidths, 0);
  const width = Math.max(1180, paddingX * 2 + maxContentWidth);
  const root = {
    x: (width - rootWidth) / 2,
    y: paddingY,
    width: rootWidth,
    height: rootHeight,
  };
  const positionedNodes: SkillFlowCanvasNode[] = [];
  const positionMap = new Map<string, SkillFlowCanvasNode>();

  layerLayout.layers.forEach((layer, layerIndex) => {
    const layerWidth = layer.length * cardWidth + Math.max(0, layer.length - 1) * columnGap;
    const layerStartX = Math.max(paddingX, (width - layerWidth) / 2);
    layer.forEach((item, itemIndex) => {
      const positioned = {
        ...item,
        rank: layerIndex,
        x: layerStartX + itemIndex * (cardWidth + columnGap),
        y: paddingY + rootHeight + rootGap + layerIndex * (cardHeight + rowGap),
        width: cardWidth,
        height: cardHeight,
      };
      positionedNodes.push(positioned);
      positionMap.set(item.nodeId, positioned);
    });
  });

  const rawEdges = Array.isArray(skill.edges) ? skill.edges : [];
  const edgeSiblingCounts = rawEdges.reduce<Record<string, number>>((acc, edge) => {
    const sourceId = String(edge.source_node_id || '');
    if (sourceId) acc[sourceId] = (acc[sourceId] || 0) + 1;
    return acc;
  }, {});
  const incomingCounts = rawEdges.reduce<Record<string, number>>((acc, edge) => {
    const targetId = String(edge.next_node_id || '');
    if (targetId) acc[targetId] = (acc[targetId] || 0) + 1;
    return acc;
  }, {});
  const edgeSiblingIndexes: Record<string, number> = {};
  const incomingIndexes: Record<string, number> = {};
  const layoutEdges: SkillFlowCanvasEdge[] = [];
  const height = paddingY * 2 + rootHeight + rootGap + layerLayout.layers.length * cardHeight + Math.max(0, layerLayout.layers.length - 1) * rowGap;
  const startNode = positionMap.get(String(skill.start_node_id || positionedNodes[0]?.nodeId || ''));
  if (startNode) {
    const sourceX = root.x + root.width / 2;
    const sourceY = root.y + root.height + 8;
    const targetX = startNode.x + startNode.width / 2;
    const targetY = startNode.y - 8;
    const laneY = edgeLaneY(sourceY, targetY, 0, 1);
    const labelAnchor = avoidFlowLabelOverlap(
      { x: (sourceX + targetX) / 2, y: laneY },
      [...positionedNodes, { ...root, nodeId: '__root__' } as SkillFlowCanvasNode],
      width,
      height,
    );
    layoutEdges.push({
      id: `root_${startNode.nodeId}`,
      kind: 'root',
      labelTone: 'root',
      label: '开始',
      title: `开始 -> ${nodeNameMap[startNode.nodeId] || startNode.nodeId}`,
      path: forwardFlowPath(sourceX, sourceY, targetX, targetY, laneY),
      labelX: labelAnchor.x,
      labelY: labelAnchor.y,
    });
  }
  rawEdges.forEach((edge, index) => {
    const sourceId = String(edge.source_node_id || '');
    const targetId = String(edge.next_node_id || '');
    const source = positionMap.get(sourceId);
    const target = positionMap.get(targetId);
    if (!source || !target) return;
    const label = edgeDisplayLabel(edge, nodeNameMap);
    const title = incomingEdgeLabel(edge, nodeNameMap);
    const siblingCount = edgeSiblingCounts[sourceId] || 1;
    const siblingIndex = edgeSiblingIndexes[sourceId] || 0;
    edgeSiblingIndexes[sourceId] = siblingIndex + 1;
    const incomingCount = incomingCounts[targetId] || 1;
    const incomingIndex = incomingIndexes[targetId] || 0;
    incomingIndexes[targetId] = incomingIndex + 1;
    const sourceX = source.x + source.width / 2;
    const sourceY = source.y + source.height + 8;
    const targetX = target.x + target.width / 2;
    const targetY = target.y - 8;
    const isReturn = targetY <= sourceY;
    const laneY = edgeLaneY(sourceY, targetY, siblingIndex, siblingCount);
    const shouldAvoidNodes = !isReturn && forwardRouteHitsNode(source, target, positionedNodes, laneY);
    const path = isReturn
      ? sideReturnFlowPath(source, target, width, siblingIndex)
      : shouldAvoidNodes
        ? sideForwardFlowPath(source, target, positionedNodes, width, siblingIndex, incomingIndex)
        : forwardFlowPath(sourceX, sourceY, targetX, targetY, laneY);
    const labelAnchor = isReturn
      ? returnEdgeLabelPosition(source, target, width, siblingIndex)
      : shouldAvoidNodes
        ? sideForwardEdgeLabelPosition(source, target, positionedNodes, width, siblingIndex, incomingIndex)
        : forwardEdgeLabelPosition(sourceX, targetX, laneY, siblingIndex, siblingCount, incomingIndex, incomingCount);
    const safeLabelAnchor = avoidFlowLabelOverlap(labelAnchor, positionedNodes, width, height);
    layoutEdges.push({
      id: `${sourceId}_${targetId}_${index}`,
      kind: 'edge',
      label: compactEdgeLabel(label),
      title,
      path,
      labelX: safeLabelAnchor.x,
      labelY: safeLabelAnchor.y,
      labelTone: isReturn ? 'return' : (siblingCount > 1 ? 'branch' : 'root'),
    });
  });

  return { width, height, root, nodes: positionedNodes, edges: layoutEdges };
}

function buildSkillFlowLayout(skill: SkillCard, nodes: Array<Record<string, unknown>>) {
  const byId = new Map(nodes.map((node, index) => [
    String(node.node_id || node.step_id || `node_${index + 1}`),
    { node, index },
  ]));
  const edgeMap = skillGraphEdgeMap(skill);
  const startId = String(skill.start_node_id || nodes[0]?.node_id || nodes[0]?.step_id || '');
  const start = startId && byId.has(startId)
    ? startId
    : nodes.length > 0
      ? String(nodes[0].node_id || nodes[0].step_id || 'node_1')
      : '';
  const reachable = new Set<string>();
  const queue = start ? [start] : [];
  while (queue.length > 0) {
    const nodeId = queue.shift() || '';
    if (!nodeId || reachable.has(nodeId) || !byId.has(nodeId)) continue;
    reachable.add(nodeId);
    (edgeMap[nodeId] || [])
      .slice()
      .sort((a, b) => Number(a.priority || 0) - Number(b.priority || 0))
      .forEach((edge) => {
        const nextId = String(edge.next_node_id || '');
        if (nextId && byId.has(nextId) && !reachable.has(nextId)) queue.push(nextId);
      });
  }

  const ranks = new Map<string, number>();
  if (start) ranks.set(start, 0);
  for (let pass = 0; pass < nodes.length + 2; pass += 1) {
    let changed = false;
    (Array.isArray(skill.edges) ? skill.edges : []).forEach((edge) => {
      const sourceId = String(edge.source_node_id || '');
      const targetId = String(edge.next_node_id || '');
      if (!reachable.has(sourceId) || !reachable.has(targetId)) return;
      const sourceRank = ranks.get(sourceId);
      if (sourceRank === undefined) return;
      const nextRank = sourceRank + 1;
      if ((ranks.get(targetId) ?? -1) < nextRank) {
        ranks.set(targetId, nextRank);
        changed = true;
      }
    });
    if (!changed) break;
  }

  const layerMap = new Map<number, Array<{ nodeId: string; step: Record<string, unknown>; index: number }>>();
  const orderedReachable = nodes
    .map((node, index) => ({
      nodeId: String(node.node_id || node.step_id || `node_${index + 1}`),
      step: node,
      index,
    }))
    .filter((item) => reachable.has(item.nodeId));
  orderedReachable.forEach((item) => {
    const rank = ranks.get(item.nodeId) ?? 0;
    if (!layerMap.has(rank)) layerMap.set(rank, []);
    layerMap.get(rank)?.push(item);
  });

  const layers = Array.from(layerMap.entries())
    .sort(([a], [b]) => a - b)
    .map(([, layer]) => layer);

  const remainder = nodes
    .map((node, index) => ({
      nodeId: String(node.node_id || node.step_id || `node_${index + 1}`),
      step: node,
      index,
    }))
    .filter((item) => !reachable.has(item.nodeId));
  if (remainder.length > 0) layers.push(remainder);
  return { layers };
}

function edgeLaneY(sourceY: number, targetY: number, siblingIndex: number, siblingCount: number): number {
  const safeTop = sourceY + 58;
  const safeBottom = targetY - 58;
  if (safeBottom <= safeTop) {
    return sourceY + Math.max(72, (targetY - sourceY) * 0.42);
  }
  const laneCount = Math.max(1, siblingCount);
  const maxSpread = Math.min(38, Math.max(24, (safeBottom - safeTop) / Math.max(1, laneCount - 1 || 1)));
  const start = (safeTop + safeBottom) / 2 - ((laneCount - 1) * maxSpread) / 2;
  return Math.max(safeTop, Math.min(safeBottom, start + siblingIndex * maxSpread));
}

function forwardFlowPath(sourceX: number, sourceY: number, targetX: number, targetY: number, laneY: number): string {
  const safeLaneY = Math.max(sourceY + 48, Math.min(targetY - 48, laneY));
  const verticalEase = Math.min(44, Math.max(22, (targetY - sourceY) * 0.18));
  const horizontalGap = Math.abs(targetX - sourceX);
  if (horizontalGap < 12) {
    return [
      `M ${sourceX} ${sourceY}`,
      `C ${sourceX} ${sourceY + verticalEase}, ${targetX} ${targetY - verticalEase}, ${targetX} ${targetY}`,
    ].join(' ');
  }
  const bend = Math.max(44, Math.min(120, horizontalGap * 0.28));
  return [
    `M ${sourceX} ${sourceY}`,
    `C ${sourceX} ${sourceY + verticalEase}, ${sourceX} ${safeLaneY - verticalEase}, ${sourceX} ${safeLaneY}`,
    `C ${sourceX + Math.sign(targetX - sourceX) * bend} ${safeLaneY}, ${targetX - Math.sign(targetX - sourceX) * bend} ${safeLaneY}, ${targetX} ${safeLaneY}`,
    `C ${targetX} ${safeLaneY + verticalEase}, ${targetX} ${targetY - verticalEase}, ${targetX} ${targetY}`,
  ].join(' ');
}

function rectsOverlap(
  a: { left: number; right: number; top: number; bottom: number },
  b: { left: number; right: number; top: number; bottom: number },
) {
  return !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom);
}

function segmentHitsFlowNode(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  node: SkillFlowCanvasNode,
  margin = 18,
) {
  const segmentRect = {
    left: Math.min(x1, x2) - margin,
    right: Math.max(x1, x2) + margin,
    top: Math.min(y1, y2) - margin,
    bottom: Math.max(y1, y2) + margin,
  };
  const nodeRect = {
    left: node.x,
    right: node.x + node.width,
    top: node.y,
    bottom: node.y + node.height,
  };
  return rectsOverlap(segmentRect, nodeRect);
}

function forwardRouteHitsNode(
  source: SkillFlowCanvasNode,
  target: SkillFlowCanvasNode,
  nodes: SkillFlowCanvasNode[],
  laneY: number,
) {
  const sourceX = source.x + source.width / 2;
  const sourceY = source.y + source.height + 8;
  const targetX = target.x + target.width / 2;
  const targetY = target.y - 8;
  const safeLaneY = Math.max(sourceY + 48, Math.min(targetY - 48, laneY));
  return nodes.some((node) => {
    if (node.nodeId === source.nodeId || node.nodeId === target.nodeId) return false;
    return segmentHitsFlowNode(sourceX, sourceY, sourceX, safeLaneY, node)
      || segmentHitsFlowNode(sourceX, safeLaneY, targetX, safeLaneY, node)
      || segmentHitsFlowNode(targetX, safeLaneY, targetX, targetY, node);
  });
}

function sideForwardLaneX(
  source: SkillFlowCanvasNode,
  target: SkillFlowCanvasNode,
  nodes: SkillFlowCanvasNode[],
  canvasWidth: number,
  siblingIndex: number,
  incomingIndex: number,
) {
  const sourceY = source.y + source.height + 8;
  const targetY = target.y - 8;
  const verticalTop = Math.min(sourceY, targetY);
  const verticalBottom = Math.max(sourceY, targetY);
  const relevantNodes = nodes.filter((node) => (
    node.nodeId !== source.nodeId
    && node.nodeId !== target.nodeId
    && node.y < verticalBottom + 80
    && node.y + node.height > verticalTop - 80
  ));
  const laneOffset = 76 + siblingIndex * 28 + incomingIndex * 18;
  const rightBoundary = Math.max(
    source.x + source.width,
    target.x + target.width,
    ...relevantNodes.map((node) => node.x + node.width),
  );
  const leftBoundary = Math.min(
    source.x,
    target.x,
    ...relevantNodes.map((node) => node.x),
  );
  const rightX = Math.min(canvasWidth - 74, rightBoundary + laneOffset);
  const leftX = Math.max(74, leftBoundary - laneOffset);
  const preferRight = target.x >= source.x;
  const candidates = preferRight ? [rightX, leftX] : [leftX, rightX];
  const clear = candidates.find((candidateX) => !relevantNodes.some((node) => (
    segmentHitsFlowNode(candidateX, sourceY + 34, candidateX, targetY - 34, node, 12)
  )));
  return clear ?? candidates[0];
}

function sideForwardFlowPath(
  source: SkillFlowCanvasNode,
  target: SkillFlowCanvasNode,
  nodes: SkillFlowCanvasNode[],
  canvasWidth: number,
  siblingIndex: number,
  incomingIndex: number,
): string {
  const sourceX = source.x + source.width / 2;
  const sourceY = source.y + source.height + 8;
  const targetX = target.x + target.width / 2;
  const targetY = target.y - 8;
  const sideX = sideForwardLaneX(source, target, nodes, canvasWidth, siblingIndex, incomingIndex);
  const exitY = sourceY + 54 + (siblingIndex % 2) * 18;
  const entryY = targetY - 54 - (incomingIndex % 2) * 18;
  return [
    `M ${sourceX} ${sourceY}`,
    `C ${sourceX} ${exitY - 24}, ${sideX} ${exitY - 24}, ${sideX} ${exitY}`,
    `C ${sideX} ${(exitY + entryY) / 2}, ${sideX} ${(exitY + entryY) / 2}, ${sideX} ${entryY}`,
    `C ${sideX} ${entryY + 24}, ${targetX} ${entryY + 24}, ${targetX} ${targetY}`,
  ].join(' ');
}

function sideForwardEdgeLabelPosition(
  source: SkillFlowCanvasNode,
  target: SkillFlowCanvasNode,
  nodes: SkillFlowCanvasNode[],
  canvasWidth: number,
  siblingIndex: number,
  incomingIndex: number,
) {
  const sourceY = source.y + source.height + 8;
  const targetY = target.y - 8;
  const sideX = sideForwardLaneX(source, target, nodes, canvasWidth, siblingIndex, incomingIndex);
  return {
    x: sideX,
    y: (sourceY + targetY) / 2,
  };
}

function forwardEdgeLabelPosition(
  sourceX: number,
  targetX: number,
  laneY: number,
  siblingIndex: number,
  siblingCount: number,
  incomingIndex: number,
  incomingCount: number,
) {
  const siblingOffset = siblingCount > 1 ? (siblingIndex - (siblingCount - 1) / 2) * 18 : 0;
  const incomingOffset = incomingCount > 1 ? (incomingIndex - (incomingCount - 1) / 2) * 28 : 0;
  const minX = Math.min(sourceX, targetX);
  const maxX = Math.max(sourceX, targetX);
  const midpoint = (sourceX + targetX) / 2;
  const hasHorizontalRoom = maxX - minX > 220;
  const x = hasHorizontalRoom
    ? Math.max(minX + 98, Math.min(maxX - 98, midpoint + siblingOffset + incomingOffset))
    : targetX + siblingOffset + incomingOffset;
  return { x, y: laneY };
}

function returnEdgeLabelPosition(
  source: SkillFlowCanvasNode,
  target: SkillFlowCanvasNode,
  canvasWidth: number,
  siblingIndex: number,
) {
  const sideX = Math.min(canvasWidth - 96, Math.max(source.x + source.width + 96 + siblingIndex * 30, target.x + target.width + 96));
  return {
    x: sideX,
    y: Math.max(72, target.y - 84 - siblingIndex * 18),
  };
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function flowLabelOverlapsNode(
  point: { x: number; y: number },
  node: Pick<SkillFlowCanvasNode, 'x' | 'y' | 'width' | 'height'>,
) {
  const labelWidth = 210;
  const labelHeight = 34;
  const margin = 22;
  const left = point.x - labelWidth / 2;
  const right = point.x + labelWidth / 2;
  const top = point.y - labelHeight / 2;
  const bottom = point.y + labelHeight / 2;
  return !(
    right < node.x - margin
    || left > node.x + node.width + margin
    || bottom < node.y - margin
    || top > node.y + node.height + margin
  );
}

function avoidFlowLabelOverlap(
  anchor: { x: number; y: number },
  nodes: SkillFlowCanvasNode[],
  canvasWidth: number,
  canvasHeight: number,
) {
  const clampPoint = (point: { x: number; y: number }) => ({
    x: clampNumber(point.x, 112, canvasWidth - 112),
    y: clampNumber(point.y, 34, canvasHeight - 34),
  });
  const fits = (point: { x: number; y: number }) => !nodes.some((node) => flowLabelOverlapsNode(point, node));
  const base = clampPoint(anchor);
  if (fits(base)) return base;
  const candidates: Array<{ x: number; y: number }> = [];
  [48, 84, 122, 168, 216].forEach((offset) => {
    candidates.push(
      { x: anchor.x, y: anchor.y - offset },
      { x: anchor.x, y: anchor.y + offset },
      { x: anchor.x - offset * 1.4, y: anchor.y },
      { x: anchor.x + offset * 1.4, y: anchor.y },
      { x: anchor.x - offset, y: anchor.y - offset },
      { x: anchor.x + offset, y: anchor.y - offset },
      { x: anchor.x - offset, y: anchor.y + offset },
      { x: anchor.x + offset, y: anchor.y + offset },
    );
  });
  const found = candidates.map(clampPoint).find(fits);
  return found || clampPoint({ x: anchor.x, y: anchor.y - 76 });
}

function sideReturnFlowPath(
  source: SkillFlowCanvasNode,
  target: SkillFlowCanvasNode,
  canvasWidth: number,
  siblingIndex: number,
): string {
  const sourceX = source.x + source.width / 2;
  const sourceY = source.y + source.height + 8;
  const targetX = target.x + target.width / 2;
  const targetY = target.y - 8;
  const sideX = Math.min(canvasWidth - 54, Math.max(source.x + source.width + 70 + siblingIndex * 28, target.x + target.width + 70));
  const bottomY = sourceY + 64;
  const topY = Math.max(44, targetY - 64);
  return [
    `M ${sourceX} ${sourceY}`,
    `C ${sourceX} ${bottomY}, ${sideX} ${bottomY}, ${sideX} ${bottomY}`,
    `C ${sideX} ${bottomY}, ${sideX} ${topY}, ${sideX} ${topY}`,
    `C ${sideX} ${topY}, ${targetX} ${topY}, ${targetX} ${targetY}`,
  ].join(' ');
}

function incomingEdgeLabel(edge: Record<string, unknown>, nodeNameMap: Record<string, string> = {}): string {
  const source = String(edge.source_node_id || '');
  const sourceName = source && nodeNameMap[source] ? nodeNameMap[source] : source;
  const label = String(edge.label || '');
  const condition = String(edge.condition || '');
  if (label && condition) return `${label}（${condition}）`;
  if (label) return label;
  if (condition) return condition;
  return sourceName ? `来自 ${sourceName}` : '流转';
}

function edgeDisplayLabel(edge: Record<string, unknown>, nodeNameMap: Record<string, string> = {}): string {
  const label = String(edge.label || '').trim();
  if (label) return label;
  const condition = String(edge.condition || '').trim();
  if (condition) return condition;
  const source = String(edge.source_node_id || '');
  const sourceName = source && nodeNameMap[source] ? nodeNameMap[source] : source;
  return sourceName ? `来自 ${sourceName}` : '流转';
}

function compactEdgeLabel(value: string): string {
  const text = value.replace(/\s+/g, ' ').trim();
  if (text.length <= 24) return text;
  return `${text.slice(0, 21)}...`;
}

function nodeTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    collect_info: '收集信息',
    decision: '条件判断',
    tool_call: '调用工具',
    knowledge_query: '检索知识',
    response: '回复用户',
    handoff: '转人工',
    subflow: '子流程',
  };
  return labels[type] || type || '节点';
}

function knowledgeScopeLabels(value: unknown): string[] {
  if (!isRecord(value) || Object.keys(value).length === 0) return [];
  return Object.entries(value).map(([key, item]) => `${key}: ${String(item)}`);
}

function compactInputStyle(value: string, minCh = 8, maxCh = 92): CSSProperties {
  const longestLine = String(value || '').split('\n').reduce((max, line) => Math.max(max, line.length), 0);
  const width = Math.max(minCh, Math.min(maxCh, longestLine + 2));
  return { width: `min(${width}ch, 100%)` };
}

function EditableSourceHeading({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <EditableSourceField>
      <Input
        className="skill-source-title-input"
        style={compactInputStyle(value, 10, 56)}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </EditableSourceField>
  );
}

function EditableSourceStepHeading({
  index,
  value,
  fallback,
  onChange,
}: {
  index: number;
  value: string;
  fallback: string;
  onChange: (value: string) => void;
}) {
  return (
    <EditableSourceField>
      <div className="skill-source-step-title-edit">
        <span>Node {index + 1}:</span>
        <Input
          value={value || fallback}
          style={compactInputStyle(value || fallback, 10, 56)}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
    </EditableSourceField>
  );
}

function EditableSourceTextLine({
  label,
  value,
  multiline = false,
  onChange,
}: {
  label: string;
  value: string;
  multiline?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="skill-source-line">
      <span className="skill-source-key">{label}</span>
      <span className="skill-source-value">
        <EditableSourceField>
          {multiline ? (
            <Input.TextArea
              className="skill-source-edit-input"
              value={value}
              style={compactInputStyle(value, 14, 110)}
              autoSize={{ minRows: 2, maxRows: 8 }}
              onChange={(event) => onChange(event.target.value)}
            />
          ) : (
            <Input
              className="skill-source-edit-input"
              value={value}
              style={compactInputStyle(value)}
              onChange={(event) => onChange(event.target.value)}
            />
          )}
        </EditableSourceField>
      </span>
    </div>
  );
}

function EditableSourceListLine({
  label,
  values,
  onChange,
}: {
  label: string;
  values: string[];
  onChange: (value: string) => void;
}) {
  return (
    <div className="skill-source-line">
      <span className="skill-source-key">{label}</span>
      <span className="skill-source-value">
        <EditableSourceField>
          <Input.TextArea
            className="skill-source-edit-input"
            value={values.join('\n')}
            style={compactInputStyle(values.join('\n'), 10, 86)}
            autoSize={{ minRows: 1, maxRows: 8 }}
            onChange={(event) => onChange(event.target.value)}
          />
        </EditableSourceField>
      </span>
    </div>
  );
}

function EditableSourceActionLine({
  values,
  toolDescriptions,
  toolStatuses,
  onChange,
}: {
  values: string[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  onChange: (value: string) => void;
}) {
  return (
    <div className="skill-source-line">
      <span className="skill-source-key">{fieldLabel('allowed_actions')}</span>
      <span className="skill-source-value">
        <EditableSourceField>
          <EditableActionList
            actions={values}
            toolDescriptions={toolDescriptions}
            toolStatuses={toolStatuses}
            onChange={onChange}
          />
        </EditableSourceField>
      </span>
    </div>
  );
}

function EditableActionList({
  actions,
  toolDescriptions,
  toolStatuses,
  onChange,
}: {
  actions: string[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  onChange: (value: string) => void;
}) {
  const [editingAction, setEditingAction] = useState<{ index: number; value: string } | null>(null);

  function beginEdit(index: number, value: string) {
    setEditingAction({ index, value });
  }

  function commitEdit() {
    if (!editingAction) return;
    const next = [...actions];
    const nextValue = editingAction.value.trim();
    if (editingAction.index >= next.length) {
      if (nextValue) next.push(nextValue);
    } else if (nextValue) {
      next[editingAction.index] = nextValue;
    } else {
      next.splice(editingAction.index, 1);
    }
    onChange(next.join('\n'));
    setEditingAction(null);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitEdit();
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      setEditingAction(null);
    }
  }

  if (actions.length === 0 && !editingAction) {
    return (
      <button type="button" className="skill-source-action-add" onClick={() => beginEdit(0, '')}>
        点击新增动作
      </button>
    );
  }

  return (
    <div className="skill-source-action-editor">
      <div className="skill-action-list editable">
        {actions.map((action, index) =>
          editingAction?.index === index ? (
            <Input
              key={`editing_${index}`}
              className="skill-source-action-input"
              style={compactInputStyle(editingAction.value, 8, 42)}
              value={editingAction.value}
              autoFocus
              onBlur={commitEdit}
              onChange={(event) => setEditingAction({ index, value: event.target.value })}
              onKeyDown={handleKeyDown}
            />
          ) : (
            <button
              type="button"
              className="skill-source-action-edit-button"
              key={`${action}_${index}`}
              onClick={() => beginEdit(index, action)}
            >
              <ActionChip action={action} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
            </button>
          ),
        )}
        {editingAction && editingAction.index >= actions.length && (
          <Input
            className="skill-source-action-input"
            style={compactInputStyle(editingAction.value, 8, 42)}
            value={editingAction.value}
            autoFocus
            onBlur={commitEdit}
            onChange={(event) => setEditingAction({ index: editingAction.index, value: event.target.value })}
            onKeyDown={handleKeyDown}
          />
        )}
        <button type="button" className="skill-source-action-add" onClick={() => beginEdit(actions.length, '')}>
          +
        </button>
      </div>
      <span className="skill-source-edit-hint">点击单个动作修改</span>
    </div>
  );
}

function EditableSourceField({ children }: { children: ReactNode }) {
  function stop(event: MouseEvent<HTMLDivElement> | KeyboardEvent<HTMLDivElement>) {
    event.stopPropagation();
  }

  return (
    <div className="skill-source-edit-field" onClick={stop} onKeyDown={stop}>
      {children}
    </div>
  );
}

function SelectableTarget({
  className,
  target,
  onToggle,
  children,
}: {
  className: string;
  target: TargetSelection;
  onToggle: (target: TargetSelection) => void;
  children: ReactNode;
}) {
  function handleClick(event: MouseEvent<HTMLDivElement>) {
    if (hasSelectedText()) {
      event.preventDefault();
      return;
    }
    onToggle(target);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onToggle(target);
  }

  return (
    <div role="button" tabIndex={0} className={className} onClick={handleClick} onKeyDown={handleKeyDown}>
      {children}
    </div>
  );
}

function ActionDiffList({
  diff,
  currentActions,
  toolDescriptions,
  toolStatuses,
}: {
  diff: TextDiffAnimation;
  currentActions: string[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
}) {
  const oldActions = actionsFromDiffText(diffFullOldValue(diff));
  const newActions = actionsFromDiffText(diffFullNewValue(diff));
  const visibleActions = currentActions.length > 0 ? currentActions : newActions;
  const inserted = new Set(newActions.filter((action) => !oldActions.includes(action)));
  const removed = oldActions.filter((action) => !newActions.includes(action));
  const phaseClass = diff.phase === 'mark' ? 'marked' : diff.phase === 'type' ? 'typing' : 'settled';
  if (visibleActions.length === 0 && removed.length === 0) return <span className="skill-action-empty">-</span>;
  return (
    <div className="skill-action-list">
      {removed.map((action, index) => (
        <ActionChip
          action={action}
          toolDescriptions={toolDescriptions}
          toolStatuses={toolStatuses}
          className="removed"
          key={`removed_${action}_${index}`}
        />
      ))}
      {visibleActions.map((action, index) => (
        <ActionChip
          action={action}
          toolDescriptions={toolDescriptions}
          toolStatuses={toolStatuses}
          className={inserted.has(action) ? `added ${phaseClass}` : ''}
          key={`${action}_${index}`}
        />
      ))}
    </div>
  );
}

function ActionList({
  actions,
  toolDescriptions,
  toolStatuses,
}: {
  actions: string[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
}) {
  if (actions.length === 0) return <span className="skill-action-empty">-</span>;
  return (
    <div className="skill-action-list">
      {actions.map((action, index) => (
        <ActionChip
          action={action}
          toolDescriptions={toolDescriptions}
          toolStatuses={toolStatuses}
          key={`${action}_${index}`}
        />
      ))}
    </div>
  );
}

function ActionChip({
  action,
  toolDescriptions,
  toolStatuses,
  className = '',
}: {
  action: string;
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  className?: string;
}) {
  const toolName = toolNameFromAction(action);
  const description = toolName ? toolDescriptions[toolName] || '当前工具配置中暂无描述' : '';
  const status = toolName ? toolStatuses[toolName] || 'incomplete' : '';
  const chip = (
    <span
      className={`skill-action-chip ${toolName ? `tool ${status}` : ''} ${className}`.trim()}
      title={description || undefined}
    >
      {actionLabel(action)}
    </span>
  );
  return description ? <Tooltip title={description}>{chip}</Tooltip> : chip;
}

function SaveReviewDiffValue({
  diff,
  toolDescriptions,
  toolStatuses,
}: {
  diff: TextDiffAnimation;
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
}) {
  if (diff.field === 'allowed_actions') {
    const removedActions = actionsFromDiffText(diffFullOldValue(diff));
    const insertedActions = actionsFromDiffText(diffFullNewValue(diff));
    return (
      <>
        {diff.removed && (
          <div className="save-review-action-diff old">
            <span className="save-review-diff-sign">-</span>
            <ActionList actions={removedActions} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
          </div>
        )}
        {diff.inserted && (
          <div className="save-review-action-diff new">
            <span className="save-review-diff-sign">+</span>
            <ActionList actions={insertedActions} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
          </div>
        )}
      </>
    );
  }
  return (
    <>
      {diff.removed && <div><span className="diff-old">- {diff.removed}</span></div>}
      {diff.inserted && <div><span className="diff-new">+ {diff.inserted}</span></div>}
    </>
  );
}

function InlineDiffText({
  path,
  field,
  value,
  diffs,
}: {
  path: string;
  field: string;
  value: string;
  diffs: TextDiffAnimation[];
}): ReactNode {
  const diff = diffs.find((item) => item.path === path && item.field === field);
  if (!diff) return value;
  if (diff.phase === 'mark') {
    return (
      <>
        {diff.prefix}
        {diff.removed ? <span className="skill-inline-remove">{diff.removed}</span> : null}
        {diff.suffix}
      </>
    );
  }
  const typedInsert = diff.inserted.slice(0, Math.ceil(diff.inserted.length * diff.progress));
  return (
    <>
      {diff.prefix}
      {typedInsert ? <span className={`skill-inline-add ${diff.phase}`}>{typedInsert}</span> : null}
      {diff.suffix}
    </>
  );
}

function diffFullOldValue(diff: TextDiffAnimation): string {
  return `${diff.prefix}${diff.removed}${diff.suffix}`;
}

function diffFullNewValue(diff: TextDiffAnimation): string {
  return `${diff.prefix}${diff.inserted}${diff.suffix}`;
}

function actionsFromDiffText(value: string): string[] {
  const normalized = value.replace(/`/g, '').trim();
  if (!normalized || normalized === '-') return [];
  return normalized
    .split(/[、,\n]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseInitialSkillPrompt(text: string): { title: string; raw_content: string } {
  const titleMatch = text.match(/标题[:：]\s*([^\n，,]+)/);
  const rawMatch = text.match(/原始(?:SOP|技能)?文本[:：]?\s*([\s\S]+)/);
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
  const title = titleMatch?.[1]?.trim() || lines[0]?.slice(0, 32) || '新技能';
  const rawContent = rawMatch?.[1]?.trim() || lines.slice(titleMatch ? 0 : 1).join('\n') || text;
  return { title, raw_content: rawContent };
}

function createStreamingDraftSeed(payload: { title: string; raw_content: string }): SkillCard {
  return {
    skill_id: `skill_${slugSegment(payload.title) || 'preview'}`,
    name: payload.title || '新技能',
    version: '1.0.0',
    business_domain: '',
    description: payload.raw_content.slice(0, 120),
    trigger_intents: [],
    user_utterance_examples: [],
    goal: [],
    required_info: [],
    response_rules: [],
    nodes: [],
    edges: [],
    start_node_id: '',
    terminal_node_ids: [],
    interruption_policy: {},
  };
}

function previewSkillFromStream(
  streamText: string,
  previous: SkillCard,
  payload: { title: string; raw_content: string },
): SkillCard {
  const parsed = parseCompleteStreamSkill(streamText);
  if (parsed) return parsed;
  const source = extractDraftSkillSource(streamText);
  const next = cloneSkill(previous || createStreamingDraftSeed(payload));
  applyStringPreview(next, source, 'skill_id');
  applyStringPreview(next, source, 'name');
  applyStringPreview(next, source, 'version');
  applyStringPreview(next, source, 'business_domain');
  applyStringPreview(next, source, 'description');
  applyArrayPreview(next, source, 'trigger_intents');
  applyArrayPreview(next, source, 'user_utterance_examples');
  applyArrayPreview(next, source, 'goal');
  applyArrayPreview(next, source, 'required_info');
  applyArrayPreview(next, source, 'response_rules');
  const nodes = extractNodePreview(source);
  if (nodes.length > 0) {
    next.nodes = nodes;
    next.start_node_id = String(nodes[0]?.node_id || '');
    next.terminal_node_ids = nodes.length > 0 ? [String(nodes[nodes.length - 1]?.node_id || '')].filter(Boolean) : [];
    next.edges = nodes.slice(0, -1).map((node, index) => ({
      source_node_id: String(node.node_id || ''),
      next_node_id: String(nodes[index + 1]?.node_id || ''),
      condition: '',
      priority: index,
      label: '',
    })).filter((edge) => edge.source_node_id && edge.next_node_id);
  }
  return next;
}

function parseCompleteStreamSkill(streamText: string): SkillCard | null {
  try {
    const parsed = JSON.parse(extractJsonCandidate(streamText)) as Record<string, unknown>;
    const draft = isRecord(parsed.draft_skill) ? parsed.draft_skill : parsed;
    if (!isRecord(draft)) return null;
    return {
      skill_id: stringValue(draft.skill_id, 'skill_preview'),
      name: stringValue(draft.name, '新技能'),
      version: stringValue(draft.version, '1.0.0'),
      business_domain: stringValue(draft.business_domain, ''),
      description: stringValue(draft.description, ''),
      trigger_intents: asStringList(draft.trigger_intents),
      user_utterance_examples: asStringList(draft.user_utterance_examples),
      goal: asStringList(draft.goal),
      required_info: asStringList(draft.required_info),
      response_rules: asStringList(draft.response_rules),
      nodes: Array.isArray(draft.nodes) ? draft.nodes.filter(isRecord).map(normalizeNodePreview) : [],
      edges: Array.isArray(draft.edges) ? draft.edges.filter(isRecord).map(normalizeEdgePreview) : [],
      start_node_id: stringValue(draft.start_node_id, ''),
      terminal_node_ids: asStringList(draft.terminal_node_ids),
      interruption_policy: isRecord(draft.interruption_policy) ? stringRecord(draft.interruption_policy) : {},
    };
  } catch {
    return null;
  }
}

function extractDraftSkillSource(streamText: string): string {
  const fieldIndex = streamText.indexOf('"draft_skill"');
  if (fieldIndex < 0) return streamText;
  const objectStart = streamText.indexOf('{', fieldIndex);
  if (objectStart < 0) return streamText.slice(fieldIndex);
  return streamText.slice(objectStart);
}

function extractJsonCandidate(streamText: string): string {
  const stripped = streamText.trim();
  const start = stripped.indexOf('{');
  const end = stripped.lastIndexOf('}');
  return start >= 0 && end >= start ? stripped.slice(start, end + 1) : stripped;
}

function applyStringPreview(skill: SkillCard, source: string, field: keyof SkillCard): void {
  const value = extractJsonStringField(source, String(field));
  if (value !== null) {
    (skill as unknown as Record<string, unknown>)[field] = value;
  }
}

function applyArrayPreview(skill: SkillCard, source: string, field: keyof SkillCard): void {
  const value = extractJsonStringArrayField(source, String(field));
  if (value !== null) {
    (skill as unknown as Record<string, unknown>)[field] = value;
  }
}

function extractNodePreview(source: string): Array<Record<string, unknown>> {
  const fragments = extractObjectFragmentsFromArrayField(source, 'nodes');
  return fragments
    .map((fragment, index) => parseNodeFragment(fragment, index))
    .filter((node): node is Record<string, unknown> => Boolean(node));
}

function parseNodeFragment(fragment: string, index: number): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(fragment) as unknown;
    if (isRecord(parsed)) return normalizeNodePreview(parsed, index);
  } catch {
    // Partial object: fall through to field extraction.
  }
  const nodeId = extractJsonStringField(fragment, 'node_id') || '';
  const type = extractJsonStringField(fragment, 'type') || 'collect_info';
  const name = extractJsonStringField(fragment, 'name') || '';
  const instruction = extractJsonStringField(fragment, 'instruction') || '';
  const condition = extractJsonStringField(fragment, 'condition') || '';
  const expectedUserInfo = extractJsonStringArrayField(fragment, 'expected_user_info') || [];
  const allowedActions = extractJsonStringArrayField(fragment, 'allowed_actions') || [];
  if (!nodeId && !name && !instruction && expectedUserInfo.length === 0 && allowedActions.length === 0) {
    return null;
  }
  return {
    node_id: nodeId || `node_${index + 1}`,
    type,
    name: name || nodeId || `节点 ${index + 1}`,
    instruction,
    optional: false,
    condition,
    expected_user_info: expectedUserInfo,
    allowed_actions: allowedActions,
    knowledge_scope: {},
    retry_policy: {},
    metadata: {},
  };
}

function normalizeNodePreview(node: Record<string, unknown>, index = 0): Record<string, unknown> {
  const nodeId = stringValue(node.node_id, `node_${index + 1}`);
  return {
    node_id: nodeId,
    type: stringValue(node.type, 'collect_info'),
    name: stringValue(node.name, nodeId),
    instruction: stringValue(node.instruction, ''),
    optional: Boolean(node.optional),
    condition: stringValue(node.condition, ''),
    expected_user_info: asStringList(node.expected_user_info),
    allowed_actions: asStringList(node.allowed_actions),
    knowledge_scope: isRecord(node.knowledge_scope) ? node.knowledge_scope : {},
    retry_policy: isRecord(node.retry_policy) ? node.retry_policy : {},
    metadata: isRecord(node.metadata) ? node.metadata : {},
  };
}

function normalizeEdgePreview(edge: Record<string, unknown>, index = 0): Record<string, unknown> {
  return {
    source_node_id: stringValue(edge.source_node_id, ''),
    next_node_id: stringValue(edge.next_node_id, ''),
    condition: stringValue(edge.condition, ''),
    priority: Number(edge.priority || index),
    label: stringValue(edge.label, ''),
  };
}

function extractJsonStringField(source: string, field: string): string | null {
  const match = new RegExp(`"${escapeRegExp(field)}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"`).exec(source);
  if (!match) return null;
  return decodeJsonString(match[1]);
}

function extractJsonStringArrayField(source: string, field: string): string[] | null {
  const start = findFieldValueStart(source, field);
  if (start === null) return null;
  const arrayStart = skipWhitespace(source, start);
  if (source[arrayStart] !== '[') return null;
  const arrayEnd = findBalancedEnd(source, arrayStart, '[', ']');
  const arrayText = arrayEnd === null ? source.slice(arrayStart + 1) : source.slice(arrayStart, arrayEnd + 1);
  if (arrayEnd !== null) {
    try {
      const parsed = JSON.parse(arrayText) as unknown;
      return asStringList(parsed);
    } catch {
      return extractQuotedJsonStrings(arrayText);
    }
  }
  return extractQuotedJsonStrings(arrayText);
}

function extractObjectFragmentsFromArrayField(source: string, field: string): string[] {
  const start = findFieldValueStart(source, field);
  if (start === null) return [];
  const arrayStart = skipWhitespace(source, start);
  if (source[arrayStart] !== '[') return [];
  const fragments: string[] = [];
  let objectStart = -1;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = arrayStart + 1; index < source.length; index += 1) {
    const char = source[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === '\\') {
        escaped = true;
      } else if (char === '"') {
        inString = false;
      }
      continue;
    }
    if (char === '"') {
      inString = true;
      continue;
    }
    if (char === '{') {
      if (depth === 0) objectStart = index;
      depth += 1;
      continue;
    }
    if (char === '}') {
      depth -= 1;
      if (depth === 0 && objectStart >= 0) {
        fragments.push(source.slice(objectStart, index + 1));
        objectStart = -1;
      }
      continue;
    }
    if (char === ']' && depth === 0) break;
  }
  if (depth > 0 && objectStart >= 0) {
    fragments.push(source.slice(objectStart));
  }
  return fragments;
}

function findFieldValueStart(source: string, field: string): number | null {
  const match = new RegExp(`"${escapeRegExp(field)}"\\s*:`).exec(source);
  return match ? match.index + match[0].length : null;
}

function skipWhitespace(source: string, start: number): number {
  let index = start;
  while (index < source.length && /\s/.test(source[index])) index += 1;
  return index;
}

function findBalancedEnd(source: string, start: number, openChar: string, closeChar: string): number | null {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === '\\') {
        escaped = true;
      } else if (char === '"') {
        inString = false;
      }
      continue;
    }
    if (char === '"') {
      inString = true;
      continue;
    }
    if (char === openChar) depth += 1;
    if (char === closeChar) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return null;
}

function extractQuotedJsonStrings(source: string): string[] {
  const values: string[] = [];
  const pattern = /"((?:\\.|[^"\\])*)"/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source))) {
    const value = decodeJsonString(match[1]);
    if (value) values.push(value);
  }
  return values;
}

function decodeJsonString(value: string): string {
  try {
    return JSON.parse(`"${value}"`) as string;
  } catch {
    return value;
  }
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

function stringRecord(value: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, String(item)]));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function parseJsonObject(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function slugSegment(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 32);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function joinList(values: unknown): string {
  if (Array.isArray(values)) {
    const items = values.map(String).filter(Boolean);
    return items.length > 0 ? items.map((item) => `\`${item}\``).join(', ') : '-';
  }
  if (typeof values === 'string' && values.trim()) return values;
  return '-';
}

function joinPlain(values: unknown): string {
  if (Array.isArray(values)) {
    const items = values.map(String).filter(Boolean);
    return items.length > 0 ? items.join('、') : '-';
  }
  if (typeof values === 'string' && values.trim()) return values;
  return '-';
}

function asStringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === 'string' && value.trim()) return [value];
  return [];
}

function hasSelectedText(): boolean {
  return Boolean(window.getSelection()?.toString().trim());
}

async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  let binary = '';
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return window.btoa(binary);
}

function filenameTitle(filename: string): string {
  return filename.replace(/\.[^.]+$/, '').trim() || '新技能';
}

const uploadContentMarker = '上传文档内容：';

function buildOutgoingText(input: string, attachments: UploadAttachment[]): string {
  const text = input.trim();
  const attachmentText = attachments
    .filter((item) => item.status === 'ready' && item.text?.trim())
    .map((item) => `文件：${item.name}\n${item.text?.trim() || ''}`)
    .join('\n\n');
  return [text, attachmentText ? `上传文档内容：\n${attachmentText}` : ''].filter(Boolean).join('\n\n');
}

function visibleChatContent(item: ChatItem): string {
  if (item.role !== 'user') return item.content;
  return stripUploadContent(item.content || item.outgoingText || '');
}

function skillEditTeacherPraiseReply(
  input: string,
  manualSourceEdited: boolean,
  stage: TeacherPraiseStage,
): string | null {
  if (!manualSourceEdited) return null;
  const normalized = input.replace(/\s+/g, '').replace(/[，。！？!?、,.]/g, '');
  if (normalized.includes('我改的好还是你改的好') || normalized.includes('是我改的好还是你改的好')) {
    return '老师改的太好了';
  }
  const asksForReview = normalized.includes('怎么样') || normalized.includes('如何') || normalized.includes('觉得');
  const mentionsOwnEdit =
    normalized.includes('我把这一块儿改了改') ||
    normalized.includes('我把这一块改了改') ||
    normalized.includes('我改了这一块') ||
    normalized.includes('这一块改了改');
  if (stage === 'idle' && asksForReview && mentionsOwnEdit) {
    return '老师，你改的清楚多了';
  }
  return null;
}

function buildEditedOutgoingText(item: ChatItem, displayText: string): string {
  const source = item.outgoingText || item.content;
  const markerIndex = source.indexOf(uploadContentMarker);
  if (markerIndex < 0) return displayText.trim();
  const uploadContent = source.slice(markerIndex).trim();
  return [displayText.trim(), uploadContent].filter(Boolean).join('\n\n');
}

function stripUploadContent(text: string): string {
  const markerIndex = text.indexOf(uploadContentMarker);
  return (markerIndex >= 0 ? text.slice(0, markerIndex) : text).trim();
}

function buildDisplayAttachments(attachments: UploadAttachment[]): ChatAttachment[] {
  return attachments
    .filter((item) => item.status === 'ready')
    .map((item) => ({
      id: item.id,
      name: item.name,
      type: attachmentTypeLabel(item.name),
    }));
}

function attachmentTypeLabel(filename: string): string {
  const extension = filename.split('.').pop()?.trim().toUpperCase();
  return extension || 'FILE';
}

function splitEditableList(value: string): string[] {
  return value
    .split(/\n|,|，|、/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatMessageTime(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function CopyGlyph() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="8" y="4" width="12" height="12" rx="3" />
      <rect x="4" y="8" width="12" height="12" rx="3" />
    </svg>
  );
}

function PencilGlyph() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M5 19l4.2-1 10-10a2.1 2.1 0 0 0-3-3l-10 10L5 19z" />
      <path d="M14.8 5.2l4 4" />
    </svg>
  );
}

function normalizeToolSuggestions(value: unknown): ToolSuggestionItem[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => isRecord(item))
    .map((item) => ({
      name: String(item.name || '').trim(),
      display_name: typeof item.display_name === 'string' ? item.display_name : undefined,
      description: typeof item.description === 'string' ? item.description : undefined,
      bucket: typeof item.bucket === 'string' && item.bucket.trim() ? item.bucket.trim() : '技能自发现工具',
      tool_type: item.tool_type === 'mcp' ? 'mcp' : 'http',
      method: typeof item.method === 'string' ? item.method : 'POST',
      url: typeof item.url === 'string' ? item.url : '',
      mcp_config: isRecord(item.mcp_config) ? item.mcp_config : {},
      input_schema: isRecord(item.input_schema) ? item.input_schema : {},
      output_schema: isRecord(item.output_schema) ? item.output_schema : {},
      sample_arguments: isRecord(item.sample_arguments) ? item.sample_arguments : {},
      source_excerpt: typeof item.source_excerpt === 'string' ? item.source_excerpt : undefined,
      probe_result: isRecord(item.probe_result) ? item.probe_result as ToolProbeResponse : undefined,
      reason: typeof item.reason === 'string' ? item.reason : '',
      resolution_status: toolSuggestionResolutionValue(item.resolution_status),
      matched_tool_id: typeof item.matched_tool_id === 'string' ? item.matched_tool_id : undefined,
      matched_tool_name: typeof item.matched_tool_name === 'string' ? item.matched_tool_name : undefined,
      matched_tool_display_name: typeof item.matched_tool_display_name === 'string' ? item.matched_tool_display_name : undefined,
      missing_reason: typeof item.missing_reason === 'string' ? item.missing_reason : undefined,
      status: 'pending' as const,
      probeStatus: isRecord(item.probe_result)
        ? Boolean(item.probe_result.success)
          ? 'success' as const
          : 'error' as const
        : 'idle' as const,
    }))
    .filter((item) => item.name);
}

function toolSuggestionResolutionValue(value: unknown): ToolSuggestion['resolution_status'] {
  return value === 'existing' || value === 'incomplete' || value === 'new_candidate' ? value : 'new_candidate';
}

function toolSuggestionResolution(suggestion: ToolSuggestionItem): NonNullable<ToolSuggestion['resolution_status']> {
  return suggestion.resolution_status || 'new_candidate';
}

function toolSuggestionTitle(suggestion: ToolSuggestionItem): string {
  const label = suggestion.display_name || suggestion.name;
  if (toolSuggestionResolution(suggestion) === 'existing') {
    return `已匹配工具：${suggestion.matched_tool_display_name || label}`;
  }
  return `建议新增工具：${label}`;
}

function toolSuggestionResolutionLabel(suggestion: ToolSuggestionItem): string {
  const status = toolSuggestionResolution(suggestion);
  if (status === 'existing') return '已匹配现有工具';
  if (status === 'incomplete') return '工具信息不足';
  return '可新增候选';
}

function toolSuggestionStatusText(suggestion: ToolSuggestionItem): string {
  if (suggestion.status === 'accepted') return '已确认';
  if (suggestion.status === 'created') return '已新增';
  if (suggestion.status === 'rejected') return '已拒绝';
  if (suggestion.probeStatus === 'probing') return '测试中';
  if (suggestion.probe_result?.success) return '测试通过';
  if (suggestion.probe_result && !suggestion.probe_result.success) return '测试失败';
  if (toolSuggestionResolution(suggestion) === 'existing') return '已存在';
  if (toolSuggestionResolution(suggestion) === 'incomplete') return '信息不足';
  return '待新增';
}

function toolSuggestionStatusClass(suggestion: ToolSuggestionItem): string {
  if (suggestion.status === 'accepted' || suggestion.status === 'created' || suggestion.probe_result?.success || toolSuggestionResolution(suggestion) === 'existing') {
    return 'success';
  }
  if (suggestion.status === 'rejected' || (suggestion.probe_result && !suggestion.probe_result.success)) {
    return 'error';
  }
  if (suggestion.probeStatus === 'probing') return 'running';
  if (toolSuggestionResolution(suggestion) === 'incomplete') return 'muted';
  return 'pending';
}

function toolSuggestionSelectionsComplete(suggestions: ToolSuggestionItem[]): boolean {
  const candidates = suggestions.filter((suggestion) => toolSuggestionResolution(suggestion) === 'new_candidate');
  return candidates.length > 0 && candidates.every((suggestion) =>
    suggestion.status === 'accepted' || suggestion.status === 'created' || suggestion.status === 'rejected',
  );
}

function compactWarning(warning: string): string {
  const text = warning.trim();
  const toolName = warningToolName(text);
  if (
    toolName &&
    (text.includes('未配置工具') ||
      text.includes('available_tools') ||
      text.includes('tool_suggestions') ||
      text.includes('allowed_actions'))
  ) {
    return `未配置工具 ${toolName}，需在原文中提供完整接口信息后新增。`;
  }
  if (text.includes('没有任何工具支持') || (text.includes('available_tools') && text.includes('工具'))) {
    return '缺少可用工具，需先新增工具后再执行该流程。';
  }
  const replacements: Array<[string, string]> = [
    ['原始改写未包含工具节点，已按可用工具补充闭环执行节点。', '已补充工具执行节点。'],
    ['原始改写缺少执行前确认节点，已补充确认节点。', '已补充执行前确认节点。'],
    ['原始改写缺少最终回复节点，已补充闭环反馈节点。', '已补充最终回复节点。'],
    ['模型未生成节点，已使用规则生成默认节点。', '已生成默认节点。'],
  ];
  const matched = replacements.find(([source]) => source === text);
  if (matched) return matched[1];
  return text;
}

function compactWarningItems(
  warnings: string[],
  toolSuggestions: ToolSuggestionItem[] | undefined,
): Array<{ text: string; title: string }> {
  const suggestionNames = new Set(
    (toolSuggestions || [])
      .flatMap((item) => [item.name, item.display_name, item.matched_tool_name, item.matched_tool_display_name])
      .filter((value): value is string => Boolean(value))
      .map((value) => value.toLowerCase()),
  );
  const items: Array<{ text: string; title: string }> = [];
  for (const warning of warnings) {
    const toolName = warningToolName(warning);
    if (toolName && !suggestionNames.has(toolName.toLowerCase())) {
      continue;
    }
    const text = compactWarning(warning);
    const existing = items.find((item) => item.text === text);
    if (existing) {
      existing.title = `${existing.title}\n${warning}`;
      continue;
    }
    items.push({ text, title: warning });
  }
  return items;
}

function warningToolName(text: string): string {
  const patterns = [
    /未配置工具\s+`?([A-Za-z0-9_.:-]+)`?/,
    /工具\s+`?([A-Za-z0-9_.:-]+)`?\s+不在/,
    /引用了未配置工具\s+`?([A-Za-z0-9_.:-]+)`?/,
    /提到了工具\s+`?([A-Za-z0-9_.:-]+)`?/,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match?.[1]) return match[1].replace(/[`，。,.]+$/g, '');
  }
  return '';
}

function readDistillCache(key: string): DistillCacheSnapshot | null {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<DistillCacheSnapshot>;
    return {
      draft: parsed.draft || null,
      loadedSkill: parsed.loadedSkill || null,
      lastSavedDraft: parsed.lastSavedDraft || null,
      messages: Array.isArray(parsed.messages) ? parsed.messages : DEFAULT_DISTILL_MESSAGES,
      input: typeof parsed.input === 'string' ? parsed.input : '',
      selectedPaths: normalizeInitialSelectedPaths(
        Array.isArray(parsed.selectedPaths) ? parsed.selectedPaths.map(String) : DEFAULT_TARGET_PATHS,
      ),
      highlightedPaths: Array.isArray(parsed.highlightedPaths) ? parsed.highlightedPaths.map(String) : [],
      updatingPaths: Array.isArray(parsed.updatingPaths) ? parsed.updatingPaths.map(String) : [],
      dirtyPaths: Array.isArray(parsed.dirtyPaths) ? parsed.dirtyPaths.map(String) : [],
      textDiffs: Array.isArray(parsed.textDiffs) ? parsed.textDiffs : [],
      pendingChange: parsed.pendingChange || null,
      viewMode: parsed.viewMode === 'flow' ? 'flow' : 'source',
      attachments: Array.isArray(parsed.attachments)
        ? parsed.attachments.filter((item): item is UploadAttachment => isRecord(item)).map((item) => ({
            id: String(item.id || `file_${Date.now()}_${Math.random().toString(16).slice(2)}`),
            name: String(item.name || '未命名文件'),
            status: item.status === 'error' ? 'error' : 'ready',
            text: typeof item.text === 'string' ? item.text : undefined,
            error: typeof item.error === 'string' ? item.error : undefined,
          }))
        : [],
      streamStatus: typeof parsed.streamStatus === 'string' ? parsed.streamStatus : '',
      activeJob: isRecord(parsed.activeJob)
        ? {
            jobId: String(parsed.activeJob.jobId || ''),
            kind: parsed.activeJob.kind === 'rewrite' ? 'rewrite' : 'distill',
            assistantId: String(parsed.activeJob.assistantId || ''),
            lastSeq: Number(parsed.activeJob.lastSeq || 0),
            status: typeof parsed.activeJob.status === 'string' ? parsed.activeJob.status : undefined,
            createPayload: isRecord(parsed.activeJob.createPayload)
              ? {
                  title: String(parsed.activeJob.createPayload.title || ''),
                  raw_content: String(parsed.activeJob.createPayload.raw_content || ''),
                }
              : undefined,
            previousDraft: isRecord(parsed.activeJob.previousDraft)
              ? (parsed.activeJob.previousDraft as SkillCard)
              : undefined,
            targets: Array.isArray(parsed.activeJob.targets)
              ? parsed.activeJob.targets.map(String)
              : undefined,
          }
        : null,
      manualSourceEdited: parsed.manualSourceEdited === true,
      teacherPraiseStage: parsed.teacherPraiseStage === 'praised' ? 'praised' : 'idle',
    };
  } catch {
    window.sessionStorage.removeItem(key);
    return null;
  }
}

function writeDistillCache(key: string, snapshot: DistillCacheSnapshot): void {
  try {
    window.sessionStorage.setItem(key, JSON.stringify(snapshot));
  } catch {
    // Cache is best-effort. Large uploaded documents can exceed browser quota.
  }
}

function normalizeInitialSelectedPaths(paths: string[]): string[] {
  if (paths.length === 1 && paths[0] === 'basic') return [];
  return paths;
}

function allTargetPaths(skill: SkillCard): string[] {
  return [
    'basic',
    ...skillGraphSteps(skill).map((_step, index) => stepTargetPath(index)),
  ];
}

function reconcileSelectedPaths(paths: string[], skill: SkillCard): string[] {
  if (paths.length === 0) return [];
  const available = allTargetPaths(skill);
  const next = paths.filter((path) => available.includes(path));
  return next.length > 0 ? next : DEFAULT_TARGET_PATHS;
}

function targetClass(
  baseClass: string,
  path: string,
  selectedPaths: string[],
  highlightedPaths: string[],
  updatingPaths: string[],
  dirtyPaths: string[],
): string {
  return [
    baseClass,
    selectedPaths.includes(path) ? 'active' : '',
    highlightedPaths.includes(path) ? 'changed' : '',
    updatingPaths.includes(path) ? 'updating' : '',
    dirtyPaths.includes(path) ? 'dirty' : '',
  ].filter(Boolean).join(' ');
}

function mergePaths(current: string[], next: string[]): string[] {
  return Array.from(new Set([...current, ...next]));
}

function cloneSkill(skill: SkillCard): SkillCard {
  return JSON.parse(JSON.stringify(skill)) as SkillCard;
}

function comparableSkillContent(skill: SkillCard): SkillCard {
  const next = cloneSkill(skill);
  next.version = '';
  return next;
}

function hasSkillContentChanges(targetDraft: SkillCard | null, baseDraft: SkillCard | null): boolean {
  if (!targetDraft) return false;
  if (!baseDraft) return true;
  return JSON.stringify(comparableSkillContent(targetDraft)) !== JSON.stringify(comparableSkillContent(baseDraft));
}

function removeToolActionFromSkill(skill: SkillCard, toolName: string): SkillCard {
  const next = cloneSkill(skill);
  const targetAction = `call_tool:${toolName}`;
  next.nodes = (Array.isArray(next.nodes) ? next.nodes : []).map((node) => ({
    ...node,
    allowed_actions: asStringList(node.allowed_actions).filter((action) => action !== targetAction),
  }));
  return next;
}

function cloneSkillRead(skill: SkillRead): SkillRead {
  return JSON.parse(JSON.stringify(skill)) as SkillRead;
}

function collectRollbackOperations(messages: ChatItem[]): DistillHistoryOperation[] {
  const operations = messages.flatMap((item) => item.operations || []);
  const relevant = operations.filter((operation) =>
    ['skill_change', 'version_save', 'tool_add'].includes(operation.kind),
  );
  const seen = new Set<string>();
  return relevant.filter((operation) => {
    const key = `${operation.kind}:${operation.skillId || ''}:${operation.version || ''}:${operation.toolId || operation.toolName || ''}:${operation.label}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function blankSkillForAnimation(skill: SkillCard): SkillCard {
  const blank = cloneSkill(skill);
  blank.skill_id = '';
  blank.name = '';
  blank.version = '';
  blank.business_domain = '';
  blank.description = '';
  blank.trigger_intents = [];
  blank.user_utterance_examples = [];
  blank.goal = [];
  blank.required_info = [];
  blank.response_rules = [];
  blank.nodes = skillGraphSteps(skill).map((step) => ({
    node_id: '',
    type: String(step.type || 'collect_info'),
    name: '',
    instruction: '',
    optional: Boolean(step.optional),
    condition: '',
    expected_user_info: [],
    allowed_actions: [],
    knowledge_scope: {},
    retry_policy: {},
    metadata: {},
  }));
  blank.edges = [];
  blank.start_node_id = '';
  blank.terminal_node_ids = [];
  return blank;
}

function diffTargetPaths(previousDraft: SkillCard, nextDraft: SkillCard, targetPaths: string[]): string[] {
  const candidates = Array.from(new Set([...targetPaths, ...allTargetPaths(previousDraft), ...allTargetPaths(nextDraft)]));
  return candidates.filter((path) => sectionSignature(previousDraft, path) !== sectionSignature(nextDraft, path));
}

function sectionSignature(skill: SkillCard, path: string): string {
  if (path === 'basic') {
    return JSON.stringify({
      skill_id: skill.skill_id,
      name: skill.name,
      version: skill.version,
      business_domain: skill.business_domain || '',
      description: skill.description,
      trigger_intents: skill.trigger_intents || [],
      user_utterance_examples: skill.user_utterance_examples || [],
      goal: skill.goal || [],
      required_info: skill.required_info || [],
      interruption_policy: skill.interruption_policy || {},
      response_rules: skill.response_rules || [],
    });
  }
  const stepIndex = stepIndexFromPath(path);
  if (stepIndex === null) return '';
  return JSON.stringify(skillGraphSteps(skill)[stepIndex] || null);
}

function collectTextDiffs(previousDraft: SkillCard, nextDraft: SkillCard, changedPaths: string[]): TextDiffAnimation[] {
  const diffs: TextDiffAnimation[] = [];
  const paths = changedPaths.includes('all') ? allTargetPaths(nextDraft) : changedPaths;
  paths.forEach((path) => {
    if (path === 'basic') {
      [
        'skill_id',
        'name',
        'version',
        'business_domain',
        'description',
        'trigger_intents',
        'user_utterance_examples',
        'goal',
        'required_info',
        'response_rules',
      ].forEach((field) => {
        const diff = makeTextDiff(
          path,
          field,
          getDisplayField(previousDraft, path, field),
          getDisplayField(nextDraft, path, field),
        );
        if (diff) diffs.push(diff);
      });
      return;
    }
    const stepIndex = stepIndexFromPath(path);
    if (stepIndex === null) return;
    ['step_id', 'type', 'condition', 'name', 'instruction', 'expected_user_info', 'allowed_actions'].forEach((field) => {
      const diff = makeTextDiff(
        path,
        field,
        getDisplayField(previousDraft, path, field),
        getDisplayField(nextDraft, path, field),
      );
      if (diff) diffs.push(diff);
    });
  });
  return diffs;
}

function makeTextDiff(path: string, field: string, oldText: string, newText: string): TextDiffAnimation | null {
  if (oldText === newText) return null;
  let prefixLength = 0;
  const maxPrefix = Math.min(oldText.length, newText.length);
  while (prefixLength < maxPrefix && oldText[prefixLength] === newText[prefixLength]) {
    prefixLength += 1;
  }
  let suffixLength = 0;
  const maxSuffix = Math.min(oldText.length - prefixLength, newText.length - prefixLength);
  while (
    suffixLength < maxSuffix &&
    oldText[oldText.length - 1 - suffixLength] === newText[newText.length - 1 - suffixLength]
  ) {
    suffixLength += 1;
  }
  return {
    key: `${path}:${field}`,
    path,
    field,
    prefix: newText.slice(0, prefixLength),
    removed: oldText.slice(prefixLength, oldText.length - suffixLength),
    inserted: newText.slice(prefixLength, newText.length - suffixLength),
    suffix: newText.slice(newText.length - suffixLength),
    phase: 'mark',
    progress: 0,
  };
}

function getDisplayField(skill: SkillCard, path: string, field: string): string {
  const value =
    path === 'basic'
      ? (skill as unknown as Record<string, unknown>)[field]
      : skillGraphSteps(skill)[stepIndexFromPath(path) ?? -1]?.[field];
  if (Array.isArray(value)) return joinList(value.map(String));
  if (typeof value === 'string') return value;
  return '';
}

function setTextField(skill: SkillCard, path: string, field: string, value: string): void {
  if (isListField(field)) return;
  if (path === 'basic') {
    (skill as unknown as Record<string, unknown>)[field] = value;
    return;
  }
  const stepIndex = stepIndexFromPath(path);
  if (stepIndex === null) return;
  if (Array.isArray(skill.nodes) && skill.nodes[stepIndex]) {
    const nodeField = field === 'step_id' ? 'node_id' : field;
    skill.nodes[stepIndex][nodeField] = value;
  }
}

function isListField(field: string): boolean {
  return [
    'trigger_intents',
    'user_utterance_examples',
    'goal',
    'required_info',
    'response_rules',
    'expected_user_info',
    'allowed_actions',
  ].includes(field);
}

function typedDraft(previousDraft: SkillCard, nextDraft: SkillCard, diffs: TextDiffAnimation[], progress: number): SkillCard {
  const output = cloneSkill(previousDraft);
  diffs.forEach((diff) => {
    const typedInsert = diff.inserted.slice(0, Math.ceil(diff.inserted.length * progress));
    setTextField(output, diff.path, diff.field, `${diff.prefix}${typedInsert}${diff.suffix}`);
  });
  if (progress >= 1) return cloneSkill(nextDraft);
  return output;
}

function bumpSkillVersion(version: string): string {
  const parts = version.split('.').map((item) => Number.parseInt(item, 10));
  const major = Number.isFinite(parts[0]) ? parts[0] : 1;
  const minor = Number.isFinite(parts[1]) ? parts[1] : 0;
  return `${major}.${minor + 1}.0`;
}

function buildToolDescriptionMap(tools: ToolRead[], messages: ChatItem[] = []): ToolDescriptionMap {
  const descriptions = tools.reduce<ToolDescriptionMap>((acc, tool) => {
    acc[tool.name] = [tool.display_name, tool.description].filter(Boolean).join('：') || tool.name;
    return acc;
  }, {});
  messages.flatMap((item) => item.toolSuggestions || []).forEach((suggestion) => {
    const label = suggestion.display_name || suggestion.name;
    descriptions[suggestion.name] = [label, suggestion.description || suggestion.reason].filter(Boolean).join('：') || suggestion.name;
    if (suggestion.matched_tool_name) {
      descriptions[suggestion.matched_tool_name] = descriptions[suggestion.name];
    }
  });
  return descriptions;
}

function buildToolStatusMap(tools: ToolRead[], messages: ChatItem[]): ToolStatusMap {
  const statuses = tools.reduce<ToolStatusMap>((acc, tool) => {
    acc[tool.name] = 'existing';
    return acc;
  }, {});
  messages.flatMap((item) => item.toolSuggestions || []).forEach((suggestion) => {
    const resolution = toolSuggestionResolution(suggestion);
    const status: ToolActionStatus =
      resolution === 'existing'
        ? 'existing'
        : resolution === 'incomplete'
          ? 'incomplete'
          : suggestion.status === 'accepted' || suggestion.status === 'created' || suggestion.status === 'rejected'
            ? suggestion.status
            : 'pending';
    statuses[suggestion.name] = status;
    if (suggestion.matched_tool_name) statuses[suggestion.matched_tool_name] = status;
  });
  return statuses;
}

function toolPayloadFromSuggestion(suggestion: ToolSuggestionItem, skillId?: string): Record<string, unknown> {
  const outputSchema = suggestion.probe_result?.success && suggestion.probe_result.inferred_output_schema
    ? suggestion.probe_result.inferred_output_schema
    : suggestion.output_schema || {};
  return {
    tenant_id: TENANT_ID,
    name: suggestion.name,
    display_name: suggestion.display_name || suggestion.name,
    description: suggestion.description || suggestion.reason || '',
    bucket: suggestion.bucket || '技能自发现工具',
    tool_type: suggestion.tool_type || 'http',
    method: suggestion.method || 'POST',
    url: suggestion.url || `/api/mock/${suggestion.name.replace(/\./g, '/')}`,
    headers: {},
    auth: {},
    mcp_config: suggestion.tool_type === 'mcp' ? suggestion.mcp_config || {} : {},
    input_schema: suggestion.input_schema || {},
    output_schema: outputSchema,
    allowed_skills: skillId ? [skillId] : [],
    enabled: true,
  };
}

function toolReadFromSuggestion(suggestion: ToolSuggestionItem, skillId?: string): ToolRead {
  const outputSchema = suggestion.probe_result?.success && suggestion.probe_result.inferred_output_schema
    ? suggestion.probe_result.inferred_output_schema
    : suggestion.output_schema || {};
  return {
    id: suggestion.name,
    tenant_id: TENANT_ID,
    name: suggestion.name,
    display_name: suggestion.display_name || suggestion.name,
    description: suggestion.description || suggestion.reason || '',
    bucket: suggestion.bucket || '技能自发现工具',
    tool_type: suggestion.tool_type || 'http',
    method: suggestion.method || 'POST',
    url: suggestion.url || `/api/mock/${suggestion.name.replace(/\./g, '/')}`,
    headers: {},
    auth: {},
    mcp_config: suggestion.tool_type === 'mcp' ? suggestion.mcp_config || {} : {},
    input_schema: suggestion.input_schema || {},
    output_schema: outputSchema,
    allowed_skills: skillId ? [skillId] : [],
    enabled: true,
    updated_at: new Date().toISOString(),
  };
}

function upsertToolRead(current: ToolRead[], nextTool: ToolRead): ToolRead[] {
  const exists = current.some((tool) => tool.name === nextTool.name);
  return exists
    ? current.map((tool) => (tool.name === nextTool.name ? { ...tool, ...nextTool, id: nextTool.id || tool.id } : tool))
    : [...current, nextTool];
}

function buildToolIntegrationInstruction(suggestions: ToolSuggestionItem | ToolSuggestionItem[]): string {
  const items = Array.isArray(suggestions) ? suggestions : [suggestions];
  const toolDetails = items.map((suggestion) => {
    const displayName = suggestion.display_name || suggestion.name;
    return {
      name: suggestion.name,
      display_name: displayName,
      description: suggestion.description || '',
      method: suggestion.method || 'POST',
      url: suggestion.url || '',
      input_schema: suggestion.input_schema || {},
      output_schema: suggestion.probe_result?.success && suggestion.probe_result.inferred_output_schema
        ? suggestion.probe_result.inferred_output_schema
        : suggestion.output_schema || {},
      sample_arguments: suggestion.sample_arguments || {},
    };
  });
  return [
    '以下工具已经新增到工具配置。',
    '请更新当前技能：统一判断这些工具分别应接入哪些节点，并只在确实需要调用工具的节点中加入对应 allowed_actions。',
    '同步改写对应节点说明，使模型在参数满足时调用工具，并根据工具结果继续推进或给出最终回复；不要修改无关字段。',
    `工具详情：${JSON.stringify(toolDetails, null, 2)}`,
  ].join('\n');
}

function fieldLabel(field: string): string {
  const labels: Record<string, string> = {
    skill_id: '技能 ID',
    name: '名称',
    version: '版本',
    business_domain: '业务域',
    description: '描述',
    trigger_intents: '触发意图',
    user_utterance_examples: '示例话术',
    goal: '目标',
    required_info: '必填信息',
    response_rules: '回复规则',
    step_id: '节点 ID',
    type: '节点类型',
    condition: '条件',
    instruction: '节点说明',
    expected_user_info: '期望字段',
    allowed_actions: '允许动作',
  };
  return labels[field] || field;
}

function toolNameFromAction(action: string): string {
  return action.startsWith('call_tool:') ? action.replace(/^call_tool:/, '').trim() : '';
}

function actionLabel(action: string): string {
  const toolName = toolNameFromAction(action);
  if (toolName) return `调用工具：${toolName}`;
  const labels: Record<string, string> = {
    ask_user: '询问用户',
    continue_flow: '继续流程',
    answer_user: '回复用户',
    handoff_human: '转人工',
    ask_clarification: '澄清问题',
    clarify_user: '澄清用户需求',
    update_memory: '更新记忆',
    reflect: '反思',
    finish: '结束流程',
    stop: '停止流程',
  };
  return labels[action] || action;
}

function diffTargetLabel(path: string, skill: SkillCard | null): string {
  if (!skill) return path;
  return targetLabel([path], skill);
}

function targetLabel(paths: string[], skill: SkillCard): string {
  const labels = paths.map((path) => {
    if (path === 'basic') return '基础信息';
    const stepIndex = stepIndexFromPath(path);
    if (stepIndex !== null) {
      const index = stepIndex;
      const step = index >= 0 ? skillGraphSteps(skill)[index] : null;
      return step ? `节点 ${index + 1}：${step.name || step.step_id || path}` : path;
    }
    return path;
  });
  return labels.join('、');
}

function stepTargetPath(index: number): string {
  return `nodes[${index}]`;
}

function stepIndexFromPath(path: string): number | null {
  const match = path.match(/^nodes\[(\d+)\]$/);
  return match ? Number(match[1]) : null;
}
