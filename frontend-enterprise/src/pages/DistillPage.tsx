import {
  ApiOutlined,
  ArrowLeftOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CheckOutlined,
  CodeOutlined,
  CloseOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  DownOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  PlusOutlined,
  RightOutlined,
  SaveOutlined,
  SendOutlined,
  StopOutlined,
  UploadOutlined,
  WarningOutlined,
} from '../icons';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEventHandler,
  type ClipboardEvent,
  type CSSProperties,
  type DragEvent,
  type HTMLAttributes,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Select as UISelect,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import AppHeader from '@/components/AppHeader';
import { ModelConfigDropdown } from '@/components/ModelConfigDropdown';
import { cn } from '@/lib/utils';
import { SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';
import type { EnterpriseAuthUser } from '../auth';
import {
  ACTION_EMPTY_CLASS,
  ACTION_LIST_CLASS,
  CARD_OUTLINE_BUTTON_CLASS,
  CHAT_ACTIONS_GROUP_CLASS,
  CHAT_CARD_BODY_CLASS,
  CHAT_ATTACHMENT_CLASS,
  CHAT_ATTACHMENT_ICON_CLASS,
  CHAT_ATTACHMENT_MAIN_CLASS,
  CHAT_ATTACHMENT_NAME_CLASS,
  CHAT_ATTACHMENT_TYPE_CLASS,
  CHAT_ATTACHMENT_USER_CLASS,
  CHAT_ATTACHMENTS_CLASS,
  CHAT_ATTACHMENTS_USER_CLASS,
  CHAT_CARD_CLASS,
  CHAT_CARD_DRAGGING_CLASS,
  CHAT_ACTIONS_CLASS,
  CHAT_COMPOSER_SHELL_CLASS,
  CHAT_CONFIRM_CLASS,
  CHAT_CONTENT_CLASS,
  CHAT_CONTENT_USER_ATTACHMENTS_CLASS,
  CHAT_DECISION_CLASS,
  CHAT_EDIT_ACTIONS_CLASS,
  CHAT_EDIT_PANEL_CLASS,
  CHAT_EDIT_PANEL_USER_ATTACHMENTS_CLASS,
  CHAT_EDIT_TEXTAREA_CLASS,
  CHAT_HOVER_ACTIONS_CLASS,
  CHAT_HOVER_BUTTON_CLASS,
  CHAT_MESSAGES_CLASS,
  CHAT_PANEL_CLASS,
  CHAT_COMPOSER_CLASS,
  CHAT_TEXTAREA_CLASS,
  CHAT_THINKING_BLOCK_CLASS,
  CHAT_THINKING_BUTTON_CLASS,
  CHAT_THINKING_DETAIL_CLASS,
  CHAT_THINKING_DETAILS_CLASS,
  CHAT_TIME_CLASS,
  CHAT_UPLOAD_DROP_HINT_CLASS,
  CHAT_WARNING_CLASS,
  CHAT_WARNING_ITEM_CLASS,
  CHAT_WARNING_TITLE_CLASS,
  CONDITION_EDITOR_CLASS,
  CONDITION_INPUT_CLASS,
  CONDITION_PRESET_CLASS,
  CONDITION_READABLE_CLASS,
  DIFF_NEW_CLASS,
  DIFF_OLD_CLASS,
  DISTILL_CARD_BODY_CLASS,
  DISTILL_CARD_CLASS,
  DISTILL_CARD_HEADER_CLASS,
  DISTILL_ACTIONS_CLASS,
  DISTILL_PAGE_CLASS,
  FLOW_CHIP_CLASS,
  FLOW_CHIP_LIST_CLASS,
  FLOW_CHIP_MUTED_CLASS,
  FLOW_CLASS,
  FLOW_COMPACT_META_CLASS,
  FLOW_COMPACT_ROW_CLASS,
  FLOW_EDGES_CLASS,
  FLOW_EDGE_PATH_CLASS,
  FLOW_GRAPH_CANVAS_CLASS,
  FLOW_META_CLASS,
  FLOW_META_LABEL_CLASS,
  FLOW_META_ROW_CLASS,
  FLOW_NODE_BADGES_CLASS,
  FLOW_NODE_POSITION_CLASS,
  FLOW_NODE_SHELL_CLASS,
  FLOW_NODE_SUMMARY_CLASS,
  FLOW_ROOT_POSITION_CLASS,
  FLOW_ROUTE_COUNT_CLASS,
  FLOW_RULE_CONDITION_CONTROLS_CLASS,
  FLOW_RULE_CONDITION_INPUT_CLASS,
  FLOW_RULE_DELETE_CLASS,
  FLOW_RULE_EDITOR_CLASS,
  FLOW_RULE_EMPTY_CLASS,
  FLOW_RULE_FIELD_CLASS,
  FLOW_RULE_FIELD_CONDITION_CLASS,
  FLOW_RULE_FIELD_LABEL_CLASS,
  FLOW_RULE_FIELD_PRIORITY_CLASS,
  FLOW_RULE_FIELD_TARGET_CLASS,
  FLOW_RULE_HEAD_CLASS,
  FLOW_RULE_ITEM_CLASS,
  FLOW_RULE_LABEL_INPUT_CLASS,
  FLOW_RULE_LIST_CLASS,
  FLOW_RULE_PRIORITY_CLASS,
  FLOW_RULE_TARGET_CLASS,
  FLOW_ZOOM_SHELL_CLASS,
  FLOW_ZOOM_STEP_BUTTON_CLASS,
  FLOW_ZOOM_TOOLBAR_CLASS,
  FLOW_ZOOM_VALUE_CLASS,
  flowZoomPresetButtonClass,
  INLINE_ADD_CLASS,
  INLINE_ADD_SETTLED_CLASS,
  INLINE_REMOVE_CLASS,
  NODE_DELETE_CONFIRM_CLASS,
  PILL_OUTLINE_BUTTON_CLASS,
  NODE_INSERT_BUTTON_CLASS,
  NODE_INSERT_ROW_CLASS,
  NODE_INSERT_ROW_EDGE_CLASS,
  RETRY_POLICY_EDITOR_CLASS,
  RETRY_POLICY_FIELD_CLASS,
  PRIMARY_BUTTON_CLASS,
  RETURN_BUTTON_CLASS,
  REWRITE_MODEL_BUTTON_CLASS,
  SAVE_REVIEW_ACTION_DIFF_CLASS,
  SAVE_REVIEW_ACTION_DIFF_NEW_CLASS,
  SAVE_REVIEW_ACTION_DIFF_OLD_CLASS,
  SAVE_REVIEW_DIFF_CLASS,
  SAVE_REVIEW_DIFF_PATH_CLASS,
  SAVE_REVIEW_DIFF_ROW_CLASS,
  SAVE_REVIEW_DIFF_SIGN_CLASS,
  SAVE_REVIEW_DIFF_SIGN_NEW_CLASS,
  SAVE_REVIEW_DIFF_SIGN_OLD_CLASS,
  SAVE_REVIEW_FORM_CLASS,
  SAVE_REVIEW_FORM_LABEL_CLASS,
  SECTION_CARD_TITLE_CLASS,
  SELECTION_MARK_CLASS,
  SOURCE_ACTION_ADD_CLASS,
  SOURCE_ACTION_EDIT_BUTTON_CLASS,
  SOURCE_ACTION_EDITOR_CLASS,
  SOURCE_ACTION_LIST_CLASS,
  SOURCE_ACTION_LIST_EDITABLE_CLASS,
  SOURCE_ACTION_PICKER_CLASS,
  SOURCE_ACTION_REMOVE_CLASS,
  SOURCE_ACTION_SELECT_CLASS,
  SOURCE_ACTION_TOKEN_CLASS,
  SOURCE_EMPTY_STATE_CLASS,
  SOURCE_EMPTY_TEXT_CLASS,
  SOURCE_CARD_CLASS,
  SOURCE_COLLAPSIBLE_EDITOR_CLASS,
  SOURCE_COLLAPSIBLE_HEAD_CLASS,
  SOURCE_COLLAPSIBLE_PREVIEW_CLASS,
  SOURCE_COLLAPSIBLE_PREVIEW_MUTED_CLASS,
  SOURCE_COLLAPSIBLE_TOGGLE_CLASS,
  SOURCE_EDIT_FIELD_CLASS,
  SOURCE_EDIT_HINT_CLASS,
  SOURCE_EDIT_INPUT_CLASS,
  SOURCE_GROUP_TITLE_CLASS,
  SOURCE_INPUT_CLASS,
  SOURCE_JSON_INLINE_CLASS,
  SOURCE_KEY_CLASS,
  SOURCE_LINE_CLASS,
  SOURCE_MD_CLASS,
  SOURCE_META_LIST_CLASS,
  SOURCE_READONLY_VALUE_CLASS,
  SOURCE_RENDERED_CLASS,
  SOURCE_SELECT_CLASS,
  SOURCE_STEP_BLOCK_CLASS,
  SOURCE_STEP_HEADER_CLASS,
  SOURCE_STEP_TITLE_EDIT_CLASS,
  SOURCE_STEPS_CLASS,
  SOURCE_TITLE_INPUT_CLASS,
  SOURCE_TOOLBAR_CLASS,
  SOURCE_VALUE_CLASS,
  TOOL_ACTION_BUTTON_CLASS,
  TOOL_ACTION_CONFIRM_CLASS,
  TOOL_ACTION_GROUP_CLASS,
  TOOL_ACTION_GROUP_DETAIL_CLASS,
  TOOL_ACTION_REJECT_CLASS,
  TOOL_METHOD_CLASS,
  TOOL_SUGGESTION_ACTIONS_CLASS,
  TOOL_SUGGESTION_CLASS,
  TOOL_SUGGESTION_DESC_CLASS,
  TOOL_SUGGESTION_DETAIL_CLASS,
  TOOL_SUGGESTION_DETAIL_FOOTER_CLASS,
  TOOL_SUGGESTION_DETAIL_PRE_CLASS,
  TOOL_SUGGESTION_HEAD_CLASS,
  TOOL_SUGGESTION_MAIN_CLASS,
  TOOL_SUGGESTION_META_CLASS,
  TOOL_SUGGESTION_TITLE_CLASS,
  TOOL_SUGGESTIONS_CLASS,
  UPLOAD_LIST_CLASS,
  UPLOAD_NAME_CLASS,
  UPLOAD_STATUS_CLASS,
  WORKBENCH_CLASS,
  actionChipClass,
  chatBubbleClass,
  chatRowClass,
  distillFlowNodeClass,
  distillSourceSectionClass,
  flowEdgeLabelClass,
  toolStatusBadgeClass,
  uploadItemClass,
  type ToolStatusBadgeVariant,
} from './distillPageStyles';
import { api, ApiError, streamGet, streamPost, TENANT_ID } from '../api/client';
import type { ModelConfigRead, SkillCard, SkillRead, ToolProbeResponse, ToolRead, ToolSuggestion } from '../types';

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

type SelectOption = {
  value: string;
  label: string;
};

const NODE_TYPE_OPTIONS: SelectOption[] = [
  { value: 'collect_info', label: '收集信息' },
  { value: 'decision', label: '条件判断' },
  { value: 'tool_call', label: '调用工具' },
  { value: 'knowledge_query', label: '检索知识' },
  { value: 'response', label: '回复用户' },
  { value: 'handoff', label: '转人工' },
  { value: 'subflow', label: '子流程' },
];

const BASE_ACTION_OPTIONS: SelectOption[] = [
  { value: 'ask_user', label: '询问用户' },
  { value: 'continue_flow', label: '继续流程' },
  { value: 'answer_user', label: '回复用户' },
  { value: 'handoff_human', label: '转人工' },
  { value: 'ask_clarification', label: '澄清问题' },
  { value: 'clarify_user', label: '澄清用户需求' },
  { value: 'update_memory', label: '更新记忆' },
  { value: 'reflect', label: '反思检查' },
  { value: 'finish', label: '结束流程' },
  { value: 'stop', label: '停止流程' },
];

const CONDITION_PRESET_OPTIONS: SelectOption[] = [
  { value: '__always__', label: '总是可进入' },
  { value: 'missing_required_info', label: '缺少任一必填信息' },
  { value: 'missing_slots([])', label: '缺少指定字段' },
  { value: 'all_required_info_collected', label: '必填信息已收集完成' },
  { value: 'tool_success', label: '工具执行成功' },
  { value: 'tool_failed', label: '工具执行失败' },
  { value: 'user_confirmed', label: '用户已确认' },
  { value: 'user_rejected', label: '用户已拒绝' },
  { value: '__custom__', label: '自定义条件' },
];

const CONDITION_PRESET_TEXT: Record<string, string> = {
  missing_required_info: '还有必填信息没有收集到时进入',
  'missing_slots([])': '缺少某个指定字段时进入',
  all_required_info_collected: '所有必填信息都收集完成后进入',
  tool_success: '上一步工具调用成功后进入',
  tool_failed: '上一步工具调用失败后进入',
  user_confirmed: '用户明确确认后进入',
  user_rejected: '用户明确拒绝后进入',
};

const RETRY_STRATEGY_OPTIONS: SelectOption[] = [
  { value: 'ask_user', label: '继续追问用户' },
  { value: 'reflect', label: '反思并修正' },
  { value: 'retry_tool', label: '重新调用工具' },
  { value: 'handoff_human', label: '转人工处理' },
  { value: 'skip', label: '跳过当前节点' },
  { value: 'stop', label: '停止流程' },
];

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
const DISTILL_REWRITE_MODEL_STORAGE_KEY = 'skill-distill-rewrite-model';

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
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

function lockSkillIdForDraft(draft: SkillCard, lockedSkillId: string): SkillCard {
  if (!lockedSkillId || draft.skill_id === lockedSkillId) return draft;
  return { ...cloneSkill(draft), skill_id: lockedSkillId };
}

function lockNullableSkillIdForDraft(draft: SkillCard | null, lockedSkillId: string): SkillCard | null {
  return draft ? lockSkillIdForDraft(draft, lockedSkillId) : null;
}

function lockPendingChangeSkillId(change: PendingChange | null, lockedSkillId: string): PendingChange | null {
  if (!change || !lockedSkillId) return change;
  return {
    ...change,
    previousDraft: lockSkillIdForDraft(change.previousDraft, lockedSkillId),
    nextDraft: lockSkillIdForDraft(change.nextDraft, lockedSkillId),
  };
}

export default function DistillPage({ active = true, searchParamsOverride, currentUser, onLogout }: DistillPageProps = {}) {
  const navigate = useNavigate();
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
  const [clearNewConfirm, setClearNewConfirm] = useState<{ title: string; description: string } | null>(null);
  const [rerunConfirm, setRerunConfirm] = useState<{
    index: number;
    snapshot: DistillHistorySnapshot;
    rollbackOperations: DistillHistoryOperation[];
    text: string;
    outgoingText: string;
  } | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('source');
  const [loading, setLoading] = useState(false);
  const [attachments, setAttachments] = useState<UploadAttachment[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [toolDetail, setToolDetail] = useState<ToolSuggestionItem | null>(null);
  const [toolDetailMessageId, setToolDetailMessageId] = useState<string | null>(null);
  const [probeArgsText, setProbeArgsText] = useState('');
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [selectedRewriteModelId, setSelectedRewriteModelId] = useState(
    () => window.localStorage.getItem(`${DISTILL_REWRITE_MODEL_STORAGE_KEY}:${TENANT_ID}`) || '',
  );
  const [streamStatus, setStreamStatus] = useState('');
  const [activeJob, setActiveJob] = useState<ActiveDistillJob | null>(null);
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
      if (skillId && isBlankDistillWorkspace(cached)) {
        removeDistillCache(cacheKey);
      } else {
        const cachedLockedSkillId = cached.loadedSkill?.skill_id || skillId || '';
        setDraft(lockNullableSkillIdForDraft(cached.draft, cachedLockedSkillId));
        setLoadedSkill(cached.loadedSkill);
        setLastSavedDraft(lockNullableSkillIdForDraft(cached.lastSavedDraft, cachedLockedSkillId));
        setMessages(cached.messages.length > 0 ? cached.messages : DEFAULT_DISTILL_MESSAGES);
        setInput(cached.input);
        setSelectedPaths(normalizeInitialSelectedPaths(cached.selectedPaths));
        setHighlightedPaths(cached.highlightedPaths);
        setUpdatingPaths(cached.updatingPaths);
        setDirtyPaths(cached.dirtyPaths);
        setTextDiffs(cached.textDiffs);
        setPendingChange(lockPendingChangeSkillId(cached.pendingChange, cachedLockedSkillId));
        setViewMode(cached.viewMode || 'source');
        setAttachments(cached.attachments.filter((item) => item.status !== 'uploading'));
        setStreamStatus(cached.streamStatus);
        setActiveJob(cached.activeJob || null);
        if (cached.activeJob && cached.activeJob.status !== 'succeeded' && cached.activeJob.status !== 'failed') {
          setLoading(true);
        }
        setSaveDraftSnapshot(null);
        setHydratedCacheKey(cacheKey);
        setCacheReady(true);
        return;
      }
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
      setSaveDraftSnapshot(null);
      setHydratedCacheKey(cacheKey);
      setCacheReady(true);
      return;
    }

    api
      .get<SkillRead>(`/api/enterprise/skills/${encodeURIComponent(skillId)}?tenant_id=${TENANT_ID}${agentQuery}`)
      .then((result) => {
        const nextContent = lockSkillIdForDraft(result.content, result.skill_id || skillId || '');
        const nextResult = nextContent === result.content ? result : { ...result, content: nextContent };
        setDraft(nextContent);
        setLoadedSkill(nextResult);
        setLastSavedDraft(nextContent);
        setSelectedPaths(DEFAULT_TARGET_PATHS);
        setPendingChange(null);
        setHighlightedPaths([]);
        setUpdatingPaths([]);
        setDirtyPaths([]);
        setTextDiffs([]);
        setAttachments([]);
        setStreamStatus('');
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
        notify.error(error instanceof Error ? error.message : '加载技能失败');
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
    messages,
    pendingChange,
    selectedPaths,
    streamStatus,
    activeJob,
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
        notify.error(error instanceof Error ? error.message : '恢复生成失败');
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
      .get<ToolRead[]>(`/api/enterprise/tools?tenant_id=${TENANT_ID}${agentQuery}`)
      .then(setTools)
      .catch(() => setTools([]));
  }, [agentQuery]);

  useEffect(() => {
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((rows) => {
        const enabled = rows.filter((item) => item.enabled);
        setModelConfigs(enabled);
        setSelectedRewriteModelId((current) => {
          if (current && enabled.some((item) => item.id === current)) return current;
          const fallback = enabled.find((item) => item.is_default)?.id || enabled[0]?.id || '';
          if (fallback) {
            window.localStorage.setItem(`${DISTILL_REWRITE_MODEL_STORAGE_KEY}:${TENANT_ID}`, fallback);
          }
          return fallback;
        });
      })
      .catch(() => setModelConfigs([]));
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
  const lockedSkillId = loadedSkill?.skill_id || skillId || '';
  const saveReviewDraft = useMemo(() => {
    const sourceDraft = saveDraftSnapshot || draft;
    if (!sourceDraft) return null;
    const nextDraft = {
      ...cloneSkill(sourceDraft),
      name: saveName.trim() || sourceDraft.name,
      business_domain: saveDomain.trim() || undefined,
      version: saveVersion.trim() || sourceDraft.version,
    };
    return lockSkillIdForDraft(nextDraft, lockedSkillId);
  }, [draft, lockedSkillId, saveDomain, saveDraftSnapshot, saveName, saveVersion]);
  const saveReviewDiffs = useMemo(() => {
    if (!saveReviewDraft) return [];
    const baseDraft = lastSavedDraft || blankSkillForAnimation(saveReviewDraft);
    const changedPaths = diffTargetPaths(baseDraft, saveReviewDraft, allTargetPaths(saveReviewDraft));
    return collectTextDiffs(baseDraft, saveReviewDraft, changedPaths).filter((diff) => diff.field !== 'version');
  }, [lastSavedDraft, saveReviewDraft]);
  const hasSaveableDraftChanges = useMemo(
    () => hasSkillContentChanges(lockNullableSkillIdForDraft(pendingChange?.nextDraft || draft, lockedSkillId), lastSavedDraft),
    [draft, lastSavedDraft, lockedSkillId, pendingChange],
  );
  const saveReviewHasContentChanges = useMemo(
    () => hasSkillContentChanges(saveReviewDraft, lastSavedDraft),
    [lastSavedDraft, saveReviewDraft],
  );

  useEffect(() => {
    if (!lockedSkillId) return;
    setDraft((current) => lockNullableSkillIdForDraft(current, lockedSkillId));
    setLastSavedDraft((current) => lockNullableSkillIdForDraft(current, lockedSkillId));
    setSaveDraftSnapshot((current) => lockNullableSkillIdForDraft(current, lockedSkillId));
    setPendingChange((current) => lockPendingChangeSkillId(current, lockedSkillId));
  }, [lockedSkillId]);

  async function send() {
    const text = buildOutgoingText(input, readyAttachments);
    if (!text || loading || uploadingFile) return;
    const displayText = input.trim();
    const displayAttachments = buildDisplayAttachments(readyAttachments);
    const snapshotBefore = createHistorySnapshot();
    const confirmedDraft = lockNullableSkillIdForDraft(pendingChange?.nextDraft || draft, lockedSkillId);
    confirmPendingChange(false);
    setInput('');
    setAttachments([]);
    pushMessage('user', displayText, { attachments: displayAttachments, outgoingText: text, snapshotBefore });
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
    setStreamStatus('正在生成 SOP 草稿');
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
        { tenant_id: TENANT_ID, ...payload, model_config_id: selectedRewriteModelId || undefined },
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
            const draftSkill = lockSkillIdForDraft(item.data.draft_skill as SkillCard, lockedSkillId);
            const nextWarnings = Array.isArray(item.data.warnings) ? item.data.warnings.map(String) : [];
            const nextToolSuggestions = normalizeToolSuggestions(item.data.tool_suggestions);
            appendThinkingDetail(assistantId, `已生成 SOP 草稿：${draftSkill.name}`);
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
                operations: [{ kind: 'skill_change', label: `生成 SOP 草稿：${draftSkill.name}`, skillId: draftSkill.skill_id }],
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
        notify.info('已停止生成');
      } else {
        notify.error(error instanceof Error ? error.message : '生成失败');
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
    const editableDraft = lockSkillIdForDraft(currentDraft, lockedSkillId);
    const previousDraft = cloneSkill(editableDraft);
    const targets = targetPathsOverride?.length
      ? targetPathsOverride
      : selectedPaths.length > 0
        ? selectedPaths
        : allTargetPaths(editableDraft);
    const scopeLabel = targetLabel(targets, editableDraft);
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
        `/api/enterprise/skills/${encodeURIComponent(editableDraft.skill_id)}/rewrite/stream`,
        {
          tenant_id: TENANT_ID,
          current_skill: editableDraft,
          instruction: text,
          model_config_id: selectedRewriteModelId || undefined,
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
            const nextDraft = lockSkillIdForDraft(item.data.draft_skill as SkillCard, lockedSkillId);
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
        notify.info('已停止改写');
      } else {
        notify.error(error instanceof Error ? error.message : '改写失败');
      }
    } finally {
      finishStream(controller);
    }
  }

  function openSaveReview(options: { clearAfterSave?: boolean } = {}) {
    const targetDraft = lockNullableSkillIdForDraft(pendingChange?.nextDraft || draft, lockedSkillId);
    if (!targetDraft) return;
    if (!hasSkillContentChanges(targetDraft, lastSavedDraft)) {
      notify.info('当前没有内容变化，无需保存草稿。');
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
      notify.info('当前没有内容变化，无需保存草稿。');
      return;
    }
    let finalDraft: SkillCard = lockSkillIdForDraft(saveReviewDraft, lockedSkillId);
    let renamedSkillId = '';
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
          savedSkill = await api.post<SkillRead>(`/api/enterprise/skills${agentOnlyQuery}`, { tenant_id: TENANT_ID, content: finalDraft, status: 'published' });
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 409) throw error;
          finalDraft = {
            ...cloneSkill(finalDraft),
            skill_id: uniqueDraftSkillId(finalDraft.skill_id),
          };
          renamedSkillId = finalDraft.skill_id;
          savedSkill = await api.post<SkillRead>(`/api/enterprise/skills${agentOnlyQuery}`, { tenant_id: TENANT_ID, content: finalDraft, status: 'published' });
        }
      }
      const savedContent = lockSkillIdForDraft(savedSkill.content, savedSkill.skill_id || lockedSkillId);
      if (savedContent !== savedSkill.content) {
        savedSkill = { ...savedSkill, content: savedContent };
      }
      setLoadedSkill(savedSkill);
      setDraft(savedContent);
      setLastSavedDraft(savedContent);
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
        notify.success('SOP 已保存，当前改写已清空');
      } else {
        notify.success(renamedSkillId ? `SOP ID 已存在，已另存为 ${renamedSkillId}` : 'SOP 已保存');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
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
    const rawDraftSkill = data.draft_skill as SkillCard | undefined;
    if (!rawDraftSkill) return;
    const draftSkill = lockSkillIdForDraft(rawDraftSkill, lockedSkillId);
    const nextWarnings = Array.isArray(data.warnings) ? data.warnings.map(String) : [];
    const nextToolSuggestions = normalizeToolSuggestions(data.tool_suggestions);
    clearAnimationTimers();
    setDraft(draftSkill);
    setHighlightedPaths([]);
    setUpdatingPaths([]);
    setTextDiffs([]);
    setSelectedPaths(DEFAULT_TARGET_PATHS);
    appendThinkingDetail(job.assistantId, `已生成 SOP 草稿：${draftSkill.name}`);
    updateMessage(
      job.assistantId,
      `已生成「${draftSkill.name}」草稿。你可以在右侧选择一个或多个区域继续改写。`,
      {
        thinking: 'done',
        warnings: nextWarnings,
        toolSuggestions: nextToolSuggestions,
        operations: [{ kind: 'skill_change', label: `生成 SOP 草稿：${draftSkill.name}`, skillId: draftSkill.skill_id }],
      },
    );
    setStreamStatus('生成完成');
    if (nextToolSuggestions.length > 0) {
      void autoProbeToolSuggestions(job.assistantId, nextToolSuggestions);
    }
  }

  function completeResumedRewriteJob(job: ActiveDistillJob, data: Record<string, unknown>) {
    const rawNextDraft = data.draft_skill as SkillCard | undefined;
    if (!rawNextDraft) return;
    const nextDraft = lockSkillIdForDraft(rawNextDraft, lockedSkillId);
    const previousDraft = lockNullableSkillIdForDraft(job.previousDraft || draft, lockedSkillId);
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
      notify.error('仅支持 .md、.doc、.docx、.txt 文件');
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
      if (!options.silent) notify.warning('缺少样例参数，无法测试接口');
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
        ...toolPayloadFromSuggestion(suggestion, lockNullableSkillIdForDraft(pendingChange?.nextDraft || draft, lockedSkillId)?.skill_id),
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
        if (!options.silent) notify.success('接口测试成功');
      } else {
        if (!options.silent) notify.error(result.error?.message || '接口测试失败');
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
      if (!options.silent) notify.error(result.error?.message || '接口测试失败');
      return result;
    }
  }

  function applyProbeArgumentsFromDetail() {
    if (!toolDetail || !toolDetailMessageId) return;
    const parsed = parseJsonObject(probeArgsText);
    if (!parsed) {
      notify.error('样例参数必须是 JSON 对象');
      return;
    }
    setToolSuggestionPatch(toolDetailMessageId, toolDetail.name, { sample_arguments: parsed });
    setToolDetail({ ...toolDetail, sample_arguments: parsed });
    notify.success('样例参数已更新');
  }

  function probeToolDetail() {
    if (!toolDetail || !toolDetailMessageId) return;
    const parsed = parseJsonObject(probeArgsText);
    if (!parsed) {
      notify.error('样例参数必须是 JSON 对象');
      return;
    }
    void probeToolSuggestion(toolDetailMessageId, { ...toolDetail, sample_arguments: parsed }, { sampleArguments: parsed });
  }

  async function confirmToolSuggestion(messageId: string, suggestion: ToolSuggestionItem) {
    if (loading) return;
    if (toolSuggestionResolution(suggestion) !== 'new_candidate') {
      notify.warning('该工具不是可新增候选');
      return;
    }
    if (!suggestion.probe_result?.success) {
      notify.warning('请先测试接口成功后再新增工具');
      return;
    }
    const nextSuggestions = nextToolSuggestionsWithPatch(messageId, suggestion.name, { status: 'accepted' });
    setToolSuggestionStatus(messageId, suggestion.name, 'accepted');
    const shouldCommit = toolSuggestionSelectionsComplete(nextSuggestions);
    if (!shouldCommit) {
      notify.success('已确认，等待其他工具建议处理完成后统一更新 SOP');
      return;
    }
    await commitToolSuggestionSelections(messageId, nextSuggestions);
  }

  async function commitToolSuggestionSelections(messageId: string, suggestions: ToolSuggestionItem[]) {
    const activeDraft = lockNullableSkillIdForDraft(pendingChange?.nextDraft || draft, lockedSkillId);
    const acceptedSuggestions = suggestions.filter(
      (item) => toolSuggestionResolution(item) === 'new_candidate' && item.status === 'accepted',
    );
    if (acceptedSuggestions.length === 0) {
      notify.info('所有工具建议已拒绝，SOP 草稿未变更');
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
          createdTool = await api.post<ToolRead>(`/api/enterprise/tools${agentQuery ? `?${agentQuery.slice(1)}` : ''}`, payload);
          createdNewTool = true;
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 409) throw error;
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
      const toolNames = acceptedSuggestions.map((item) => item.display_name || item.name).join('、');
      const nextDraft = lockSkillIdForDraft(
        integrateToolSuggestionsIntoDraft(
          activeDraft,
          acceptedSuggestions,
          pendingChange?.changedPaths?.length ? pendingChange.changedPaths : selectedPaths,
        ),
        lockedSkillId,
      );
      const changedPaths = diffTargetPaths(activeDraft, nextDraft, allTargetPaths(nextDraft));
      confirmPendingChange(false);
      clearAnimationTimers();
      setDraft(nextDraft);
      setPendingChange(null);
      setUpdatingPaths([]);
      setTextDiffs([]);
      if (changedPaths.length > 0) {
        setHighlightedPaths((current) => mergePaths(current, changedPaths));
        setDirtyPaths((current) => mergePaths(current, changedPaths));
        appendOperationToMessage(messageId, {
          kind: 'skill_change',
          label: `接入工具：${toolNames}`,
          skillId: nextDraft.skill_id,
        });
      }
      notify.success(`已确认 ${acceptedSuggestions.length} 个工具，当前草稿已局部更新`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '新增工具或更新 SOP 失败');
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
    setDraft((current) => (current ? lockSkillIdForDraft(removeToolActionFromSkill(current, toolName), lockedSkillId) : current));
    setPendingChange((current) =>
      current
        ? {
            ...current,
            nextDraft: lockSkillIdForDraft(removeToolActionFromSkill(current.nextDraft, toolName), lockedSkillId),
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
      setClearNewConfirm({
        title: skillId ? '清空并新建 SOP？' : '清空当前改写？',
        description: skillId
          ? '清空只会进入一个新的 SOP 草稿工作台，不会删除或替换当前正在编辑的 SOP。'
          : '当前技能没有未保存变更，确认清空当前改写内容和对话记录？',
      });
      return;
    }
    setClearConfirmOpen(true);
  }

  function hasUnsavedSkillChanges() {
    const targetDraft = lockNullableSkillIdForDraft(pendingChange?.nextDraft || draft, lockedSkillId);
    if (!targetDraft) return false;
    return hasSkillContentChanges(targetDraft, lastSavedDraft);
  }

  function clearDistillWorkspace() {
    clearAnimationTimers();
    abortRef.current?.abort();
    Object.values(uploadControllersRef.current).forEach((controller) => controller.abort());
    uploadControllersRef.current = {};
    const nextRoute = `/enterprise/skills/distill?mode=create${activeAgentId ? `&agent_id=${encodeURIComponent(activeAgentId)}` : ''}`;
    const nextCacheKey = `skill-distill:${TENANT_ID}:${activeAgentId || 'default'}:create`;
    removeDistillCache(cacheKey);
    removeDistillCache(nextCacheKey);
    setCacheReady(false);
    setHydratedCacheKey('');
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
    if (skillId) {
      navigate(nextRoute, { replace: true });
    } else {
      setHydratedCacheKey(cacheKey);
      setCacheReady(true);
    }
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
    const lockedDraft = lockSkillIdForDraft(nextDraft, lockedSkillId);
    setDraft(lockedDraft);
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
    const snapshotLockedSkillId = snapshot.loadedSkill?.skill_id || lockedSkillId;
    setDraft(lockNullableSkillIdForDraft(snapshot.draft ? cloneSkill(snapshot.draft) : null, snapshotLockedSkillId));
    setLoadedSkill(snapshot.loadedSkill ? cloneSkillRead(snapshot.loadedSkill) : null);
    setLastSavedDraft(lockNullableSkillIdForDraft(snapshot.lastSavedDraft ? cloneSkill(snapshot.lastSavedDraft) : null, snapshotLockedSkillId));
    setSelectedPaths([...snapshot.selectedPaths]);
    setHighlightedPaths([...snapshot.highlightedPaths]);
    setUpdatingPaths([...snapshot.updatingPaths]);
    setDirtyPaths([...snapshot.dirtyPaths]);
    setTextDiffs(snapshot.textDiffs.map((item) => ({ ...item })));
    setPendingChange(lockPendingChangeSkillId(
      snapshot.pendingChange
        ? {
            assistantId: snapshot.pendingChange.assistantId,
            previousDraft: cloneSkill(snapshot.pendingChange.previousDraft),
            nextDraft: cloneSkill(snapshot.pendingChange.nextDraft),
            changedPaths: [...snapshot.pendingChange.changedPaths],
          }
        : null,
      snapshotLockedSkillId,
    ));
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
    setDraft(lockSkillIdForDraft(pendingChange.nextDraft, lockedSkillId));
    setUpdatingPaths([]);
    setTextDiffs([]);
    updateMessage(pendingChange.assistantId, undefined, { actionState: 'confirmed' });
    setPendingChange(null);
    if (showToast) notify.success('已确认改写');
  }

  function rejectPendingChange() {
    if (!pendingChange) return;
    clearAnimationTimers();
    setDraft(lockSkillIdForDraft(pendingChange.previousDraft, lockedSkillId));
    setHighlightedPaths([]);
    setUpdatingPaths([]);
    setTextDiffs([]);
    updateMessage(pendingChange.assistantId, undefined, { actionState: 'rejected' });
    setPendingChange(null);
    notify.info('已拒绝改写并还原');
  }

  function requestEditHistoryMessage(item: ChatItem, index: number) {
    if (loading || item.role !== 'user') return;
    setEditingMessage({ id: item.id, text: visibleChatContent(item) });
  }

  async function copyHistoryMessage(item: ChatItem) {
    const text = visibleChatContent(item);
    try {
      await navigator.clipboard.writeText(text);
      notify.success('已复制');
    } catch {
      notify.error('复制失败');
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
    setRerunConfirm({ index, snapshot, rollbackOperations, text, outgoingText });
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
      const snapshotLockedSkillId = snapshot.loadedSkill?.skill_id || lockedSkillId;
      const confirmedDraft = lockNullableSkillIdForDraft(snapshot.pendingChange?.nextDraft || snapshot.draft, snapshotLockedSkillId);
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
      notify.error(error instanceof Error ? error.message : '回退失败');
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
    const lockedPreviousDraft = lockSkillIdForDraft(previousDraft, lockedSkillId);
    const lockedNextDraft = lockSkillIdForDraft(nextDraft, lockedSkillId);
    const paths = changedPaths;
    if (paths.length === 0) {
      setDraft(lockedNextDraft);
      setHighlightedPaths([]);
      setUpdatingPaths([]);
      setTextDiffs([]);
      return;
    }
    const nextTextDiffs = collectTextDiffs(lockedPreviousDraft, lockedNextDraft, paths);
    setHighlightedPaths(paths);
    setUpdatingPaths(paths);
    setTextDiffs(nextTextDiffs);
    setDraft(lockedPreviousDraft);
    const startTimer = window.setTimeout(() => {
      setTextDiffs((current) => current.map((diff) => ({ ...diff, phase: 'type', progress: 0 })));
      const steps = 24;
      let tick = 0;
      const interval = window.setInterval(() => {
        tick += 1;
        const progress = Math.min(tick / steps, 1);
        setTextDiffs((current) => current.map((diff) => ({ ...diff, phase: 'type', progress })));
        setDraft(typedDraft(lockedPreviousDraft, lockedNextDraft, nextTextDiffs, progress));
        if (progress >= 1) {
          window.clearInterval(interval);
          animationTimersRef.current = animationTimersRef.current.filter((timer) => timer !== interval);
          setTextDiffs((current) => current.map((diff) => ({ ...diff, phase: 'settled', progress: 1 })));
          setDraft(lockedNextDraft);
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

  const pageTitle = mode === 'create' && !skillId ? '新建 SOP' : '编辑 SOP';

  return (
    <div className={DISTILL_PAGE_CLASS}>
      <AppHeader className="shrink-0" onLogout={onLogout} userName={currentUser?.username} title={pageTitle} />
      <div className={DISTILL_ACTIONS_CLASS}>
        <UIButton variant="outline" className={RETURN_BUTTON_CLASS} onClick={() => navigate('/enterprise/skills')}>
          <ArrowLeftOutlined />
          返回
        </UIButton>
      </div>
      <div className={WORKBENCH_CLASS}>
        <DistillSectionCard
          className={cn(CHAT_CARD_CLASS, 'h-full min-h-0', dragActive && CHAT_CARD_DRAGGING_CLASS)}
          bodyClassName={CHAT_CARD_BODY_CLASS}
          title="对话蒸馏"
          onDragEnter={handleDragEnter}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className={CHAT_PANEL_CLASS}>
            {dragActive && <div className={CHAT_UPLOAD_DROP_HINT_CLASS}>松开上传文档</div>}
            <div className={CHAT_MESSAGES_CLASS} ref={chatMessagesRef}>
              {messages.map((item, index) => (
                <div key={item.id} className={chatRowClass(item.role)}>
                  <div
                    className={chatBubbleClass({
                      role: item.role,
                      editing: editingMessage?.id === item.id,
                      hasAttachments: item.role === 'user' && Boolean(item.attachments?.length),
                    })}
                  >
                    {item.role === 'assistant' && item.thinking && (
                      <div className={CHAT_THINKING_BLOCK_CLASS}>
                        <button
                          type="button"
                          className={CHAT_THINKING_BUTTON_CLASS}
                          onClick={() => toggleThinking(item.id)}
                        >
                          {item.thinking === 'running' ? <LoadingOutlined /> : <CheckOutlined />}
                          <span>{item.thinking === 'running' ? '正在学习' : '学习记录'}</span>
                          {item.thinkingOpen ? <DownOutlined /> : <RightOutlined />}
                        </button>
                        {item.thinkingOpen && (
                          <div className={CHAT_THINKING_DETAILS_CLASS}>
                            {(item.thinkingDetails || []).map((detail, index) => (
                              <div key={`${item.id}_detail_${index}`} className={CHAT_THINKING_DETAIL_CLASS}>
                                {detail}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                    {item.role === 'user' && item.attachments && item.attachments.length > 0 && (
                      <div className={cn(CHAT_ATTACHMENTS_CLASS, CHAT_ATTACHMENTS_USER_CLASS)}>
                        {item.attachments.map((attachment) => (
                          <div className={cn(CHAT_ATTACHMENT_CLASS, CHAT_ATTACHMENT_USER_CLASS)} key={attachment.id} title={attachment.name}>
                            <span className={CHAT_ATTACHMENT_ICON_CLASS}>
                              <FileTextOutlined />
                            </span>
                            <span className={CHAT_ATTACHMENT_MAIN_CLASS}>
                              <span className={CHAT_ATTACHMENT_NAME_CLASS}>{attachment.name}</span>
                              <span className={CHAT_ATTACHMENT_TYPE_CLASS}>{attachment.type}</span>
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    {item.role === 'user' && editingMessage?.id === item.id ? (
                      <div
                        className={cn(
                          CHAT_EDIT_PANEL_CLASS,
                          item.attachments?.length ? CHAT_EDIT_PANEL_USER_ATTACHMENTS_CLASS : undefined,
                        )}
                      >
                        <Textarea
                          className={CHAT_EDIT_TEXTAREA_CLASS}
                          value={editingMessage.text}
                          rows={3}
                          autoFocus
                          onChange={(event) => setEditingMessage({ id: item.id, text: event.target.value })}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                              event.preventDefault();
                              submitEditingMessage();
                            }
                          }}
                        />
                        <div className={CHAT_EDIT_ACTIONS_CLASS}>
                          <UIButton variant="outline" onClick={cancelEditingMessage}>取消</UIButton>
                          <UIButton onClick={submitEditingMessage} disabled={!(editingMessage?.text || '').trim()}>
                            发送
                          </UIButton>
                        </div>
                      </div>
                    ) : (
                      <>
                        {item.content ? (
                          <div
                            className={cn(
                              CHAT_CONTENT_CLASS,
                              item.role === 'user' && item.attachments?.length ? CHAT_CONTENT_USER_ATTACHMENTS_CLASS : undefined,
                            )}
                          >
                            {visibleChatContent(item)}
                          </div>
                        ) : item.role === 'assistant' && item.thinking === 'running' ? null : item.role === 'assistant' ? (
                          '正在处理...'
                        ) : null}
                        {item.role === 'user' && (
                          <div className={CHAT_HOVER_ACTIONS_CLASS}>
                            <span className={CHAT_TIME_CLASS}>{formatMessageTime(item.createdAt)}</span>
                            <button type="button" className={CHAT_HOVER_BUTTON_CLASS} title="复制" onClick={() => void copyHistoryMessage(item)}>
                              <CopyGlyph />
                            </button>
                            <button
                              type="button"
                              className={CHAT_HOVER_BUTTON_CLASS}
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
                        <div className={CHAT_WARNING_CLASS}>
                          <div className={CHAT_WARNING_TITLE_CLASS}>
                            <WarningOutlined />
                            <span>提示</span>
                          </div>
                          {warnings.map((warning, index) => (
                            <div key={`${item.id}_warning_${index}`} className={CHAT_WARNING_ITEM_CLASS} title={warning.title}>
                              {warning.text}
                            </div>
                          ))}
                        </div>
                      );
                    })()}
                    {item.toolSuggestions && item.toolSuggestions.length > 0 && (
                      <div className={TOOL_SUGGESTIONS_CLASS}>
                        {item.toolSuggestions.map((suggestion) => {
                          const canResolveSuggestion =
                            toolSuggestionResolution(suggestion) === 'new_candidate' &&
                            suggestion.status !== 'accepted' &&
                            suggestion.status !== 'created' &&
                            suggestion.status !== 'rejected';
                          return (
                            <div className={TOOL_SUGGESTION_CLASS} key={`${item.id}_${suggestion.name}`}>
                              <div className={TOOL_SUGGESTION_MAIN_CLASS}>
                                <div className={TOOL_SUGGESTION_HEAD_CLASS}>
                                  <div className={TOOL_SUGGESTION_TITLE_CLASS}>{toolSuggestionTitle(suggestion)}</div>
                                  <span className={toolStatusBadgeClass(toolSuggestionStatusClass(suggestion))}>
                                    {toolSuggestionStatusText(suggestion)}
                                  </span>
                                </div>
                                <div className={TOOL_SUGGESTION_DESC_CLASS}>
                                  {suggestion.reason || suggestion.description || suggestion.name}
                                </div>
                                <div className={TOOL_SUGGESTION_META_CLASS}>
                                  <span className={TOOL_METHOD_CLASS}>{suggestion.method || 'POST'}</span>
                                  <span>{suggestion.url || '-'}</span>
                                </div>
                              </div>
                              <div className={TOOL_SUGGESTION_ACTIONS_CLASS}>
                                <span className={cn(TOOL_ACTION_GROUP_CLASS, TOOL_ACTION_GROUP_DETAIL_CLASS)}>
                                  <SimpleTooltip title="查看详情">
                                    <UIButton
                                      className={TOOL_ACTION_BUTTON_CLASS}
                                      variant="ghost"
                                      size="icon"
                                      onClick={() => openToolDetail(item.id, suggestion)}
                                    >
                                      <InfoCircleOutlined />
                                    </UIButton>
                                  </SimpleTooltip>
                                </span>
                                {canResolveSuggestion && (
                                  <span className={TOOL_ACTION_GROUP_CLASS}>
                                    <SimpleTooltip title="确认新增">
                                      <UIButton
                                        className={cn(TOOL_ACTION_BUTTON_CLASS, TOOL_ACTION_CONFIRM_CLASS)}
                                        variant="ghost"
                                        size="icon"
                                        disabled={!suggestion.probe_result?.success}
                                        onClick={() => void confirmToolSuggestion(item.id, suggestion)}
                                      >
                                        <CheckCircleOutlined />
                                      </UIButton>
                                    </SimpleTooltip>
                                    <SimpleTooltip title="拒绝">
                                      <UIButton
                                        className={cn(TOOL_ACTION_BUTTON_CLASS, TOOL_ACTION_REJECT_CLASS)}
                                        variant="ghost"
                                        size="icon"
                                        onClick={() => rejectToolSuggestion(item.id, suggestion.name)}
                                      >
                                        <CloseCircleOutlined />
                                      </UIButton>
                                    </SimpleTooltip>
                                  </span>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                    {item.actionState === 'pending' && (
                      <div className={CHAT_CONFIRM_CLASS}>
                        <UIButton size="sm" onClick={() => confirmPendingChange()}>
                          确认
                        </UIButton>
                        <UIButton size="sm" variant="outline" onClick={rejectPendingChange}>
                          拒绝
                        </UIButton>
                      </div>
                    )}
                    {item.actionState === 'confirmed' && <div className={CHAT_DECISION_CLASS}>已确认</div>}
                    {item.actionState === 'rejected' && <div className={CHAT_DECISION_CLASS}>已拒绝</div>}
                  </div>
                </div>
              ))}
            </div>
            <div className={CHAT_COMPOSER_SHELL_CLASS}>
              <div className={CHAT_COMPOSER_CLASS}>
              {attachments.length > 0 && (
                <div className={UPLOAD_LIST_CLASS}>
                  {attachments.map((attachment) => (
                    <div className={uploadItemClass(attachment.status)} key={attachment.id}>
                      <FileTextOutlined />
                      <span className={UPLOAD_NAME_CLASS}>{attachment.name}</span>
                      <span className={UPLOAD_STATUS_CLASS}>
                        {attachment.status === 'uploading' && '读取中'}
                        {attachment.status === 'ready' && '已读取'}
                        {attachment.status === 'error' && (attachment.error || '读取失败')}
                      </span>
                      <UIButton
                        size="icon"
                        variant="ghost"
                        onClick={() => cancelAttachment(attachment.id)}
                      >
                        <CloseOutlined />
                      </UIButton>
                    </div>
                  ))}
                </div>
              )}
              <Textarea
                className={CHAT_TEXTAREA_CLASS}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onPaste={handleComposerPaste}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void send();
                  }
                }}
                rows={4}
                placeholder={
                  draft
                    ? '说明你要如何改写右侧选中的部分'
                    : '输入或粘贴需要整理的 SOP 流程说明'
                }
              />
              <div className={CHAT_ACTIONS_CLASS}>
                <span className="min-w-0 truncate text-[12px] text-[#858b9c]">{streamStatus}</span>
                <div className={CHAT_ACTIONS_GROUP_CLASS}>
                  <label>
                    <input
                      type="file"
                      accept=".md,.txt,.doc,.docx"
                      multiple
                      className="hidden"
                      disabled={loading}
                      onChange={(event) => {
                        const files = event.target.files ? Array.from(event.target.files) : [];
                        files.forEach((file) => void stageFileUpload(file));
                        event.target.value = '';
                      }}
                    />
                    <UIButton asChild variant="outline" disabled={uploadingFile || loading} className={CARD_OUTLINE_BUTTON_CLASS}>
                      <span>
                        <UploadOutlined />
                        上传文件
                      </span>
                    </UIButton>
                  </label>
                  {loading && (
                    <UIButton variant="outline" className={CARD_OUTLINE_BUTTON_CLASS} onClick={stopStream}>
                      <StopOutlined />
                      停止
                    </UIButton>
                  )}
                  <ModelConfigDropdown
                    models={modelConfigs}
                    value={selectedRewriteModelId}
                    onChange={(modelId) => {
                      setSelectedRewriteModelId(modelId);
                      window.localStorage.setItem(`${DISTILL_REWRITE_MODEL_STORAGE_KEY}:${TENANT_ID}`, modelId);
                    }}
                    buttonClassName={REWRITE_MODEL_BUTTON_CLASS}
                  />
                  <UIButton
                    disabled={loading || uploadingFile || (!input.trim() && readyAttachments.length === 0)}
                    className={PRIMARY_BUTTON_CLASS}
                    onClick={() => void send()}
                  >
                    {loading ? <LoadingOutlined className="animate-spin" /> : <SendOutlined />}
                    发送
                  </UIButton>
                </div>
              </div>
              </div>
            </div>
          </div>
        </DistillSectionCard>
        <DistillSectionCard
          className={cn(SOURCE_CARD_CLASS, 'h-full min-h-0')}
          bodyClassName={DISTILL_CARD_BODY_CLASS}
          title={viewMode === 'source' ? '源码' : '流程图'}
          extra={
            <div className="flex flex-wrap justify-end gap-[8px]">
              <UIButton variant="outline" className={CARD_OUTLINE_BUTTON_CLASS} disabled={loading} onClick={handleClearClick}>
                清空
              </UIButton>
              <SimpleTooltip title={draft && !hasSaveableDraftChanges ? '当前没有内容变化' : ''}>
                <UIButton
                  variant="outline"
                  className={CARD_OUTLINE_BUTTON_CLASS}
                  disabled={!draft || loading || !hasSaveableDraftChanges}
                  onClick={() => openSaveReview()}
                >
                  <SaveOutlined />
                  保存草稿
                </UIButton>
              </SimpleTooltip>
            </div>
          }
        >
          <div className={SOURCE_TOOLBAR_CLASS}>
            <div className="flex flex-wrap items-center gap-[8px]">
              <UIButton
                variant="outline"
                className={CARD_OUTLINE_BUTTON_CLASS}
                onClick={() => setViewMode(viewMode === 'source' ? 'flow' : 'source')}
              >
                {viewMode === 'source' ? <BranchesOutlined /> : <CodeOutlined />}
                {viewMode === 'source' ? '显示流程' : '显示源码'}
              </UIButton>
              <UIButton variant="outline" className={CARD_OUTLINE_BUTTON_CLASS} disabled={!draft} onClick={toggleAllTargets}>
                {allSelected ? '清空选择' : '全选'}
              </UIButton>
            </div>
          </div>
          {!draft ? (
            <div className={SOURCE_EMPTY_STATE_CLASS}>
              <FileTextOutlined className="text-[28px] text-[#c0c6d4]" />
              <p className={SOURCE_EMPTY_TEXT_CLASS}>暂无 SOP 草稿</p>
              <p className="text-[12px] leading-[18px] text-[#c0c6d4]">在左侧输入说明或上传文档后开始生成</p>
            </div>
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
              lockSkillId={Boolean(lockedSkillId)}
              onToggle={toggleTarget}
              onEdit={handleSourceEdit}
            />
          ) : (
            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
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
            </div>
          )}
        </DistillSectionCard>
      </div>
      <KDialog
        open={clearConfirmOpen}
        onOpenChange={(open) => !open && setClearConfirmOpen(false)}
        title="清空前是否保存？"
        width={520}
        footer={
          <div className="flex flex-wrap justify-end gap-[8px]">
            <UIButton variant="outline" onClick={() => setClearConfirmOpen(false)}>取消</UIButton>
            <UIButton
              variant="outline"
              onClick={() => {
                setClearConfirmOpen(false);
                clearDistillWorkspace();
              }}
            >
              不保存清空
            </UIButton>
            <UIButton
              onClick={() => {
                setClearConfirmOpen(false);
                openSaveReview({ clearAfterSave: true });
              }}
            >
              保存并清空
            </UIButton>
          </div>
        }
      >
        <p className="m-0 text-[14px] leading-[22px] text-foreground">
          检测到当前 SOP 有未保存变更。你可以先保存当前内容；清空后会进入新的 SOP 草稿工作台，不会把原 SOP 替换为空。
        </p>
      </KDialog>
      <KDialog
        open={saveReviewOpen}
        onOpenChange={(open) => !open && closeSaveReview()}
        title="保存SOP版本"
        width={820}
        footer={
          <div className="flex flex-wrap justify-end gap-[8px]">
            <UIButton variant="outline" onClick={closeSaveReview}>取消</UIButton>
            <UIButton disabled={!saveReviewHasContentChanges} onClick={() => void saveDraft()}>保存</UIButton>
          </div>
        }
      >
        <div className={SAVE_REVIEW_FORM_CLASS}>
          <label className={SAVE_REVIEW_FORM_LABEL_CLASS}>
            <span>SOP名称</span>
            <Input value={saveName} onChange={(event) => setSaveName(event.target.value)} />
          </label>
          <label className={SAVE_REVIEW_FORM_LABEL_CLASS}>
            <span>业务域</span>
            <Input value={saveDomain} onChange={(event) => setSaveDomain(event.target.value)} />
          </label>
          <label className={SAVE_REVIEW_FORM_LABEL_CLASS}>
            <span>版本号</span>
            <Input value={saveVersion} disabled={!saveReviewHasContentChanges} onChange={(event) => setSaveVersion(event.target.value)} />
          </label>
        </div>
        <div className={SAVE_REVIEW_DIFF_CLASS}>
          <strong className="text-[13px] font-semibold text-foreground">本轮修改 diff</strong>
          {saveReviewDiffs.length === 0 ? (
            <EmptyState description="暂无结构差异" />
          ) : (
            saveReviewDiffs.map((diff) => (
              <div key={diff.key} className={SAVE_REVIEW_DIFF_ROW_CLASS}>
                <div className={SAVE_REVIEW_DIFF_PATH_CLASS}>{diffTargetLabel(diff.path, saveReviewDraft)} / {fieldLabel(diff.field)}</div>
                <SaveReviewDiffValue diff={diff} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
              </div>
            ))
          )}
        </div>
      </KDialog>
      <KDialog
        open={Boolean(toolDetail)}
        onOpenChange={(open) => !open && setToolDetail(null)}
        title="工具详情"
        width={1040}
        footer={
          <div className={cn(TOOL_SUGGESTION_DETAIL_FOOTER_CLASS, "flex flex-wrap justify-end gap-[8px]")}>
            <UIButton variant="outline" onClick={() => setToolDetail(null)}>关闭</UIButton>
            {toolDetail && toolSuggestionResolution(toolDetail) === 'new_candidate' && (
              <>
                <UIButton variant="outline" onClick={applyProbeArgumentsFromDetail}>应用样例参数</UIButton>
                <UIButton
                  disabled={toolDetail?.probeStatus === 'probing'}
                  onClick={probeToolDetail}
                >
                  {toolDetail?.probeStatus === 'probing' ? <LoadingOutlined className="animate-spin" /> : <ApiOutlined />}
                  {toolDetail?.probe_result ? '再次测试' : '测试接口'}
                </UIButton>
              </>
            )}
          </div>
        }
      >
        {toolDetail && (
          <div className={TOOL_SUGGESTION_DETAIL_CLASS}>
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
            <strong className="text-[13px] font-semibold text-foreground">样例参数</strong>
            <Textarea
              value={probeArgsText}
              rows={5}
              onChange={(event) => setProbeArgsText(event.target.value)}
            />
            <strong className="text-[13px] font-semibold text-foreground">输入 Schema</strong>
            <pre className={TOOL_SUGGESTION_DETAIL_PRE_CLASS}>{JSON.stringify(toolDetail.input_schema || {}, null, 2)}</pre>
            <strong className="text-[13px] font-semibold text-foreground">输出 Schema</strong>
            <pre className={TOOL_SUGGESTION_DETAIL_PRE_CLASS}>{JSON.stringify(toolDetail.output_schema || {}, null, 2)}</pre>
            {toolDetail.probe_result && (
              <>
                <strong className="text-[13px] font-semibold text-foreground">测试结果</strong>
                <pre className={TOOL_SUGGESTION_DETAIL_PRE_CLASS}>{JSON.stringify(toolDetail.probe_result, null, 2)}</pre>
              </>
            )}
          </div>
        )}
      </KDialog>
      {clearNewConfirm && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setClearNewConfirm(null)}
          title={clearNewConfirm.title}
          description={clearNewConfirm.description}
          confirmText="清空"
          destructive={false}
          onConfirm={() => {
            setClearNewConfirm(null);
            clearDistillWorkspace();
          }}
        />
      )}
      {rerunConfirm && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setRerunConfirm(null)}
          title="重新编辑这条消息？"
          confirmText="确认回退"
          destructive={false}
          description={
            <div>
              <p className="m-0 mb-[8px]">重新编辑会回到这条消息发送前的 SOP 草稿，并截断之后的推理记录。</p>
              <div className="rollback-operation-list flex flex-wrap gap-[6px]">
                {rerunConfirm.rollbackOperations.map((operation, operationIndex) => (
                  <DistillTag key={`${operation.kind}_${operationIndex}`}>{operation.label}</DistillTag>
                ))}
              </div>
            </div>
          }
          onConfirm={() => {
            const payload = rerunConfirm;
            setRerunConfirm(null);
            void rerunEditedMessage(
              payload.index,
              payload.snapshot,
              payload.rollbackOperations,
              payload.text,
              payload.outgoingText,
            );
          }}
        />
      )}
    </div>
  );
}

function DistillSectionCard({
  className,
  bodyClassName,
  title,
  extra,
  children,
  ...rest
}: {
  className?: string;
  bodyClassName?: string;
  title?: ReactNode;
  extra?: ReactNode;
  children?: ReactNode;
} & Omit<HTMLAttributes<HTMLDivElement>, 'title'>) {
  return (
    <section className={cn(DISTILL_CARD_CLASS, 'h-full min-h-0', className)} {...rest}>
      {(title || extra) && (
        <div className={DISTILL_CARD_HEADER_CLASS}>
          <div className={cn('min-w-0', SECTION_CARD_TITLE_CLASS)}>{title}</div>
          {extra ? <div className="shrink-0">{extra}</div> : null}
        </div>
      )}
      <div className={cn(DISTILL_CARD_BODY_CLASS, bodyClassName)}>{children}</div>
    </section>
  );
}

function KDialog({
  open,
  onOpenChange,
  title,
  width = 520,
  footer,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: ReactNode;
  width?: number;
  footer?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85vh] gap-0 overflow-y-auto rounded-[16px] p-0"
        style={{ width: `min(${width}px, calc(100vw - 32px))`, maxWidth: 'calc(100vw - 32px)' }}
      >
        {title != null && (
          <DialogTitle className="border-b border-border px-[24px] py-[16px] text-[16px] font-semibold text-foreground">
            {title}
          </DialogTitle>
        )}
        <div className="px-[24px] py-[20px]">{children}</div>
        {footer != null && (
          <DialogFooter>{footer}</DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

function SimpleTooltip({ title, children }: { title?: ReactNode; children: ReactNode }) {
  if (!title) return <>{children}</>;
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent>{title}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function EmptyState({ description }: { description: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-[8px] py-[32px] text-center text-[13px] text-[#858b9c]">
      {description}
    </div>
  );
}

function DistillTag({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-[6px] bg-[#f2f3f5] px-[8px] py-px text-[12px] font-medium leading-[18px] text-[#5b6273]">
      {children}
    </span>
  );
}

/**
 * Inline text input for the SOP source editor.
 */
function SourceInput({
  className,
  ...rest
}: {
  className?: string;
  value?: string;
  placeholder?: string;
  disabled?: boolean;
  readOnly?: boolean;
  style?: CSSProperties;
  onChange?: ChangeEventHandler<HTMLInputElement>;
}) {
  return <Input className={cn(SOURCE_INPUT_CLASS, className)} {...rest} />;
}

/** Auto-growing textarea replacement for Ant Design's `Input.TextArea autoSize`. */
function AutoGrowTextarea({
  className,
  minRows = 1,
  value,
  ...rest
}: {
  className?: string;
  minRows?: number;
  value?: string;
  placeholder?: string;
  disabled?: boolean;
  readOnly?: boolean;
  style?: CSSProperties;
  onChange?: ChangeEventHandler<HTMLTextAreaElement>;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);
  return (
    <Textarea
      ref={ref}
      rows={minRows}
      value={value}
      className={cn(SOURCE_INPUT_CLASS, className)}
      {...rest}
    />
  );
}

/** Native number input replacement for Ant Design's `InputNumber`. */
function SourceNumberInput({
  className,
  value,
  min,
  placeholder,
  onChange,
}: {
  className?: string;
  value: number | null;
  min?: number;
  placeholder?: string;
  onChange: (value: number | null) => void;
}) {
  return (
    <Input
      type="number"
      className={cn(SOURCE_INPUT_CLASS, className)}
      value={value ?? ''}
      min={min}
      placeholder={placeholder}
      onChange={(event) => {
        const raw = event.target.value;
        onChange(raw === '' ? null : Number(raw));
      }}
    />
  );
}

/**
 * Searchable action picker (replaces Ant Design's `Select showSearch`). Renders
 * a filterable input backed by a popover list. Committing an empty value removes
 * the action, matching the previous `allowClear` behaviour.
 */
function ActionCombobox({
  value,
  options,
  placeholder = '选择一个动作',
  onSelect,
}: {
  value?: string;
  options: SelectOption[];
  placeholder?: string;
  onSelect: (value: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const [query, setQuery] = useState('');
  const normalizedQuery = query.trim().toLowerCase();
  const filtered = normalizedQuery
    ? options.filter(
        (option) =>
          option.label.toLowerCase().includes(normalizedQuery) ||
          String(option.value).toLowerCase().includes(normalizedQuery),
      )
    : options;
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) onSelect(value || '');
      }}
    >
      <PopoverTrigger asChild>
        <input
          autoComplete="off"
          data-1p-ignore="true"
          data-lpignore="true"
          data-bwignore="true"
          className={SOURCE_ACTION_SELECT_CLASS}
          autoFocus
          value={query}
          placeholder={placeholder}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              if (filtered.length > 0) onSelect(String(filtered[0].value));
            } else if (event.key === 'Escape') {
              event.preventDefault();
              event.stopPropagation();
            }
          }}
        />
      </PopoverTrigger>
      <PopoverContent
        align="start"
        onOpenAutoFocus={(event) => event.preventDefault()}
        className="max-h-[280px] w-[320px] overflow-y-auto p-[4px]"
      >
        {filtered.length === 0 ? (
          <div className="px-[10px] py-[12px] text-center text-[13px] text-[#858b9c]">无匹配动作</div>
        ) : (
          filtered.map((option) => (
            <button
              key={String(option.value)}
              type="button"
              className={cn(
                'flex w-full items-center rounded-[8px] px-[10px] py-[6px] text-left text-[13px] text-foreground hover:bg-muted',
                option.value === value && 'bg-muted',
              )}
              onClick={() => onSelect(String(option.value))}
            >
              {option.label}
            </button>
          ))
        )}
      </PopoverContent>
    </Popover>
  );
}

/** shadcn Select styled for the SOP source editor. */
function SourceSelect({
  className,
  value,
  options,
  placeholder,
  onChange,
}: {
  className?: string;
  value?: string;
  options: SelectOption[];
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  return (
    <UISelect value={value || undefined} onValueChange={onChange}>
      <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={String(option.value)} value={String(option.value)}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </UISelect>
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
  lockSkillId,
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
  lockSkillId?: boolean;
  onToggle: (target: TargetSelection) => void;
  onEdit: (nextDraft: SkillCard, path: string) => void;
}) {
  const [deleteNodeIndex, setDeleteNodeIndex] = useState<number | null>(null);

  function editBasic(field: keyof SkillCard, value: string | string[]) {
    if (field === 'skill_id' && lockSkillId) return;
    const next = cloneSkill(skill);
    if (field === 'trigger_intents' || field === 'user_utterance_examples' || field === 'goal' || field === 'required_info' || field === 'response_rules') {
      next[field] = Array.isArray(value) ? value : splitEditableList(value);
    } else if (field === 'skill_id' || field === 'name' || field === 'version' || field === 'business_domain' || field === 'description') {
      next[field] = String(value);
    }
    onEdit(next, 'basic');
  }

  function editStep(index: number, field: string, value: string | string[] | boolean | Record<string, unknown>) {
    const next = cloneSkill(skill);
    const listValue = field === 'expected_user_info' || field === 'allowed_actions'
      ? Array.isArray(value)
        ? value
        : splitEditableList(String(value))
      : value;
    next.nodes = Array.isArray(next.nodes) ? [...next.nodes] : [];
    const currentNode = { ...(next.nodes[index] || {}) };
    const nodeField = field === 'step_id' ? 'node_id' : field;
    if (nodeField === 'node_id') {
      const previousId = String(currentNode.node_id || currentNode.step_id || `node_${index + 1}`);
      const nextId = String(listValue || '').trim();
      if (!nextId) {
        notify.warning('节点 ID 不能为空');
        return;
      }
      const duplicated = next.nodes.some((node, nodeIndex) => (
        nodeIndex !== index && String(node?.node_id || node?.step_id || '') === nextId
      ));
      if (duplicated) {
        notify.warning(`节点 ID「${nextId}」已经存在`);
        return;
      }
      currentNode.node_id = nextId;
      next.edges = normalizeSkillEdges(next).map((edge) => ({
        ...edge,
        source_node_id: String(edge.source_node_id || '') === previousId ? nextId : edge.source_node_id,
        next_node_id: String(edge.next_node_id || '') === previousId ? nextId : edge.next_node_id,
      }));
      if (next.start_node_id === previousId) next.start_node_id = nextId;
      next.terminal_node_ids = asStringList(next.terminal_node_ids).map((nodeId) => (nodeId === previousId ? nextId : nodeId));
      next.nodes[index] = currentNode;
      onEdit(next, stepTargetPath(index));
      return;
    }
    currentNode[nodeField] = listValue;
    next.nodes[index] = currentNode;
    onEdit(next, stepTargetPath(index));
  }

  function updateEdge(index: number, edgeIndex: number, patch: Record<string, unknown>) {
    const next = cloneSkill(skill);
    const sourceId = nodeIdAt(next, index);
    const edges = normalizeSkillEdges(next);
    const globalIndex = findSourceEdgeIndex(edges, sourceId, edgeIndex);
    if (globalIndex < 0) return;
    edges[globalIndex] = { ...edges[globalIndex], ...patch };
    next.edges = edges;
    onEdit(next, stepTargetPath(index));
  }

  function addEdge(index: number) {
    const next = cloneSkill(skill);
    const sourceId = nodeIdAt(next, index);
    const nodes = normalizeSkillNodes(next);
    const fallbackTarget = nodes.find((node) => String(node.node_id || node.step_id || '') !== sourceId);
    const targetId = String(nodes[index + 1]?.node_id || nodes[index + 1]?.step_id || fallbackTarget?.node_id || fallbackTarget?.step_id || '');
    const sourceEdges = normalizeSkillEdges(next).filter((edge) => String(edge.source_node_id || '') === sourceId);
    const priority = sourceEdges.length > 0
      ? Math.max(...sourceEdges.map((edge, sourceIndex) => edgePriority(edge, sourceIndex))) + 1
      : 1;
    next.edges = [
      ...normalizeSkillEdges(next),
      {
        source_node_id: sourceId,
        next_node_id: targetId,
        condition: '',
        priority,
        label: targetId ? '新增流转' : '',
      },
    ];
    onEdit(next, stepTargetPath(index));
  }

  function deleteEdge(index: number, edgeIndex: number) {
    const next = cloneSkill(skill);
    const sourceId = nodeIdAt(next, index);
    const edges = normalizeSkillEdges(next);
    const globalIndex = findSourceEdgeIndex(edges, sourceId, edgeIndex);
    if (globalIndex < 0) return;
    edges.splice(globalIndex, 1);
    next.edges = edges;
    onEdit(next, stepTargetPath(index));
  }

  function insertNodeBetween(index: number) {
    const next = cloneSkill(skill);
    const nodes = normalizeSkillNodes(next);
    const insertAt = nodes.length === 0 ? 0 : Math.max(0, Math.min(index + 1, nodes.length));
    const sourceNode = insertAt > 0 ? nodes[insertAt - 1] : null;
    const targetNode = insertAt < nodes.length ? nodes[insertAt] : null;
    const sourceId = sourceNode ? String(sourceNode.node_id || sourceNode.step_id || `node_${insertAt}`) : '';
    const targetId = targetNode ? String(targetNode.node_id || targetNode.step_id || `node_${insertAt + 1}`) : '';
    const newNodeId = uniqueNodeId(nodes, `node_${insertAt + 1}`);
    const newNode = {
      node_id: newNodeId,
      type: 'collect_info',
      name: '新增节点',
      instruction: '说明这个节点要完成的目标。',
      optional: false,
      condition: '',
      expected_user_info: [],
      allowed_actions: ['continue_flow'],
      knowledge_scope: {},
      retry_policy: {},
      metadata: {},
    };
    nodes.splice(insertAt, 0, newNode);
    next.nodes = nodes;

    const edges = normalizeSkillEdges(next);
    if (sourceId && targetId) {
      const directEdgeIndexes = edges
        .map((edge, edgeIndex) => ({ edge, edgeIndex }))
        .filter(({ edge }) => String(edge.source_node_id || '') === sourceId && String(edge.next_node_id || '') === targetId)
        .map(({ edgeIndex }) => edgeIndex);
      if (directEdgeIndexes.length > 0) {
        directEdgeIndexes.forEach((edgeIndex) => {
          edges[edgeIndex] = {
            ...edges[edgeIndex],
            next_node_id: newNodeId,
            label: String(edges[edgeIndex].label || '').trim() || '进入新增节点',
          };
        });
        const maxPriority = Math.max(...directEdgeIndexes.map((edgeIndex, localIndex) => edgePriority(edges[edgeIndex], localIndex)));
        edges.push({
          source_node_id: newNodeId,
          next_node_id: targetId,
          condition: '',
          priority: maxPriority + 1,
          label: `继续到 ${String(targetNode?.name || targetId)}`,
        });
      } else {
        const sourcePriority = edges
          .filter((edge) => String(edge.source_node_id || '') === sourceId)
          .reduce((max, edge, sourceIndex) => Math.max(max, edgePriority(edge, sourceIndex)), 0) + 1;
        edges.push({
          source_node_id: sourceId,
          next_node_id: newNodeId,
          condition: '',
          priority: sourcePriority,
          label: '进入新增节点',
        });
        edges.push({
          source_node_id: newNodeId,
          next_node_id: targetId,
          condition: '',
          priority: 1,
          label: `继续到 ${String(targetNode?.name || targetId)}`,
        });
      }
    } else if (!sourceId && targetId) {
      edges.push({
        source_node_id: newNodeId,
        next_node_id: targetId,
        condition: '',
        priority: 1,
        label: `继续到 ${String(targetNode?.name || targetId)}`,
      });
      next.start_node_id = newNodeId;
    } else if (sourceId && !targetId) {
      const sourcePriority = edges
        .filter((edge) => String(edge.source_node_id || '') === sourceId)
        .reduce((max, edge, sourceIndex) => Math.max(max, edgePriority(edge, sourceIndex)), 0) + 1;
      edges.push({
        source_node_id: sourceId,
        next_node_id: newNodeId,
        condition: '',
        priority: sourcePriority,
        label: '进入新增节点',
      });
      const previousTerminalIds = asStringList(next.terminal_node_ids);
      next.terminal_node_ids = previousTerminalIds.length > 0
        ? [...previousTerminalIds.filter((terminalId) => terminalId !== sourceId), newNodeId]
        : [newNodeId];
    } else {
      next.start_node_id = newNodeId;
      next.terminal_node_ids = [newNodeId];
    }
    next.edges = edges;
    if (!next.start_node_id) next.start_node_id = String(nodes[0]?.node_id || '');
    if (asStringList(next.terminal_node_ids).length === 0 && nodes.length > 0) {
      next.terminal_node_ids = [String(nodes[nodes.length - 1].node_id || '')];
    }
    onEdit(next, stepTargetPath(insertAt));
  }

  function confirmDeleteNode(index: number) {
    const nodes = normalizeSkillNodes(skill);
    if (nodes.length <= 1) {
      notify.warning('至少需要保留一个节点');
      return;
    }
    setDeleteNodeIndex(index);
  }

  function runDeleteNode(index: number) {
    const nodes = normalizeSkillNodes(skill);
    const node = nodes[index];
    if (!node) return;
    const nodeId = String(node.node_id || node.step_id || `node_${index + 1}`);
    const next = cloneSkill(skill);
    const nextNodes = normalizeSkillNodes(next).filter((_node, nodeIndex) => nodeIndex !== index);
    next.nodes = nextNodes;
    next.edges = normalizeSkillEdges(next).filter((edge) => (
      String(edge.source_node_id || '') !== nodeId && String(edge.next_node_id || '') !== nodeId
    ));
    if (next.start_node_id === nodeId) next.start_node_id = String(nextNodes[0]?.node_id || '');
    next.terminal_node_ids = asStringList(next.terminal_node_ids).filter((terminalId) => terminalId !== nodeId);
    if (next.terminal_node_ids.length === 0 && nextNodes.length > 0) {
      next.terminal_node_ids = [String(nextNodes[nextNodes.length - 1].node_id || '')];
    }
    onEdit(next, stepTargetPath(Math.min(index, Math.max(nextNodes.length - 1, 0))));
  }

  function renderDeleteNodeConfirm() {
    if (deleteNodeIndex === null) return null;
    const nodes = normalizeSkillNodes(skill);
    const index = deleteNodeIndex;
    const node = nodes[index];
    if (!node) return null;
    const nodeId = String(node.node_id || node.step_id || `node_${index + 1}`);
    const edges = normalizeSkillEdges(skill);
    const incomingEdges = edges.filter((edge) => String(edge.next_node_id || '') === nodeId);
    const outgoingEdges = edges.filter((edge) => String(edge.source_node_id || '') === nodeId);
    const affected = [...incomingEdges, ...outgoingEdges];
    return (
      <ConfirmDialog
        open
        onOpenChange={(open) => !open && setDeleteNodeIndex(null)}
        title={`确认删除 Node ${index + 1}：${String(node.name || nodeId)}？`}
        confirmText="确认删除"
        description={
          <div className={NODE_DELETE_CONFIRM_CLASS}>
            <p>删除后会同时移除所有连接到这个节点、或从这个节点发出的流转规则。</p>
            <strong>将受影响的连接</strong>
            <ul>
              {affected.length > 0 ? (
                affected.map((edge, edgeIndex) => (
                  <li key={`${String(edge.source_node_id)}_${String(edge.next_node_id)}_${edgeIndex}`}>
                    {nodeDisplayNameById(nodes, String(edge.source_node_id || ''))}
                    {' -> '}
                    {nodeDisplayNameById(nodes, String(edge.next_node_id || ''))}
                    {String(edge.label || edge.condition || '').trim() ? `：${String(edge.label || edge.condition)}` : ''}
                  </li>
                ))
              ) : (
                <li>无直接连接关系</li>
              )}
            </ul>
          </div>
        }
        onConfirm={() => {
          runDeleteNode(index);
          setDeleteNodeIndex(null);
        }}
      />
    );
  }

  const steps = skillGraphSteps(skill);
  const nodeNameMap = steps.reduce<Record<string, string>>((acc, step, index) => {
    const nodeId = String(step.node_id || step.step_id || `node_${index + 1}`);
    acc[nodeId] = String(step.name || nodeId);
    return acc;
  }, {});
  const edgeMap = skillGraphEdgeMap(skill);
  const terminalNodeIds = new Set(asStringList(skill.terminal_node_ids));
  const startNodeId = String(skill.start_node_id || '');
  const nodeOptions = steps.map((step, index) => {
    const nodeId = String(step.node_id || step.step_id || `node_${index + 1}`);
    return {
      value: nodeId,
      label: `Node ${index + 1} · ${String(step.name || nodeId)}`,
    };
  });
  const actionOptions = buildActionOptions(toolDescriptions, toolStatuses, steps);

  return (
    <div className={SOURCE_MD_CLASS} ref={containerRef}>
      <div className={SOURCE_GROUP_TITLE_CLASS}>基础信息</div>
      <SelectableTarget
        className={distillSourceSectionClass('basic', selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
        target={{ path: 'basic', label: '基础信息' }}
        onToggle={onToggle}
      >
        {selectedPaths.includes('basic') && <span className={SELECTION_MARK_CLASS}><CheckOutlined /></span>}
        <div className={SOURCE_RENDERED_CLASS}>
          <EditableSourceHeading value={skill.name} onChange={(value) => editBasic('name', value)} />
          <div className={SOURCE_META_LIST_CLASS}>
            <EditableSourceTextLine
              label={fieldLabel('skill_id')}
              value={skill.skill_id}
              readOnly={lockSkillId}
              onChange={(value) => editBasic('skill_id', value)}
            />
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
      <div className={SOURCE_GROUP_TITLE_CLASS}>详细节点</div>
      <div className={SOURCE_STEPS_CLASS}>
        <div className={cn(NODE_INSERT_ROW_CLASS, NODE_INSERT_ROW_EDGE_CLASS)}>
          <UIButton variant="outline" size="sm" className={NODE_INSERT_BUTTON_CLASS} onClick={() => insertNodeBetween(-1)}>
            <PlusOutlined />
            {steps.length > 0 ? '在最前新增节点' : '新增第一个节点'}
          </UIButton>
        </div>
        {steps.map((step, index) => {
          const stepId = String(step.node_id || step.step_id || `node_${index + 1}`);
          const path = stepTargetPath(index);
          const outgoingEdges = edgeMap[stepId] || [];
          const nodeState = [
            stepId === startNodeId ? '起始节点' : '',
            Boolean(step.optional) ? '可选' : '必选',
            terminalNodeIds.has(stepId) ? '终止节点' : '流程节点',
          ].filter(Boolean).join(' · ');
          return (
            <div className={SOURCE_STEP_BLOCK_CLASS} key={path}>
              {index > 0 && (
                <div className={NODE_INSERT_ROW_CLASS}>
                  <UIButton variant="outline" size="sm" className={NODE_INSERT_BUTTON_CLASS} onClick={() => insertNodeBetween(index - 1)}>
                    <PlusOutlined />
                    在 Node {index} 和 Node {index + 1} 之间新增节点
                  </UIButton>
                </div>
              )}
              <SelectableTarget
                className={distillSourceSectionClass(path, selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
                target={{ path, label: `节点 ${index + 1}：${step.name || stepId}` }}
                onToggle={onToggle}
              >
                {selectedPaths.includes(path) && <span className={SELECTION_MARK_CLASS}><CheckOutlined /></span>}
                <div className={SOURCE_RENDERED_CLASS}>
                  <div className={SOURCE_STEP_HEADER_CLASS}>
                    <EditableSourceStepHeading
                      index={index}
                      value={String(step.name || '')}
                      fallback={stepId}
                      onChange={(value) => editStep(index, 'name', value)}
                    />
                    <EditableSourceField>
                      <UIButton variant="destructive" size="sm" onClick={() => confirmDeleteNode(index)}>
                        <DeleteOutlined />
                        删除节点
                      </UIButton>
                    </EditableSourceField>
                  </div>
                  <div className={SOURCE_META_LIST_CLASS}>
                    <EditableSourceTextLine label={fieldLabel('step_id')} value={stepId} onChange={(value) => editStep(index, 'step_id', value)} />
                    <EditableSourceSelectLine
                      label={fieldLabel('type')}
                      value={String(step.type || 'collect_info')}
                      options={NODE_TYPE_OPTIONS}
                      onChange={(value) => editStep(index, 'type', value)}
                    />
                    <SourceReadonlyLine label="节点状态" value={nodeState} />
                    <EditableSourceTextLine
                      label={fieldLabel('instruction')}
                      value={String(step.instruction || '')}
                      multiline
                      collapsible
                      onChange={(value) => editStep(index, 'instruction', value)}
                    />
                    <EditableSourceListLine label={fieldLabel('expected_user_info')} values={asStringList(step.expected_user_info)} onChange={(value) => editStep(index, 'expected_user_info', value)} />
                    <EditableSourceActionLine
                      values={asStringList(step.allowed_actions)}
                      options={actionOptions}
                      toolDescriptions={toolDescriptions}
                      toolStatuses={toolStatuses}
                      onChange={(value) => editStep(index, 'allowed_actions', value)}
                    />
                    <EditableFlowRulesLine
                      sourceNodeId={stepId}
                      edges={outgoingEdges}
                      nodes={steps}
                      nodeOptions={nodeOptions}
                      terminal={terminalNodeIds.has(stepId)}
                      onAdd={() => addEdge(index)}
                      onUpdate={(edgeIndex, patch) => updateEdge(index, edgeIndex, patch)}
                      onDelete={(edgeIndex) => deleteEdge(index, edgeIndex)}
                    />
                    <SourceJsonLine label="知识范围" value={step.knowledge_scope} />
                    <EditableRetryPolicyLine
                      value={step.retry_policy}
                      onChange={(value) => editStep(index, 'retry_policy', value)}
                    />
                  </div>
                </div>
              </SelectableTarget>
            </div>
          );
        })}
        {steps.length > 0 && (
          <div className={cn(NODE_INSERT_ROW_CLASS, NODE_INSERT_ROW_EDGE_CLASS)}>
            <UIButton variant="outline" size="sm" className={NODE_INSERT_BUTTON_CLASS} onClick={() => insertNodeBetween(steps.length - 1)}>
              <PlusOutlined />
              在最后新增节点
            </UIButton>
          </div>
        )}
      </div>
      {renderDeleteNodeConfirm()}
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
  const isFitZoom = Math.abs(flowZoom - 0.64) < 0.001;
  const isFullZoom = Math.abs(flowZoom - 1) < 0.001;
  return (
    <>
      <div className={FLOW_ZOOM_TOOLBAR_CLASS} aria-label="流程图缩放">
        <span className="shrink-0">缩放</span>
        <UIButton variant="outline" size="sm" className={FLOW_ZOOM_STEP_BUTTON_CLASS} onClick={() => updateZoom(flowZoom - 0.08)} aria-label="缩小">
          -
        </UIButton>
        <span className={FLOW_ZOOM_VALUE_CLASS}>{Math.round(flowZoom * 100)}%</span>
        <UIButton variant="outline" size="sm" className={FLOW_ZOOM_STEP_BUTTON_CLASS} onClick={() => updateZoom(flowZoom + 0.08)} aria-label="放大">
          +
        </UIButton>
        <UIButton
          variant="outline"
          size="sm"
          className={flowZoomPresetButtonClass(isFitZoom)}
          aria-pressed={isFitZoom}
          onClick={() => updateZoom(0.64)}
        >
          适配
        </UIButton>
        <UIButton
          variant="outline"
          size="sm"
          className={flowZoomPresetButtonClass(isFullZoom)}
          aria-pressed={isFullZoom}
          onClick={() => updateZoom(1)}
        >
          100%
        </UIButton>
      </div>
      <div className={FLOW_CLASS} ref={containerRef}>
        <div
          className={FLOW_ZOOM_SHELL_CLASS}
          style={{ width: zoomedWidth, height: zoomedHeight }}
        >
          <div
            className={FLOW_GRAPH_CANVAS_CLASS}
            style={{
              width: graphLayout.width,
              height: graphLayout.height,
              transform: `scale(${flowZoom})`,
            }}
          >
            <svg
              className={FLOW_EDGES_CLASS}
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
                  className={FLOW_EDGE_PATH_CLASS}
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
                className={flowEdgeLabelClass(edge.labelTone || edge.kind)}
                key={`${edge.id}_label`}
                style={{ left: edge.labelX, top: edge.labelY }}
                title={edge.title}
              >
                {edge.label}
              </span>
            ))}
            <div
              className={FLOW_ROOT_POSITION_CLASS}
              style={{ left: graphLayout.root.x, top: graphLayout.root.y, width: graphLayout.root.width, height: graphLayout.root.height }}
            >
              <SelectableTarget
                className={distillFlowNodeClass('basic', true, selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
                target={{ path: 'basic', label: '基础信息' }}
                onToggle={onToggle}
              >
                {selectedPaths.includes('basic') && <span className={SELECTION_MARK_CLASS}><CheckOutlined /></span>}
                <span>基础信息</span>
                <strong><InlineDiffText path="basic" field="name" value={skill.name} diffs={textDiffs} /></strong>
                <small>{skill.skill_id}</small>
                <p><InlineDiffText path="basic" field="description" value={skill.description || '暂无描述'} diffs={textDiffs} /></p>
                <div className={FLOW_META_CLASS}>
                  <FlowMetaRow label="业务域">
                    <span className={FLOW_CHIP_CLASS}>{skill.business_domain || '-'}</span>
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
                className={FLOW_NODE_POSITION_CLASS}
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
    <div className={FLOW_NODE_SHELL_CLASS}>
      <SelectableTarget
        className={distillFlowNodeClass(path, false, selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
        target={{ path, label: `节点 ${index + 1}：${step.name || nodeId}` }}
        onToggle={onToggle}
      >
        {selectedPaths.includes(path) && <span className={SELECTION_MARK_CLASS}><CheckOutlined /></span>}
        <span>节点 {index + 1}</span>
        <strong><InlineDiffText path={path} field="name" value={String(step.name || nodeId)} diffs={textDiffs} /></strong>
        <small>{nodeId}</small>
        <div className={FLOW_NODE_BADGES_CLASS}>
          <span className={FLOW_CHIP_CLASS}>{nodeTypeLabel(String(step.type || 'collect_info'))}</span>
          {Boolean(step.optional) && <span className={FLOW_CHIP_CLASS}>可选</span>}
          {terminal && <span className={FLOW_CHIP_CLASS}>终止</span>}
        </div>
        <p className={FLOW_NODE_SUMMARY_CLASS} title={instruction}>
          <InlineDiffText path={path} field="instruction" value={instruction} diffs={textDiffs} />
        </p>
        <div className={FLOW_COMPACT_META_CLASS}>
          {expectedInfo.length > 0 && (
            <div className={FLOW_COMPACT_ROW_CLASS}>
              <span>字段</span>
              <PlainChipList values={expectedInfo.slice(0, 4)} />
            </div>
          )}
          {actionList.length > 0 && (
            <div className={FLOW_COMPACT_ROW_CLASS}>
              <span>动作</span>
              <ActionList actions={actionList.slice(0, 4)} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
            </div>
          )}
          {outgoingEdges.length > 0 && <span className={FLOW_ROUTE_COUNT_CLASS}>{outgoingRouteCountLabel(outgoingEdges)}</span>}
        </div>
      </SelectableTarget>
    </div>
  );
}

function FlowMetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={FLOW_META_ROW_CLASS}>
      <span className={FLOW_META_LABEL_CLASS}>{label}</span>
      {children}
    </div>
  );
}

function PlainChipList({ values }: { values: unknown }) {
  const items = asStringList(values);
  if (items.length === 0) return <span className={cn(FLOW_CHIP_CLASS, FLOW_CHIP_MUTED_CLASS)}>-</span>;
  return (
    <div className={FLOW_CHIP_LIST_CLASS}>
      {items.map((item, index) => (
        <span className={FLOW_CHIP_CLASS} key={`${item}_${index}`}>
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

function normalizeSkillNodes(skill: SkillCard): Array<Record<string, unknown>> {
  return Array.isArray(skill.nodes) ? skill.nodes.filter(isRecord).map((node, index) => ({
    ...node,
    node_id: String(node.node_id || node.step_id || `node_${index + 1}`),
  })) : [];
}

function normalizeSkillEdges(skill: SkillCard): Array<Record<string, unknown>> {
  return Array.isArray(skill.edges) ? skill.edges.filter(isRecord).map((edge, index) => ({
    source_node_id: String(edge.source_node_id || ''),
    next_node_id: String(edge.next_node_id || ''),
    condition: typeof edge.condition === 'string' ? edge.condition : '',
    priority: Number.isFinite(Number(edge.priority)) ? Number(edge.priority) : index,
    label: typeof edge.label === 'string' ? edge.label : '',
  })) : [];
}

function nodeIdAt(skill: SkillCard, index: number): string {
  const node = normalizeSkillNodes(skill)[index];
  return String(node?.node_id || node?.step_id || `node_${index + 1}`);
}

function findSourceEdgeIndex(edges: Array<Record<string, unknown>>, sourceId: string, localIndex: number): number {
  let seen = -1;
  return edges.findIndex((edge) => {
    if (String(edge.source_node_id || '') !== sourceId) return false;
    seen += 1;
    return seen === localIndex;
  });
}

function edgePriority(edge: Record<string, unknown>, fallback = 0): number {
  const value = Number(edge.priority);
  return Number.isFinite(value) ? value : fallback;
}

function uniqueNodeId(nodes: Array<Record<string, unknown>>, preferred: string): string {
  const used = new Set(nodes.map((node) => String(node.node_id || node.step_id || '')).filter(Boolean));
  const base = preferred.replace(/\s+/g, '_').replace(/[^\w.-]/g, '') || 'node';
  if (!used.has(base)) return base;
  for (let suffix = 2; suffix < 1000; suffix += 1) {
    const candidate = `${base}_${suffix}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${base}_${Date.now()}`;
}

function nodeDisplayNameById(nodes: Array<Record<string, unknown>>, nodeId: string): string {
  const index = nodes.findIndex((node) => String(node.node_id || node.step_id || '') === nodeId);
  if (index < 0) return nodeId || '未指定节点';
  const node = nodes[index];
  return `Node ${index + 1} · ${String(node.name || nodeId)}`;
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
  labelTone?: 'root' | 'branch' | 'parallel' | 'return';
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
  const sourceEdgeLabelCounts = rawEdges.reduce<Record<string, Record<string, number>>>((acc, edge) => {
    const sourceId = String(edge.source_node_id || '').trim();
    if (!sourceId) return acc;
    const label = normalizedEdgeLabel(edge, nodeNameMap);
    if (!acc[sourceId]) acc[sourceId] = {};
    acc[sourceId][label] = (acc[sourceId][label] || 0) + 1;
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
    const siblingCount = edgeSiblingCounts[sourceId] || 1;
    const baseLabel = normalizedEdgeLabel(edge, nodeNameMap);
    const hasDuplicateSourceLabel = (sourceEdgeLabelCounts[sourceId]?.[baseLabel] || 0) > 1;
    const isParallelFlow = hasDuplicateSourceLabel;
    const label = flowEdgeDisplayLabel(edge, nodeNameMap, siblingCount, hasDuplicateSourceLabel);
    const title = incomingEdgeLabel(edge, nodeNameMap);
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
      labelTone: isReturn ? 'return' : (isParallelFlow ? 'parallel' : (siblingCount > 1 ? 'branch' : 'root')),
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
      .sort((a, b) => {
        const priorityDelta = Number(a.priority || 0) - Number(b.priority || 0);
        if (priorityDelta !== 0) return priorityDelta;
        const aIndex = byId.get(String(a.next_node_id || ''))?.index ?? Number.MAX_SAFE_INTEGER;
        const bIndex = byId.get(String(b.next_node_id || ''))?.index ?? Number.MAX_SAFE_INTEGER;
        return aIndex - bIndex;
      })
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
      const sourceMeta = byId.get(sourceId);
      const targetMeta = byId.get(targetId);
      if (!sourceMeta || !targetMeta) return;
      if (targetMeta.index <= sourceMeta.index) return;
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
    const rank = Math.max(0, Math.min(ranks.get(item.nodeId) ?? item.index, item.index));
    if (!layerMap.has(rank)) layerMap.set(rank, []);
    layerMap.get(rank)?.push(item);
  });

  const layers = Array.from(layerMap.entries())
    .sort(([a], [b]) => a - b)
    .map(([, layer]) => layer.sort((a, b) => a.index - b.index));

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
  const targetName = edgeTargetName(edge, nodeNameMap);
  const label = String(edge.label || '');
  const condition = conditionNaturalText(String(edge.condition || ''));
  const route = [sourceName, targetName].filter(Boolean).join(' -> ');
  const detail = label && condition ? `${label}（${condition}）` : label || condition;
  if (route && detail) return `${route}：${detail}`;
  if (route) return route;
  return detail || '流转';
}

function edgeDisplayLabel(edge: Record<string, unknown>, nodeNameMap: Record<string, string> = {}): string {
  const label = String(edge.label || '').trim();
  if (label) return label;
  const condition = conditionNaturalText(String(edge.condition || '')).trim();
  if (condition) return condition;
  const source = String(edge.source_node_id || '');
  const sourceName = source && nodeNameMap[source] ? nodeNameMap[source] : source;
  return sourceName ? `来自 ${sourceName}` : '流转';
}

function normalizedEdgeLabel(edge: Record<string, unknown>, nodeNameMap: Record<string, string> = {}): string {
  return edgeDisplayLabel(edge, nodeNameMap).replace(/\s+/g, ' ').trim() || '流转';
}

function edgeTargetName(edge: Record<string, unknown>, nodeNameMap: Record<string, string> = {}): string {
  const targetId = String(edge.next_node_id || '').trim();
  return targetId ? nodeNameMap[targetId] || targetId : '';
}

function hasDuplicateSiblingEdgeLabel(
  edge: Record<string, unknown>,
  siblings: Array<Record<string, unknown>>,
  nodeNameMap: Record<string, string> = {},
): boolean {
  if (siblings.length <= 1) return false;
  const sourceId = String(edge.source_node_id || '').trim();
  const label = normalizedEdgeLabel(edge, nodeNameMap);
  return siblings.filter((item) => (
    String(item.source_node_id || '').trim() === sourceId
    && normalizedEdgeLabel(item, nodeNameMap) === label
  )).length > 1;
}

function hasDuplicateOutgoingEdgeLabel(
  edges: Array<Record<string, unknown>>,
  nodeNameMap: Record<string, string> = {},
): boolean {
  if (edges.length <= 1) return false;
  const labelCounts = edges.reduce<Record<string, number>>((acc, edge) => {
    const label = normalizedEdgeLabel(edge, nodeNameMap);
    acc[label] = (acc[label] || 0) + 1;
    return acc;
  }, {});
  return Object.values(labelCounts).some((count) => count > 1);
}

function outgoingRouteCountLabel(edges: Array<Record<string, unknown>>): string {
  return `${edges.length} 条${hasDuplicateOutgoingEdgeLabel(edges) ? '并行' : ''}流转`;
}

function flowEdgeDisplayLabel(
  edge: Record<string, unknown>,
  nodeNameMap: Record<string, string> = {},
  siblingCount = 1,
  hasDuplicateSourceLabel = false,
): string {
  const label = normalizedEdgeLabel(edge, nodeNameMap);
  const targetName = edgeTargetName(edge, nodeNameMap);
  if (hasDuplicateSourceLabel && targetName) {
    return `并行执行 · ${targetName}`;
  }
  const hasExplicitLabel = Boolean(String(edge.label || '').trim() || String(edge.condition || '').trim());
  if (siblingCount > 1 && targetName && (hasDuplicateSourceLabel || !hasExplicitLabel)) {
    return `${label} · 到${targetName}`;
  }
  return label;
}

function sourceEdgeSummary(
  edge: Record<string, unknown>,
  nodeNameMap: Record<string, string> = {},
  index = 0,
  siblingEdges: Array<Record<string, unknown>> = [],
): string {
  const targetName = edgeTargetName(edge, nodeNameMap) || '未指定节点';
  const label = String(edge.label || '').trim();
  const condition = conditionNaturalText(String(edge.condition || '')).trim();
  const hasPriority = edge.priority !== undefined && edge.priority !== null && String(edge.priority).trim() !== '';
  const priority = hasPriority && typeof edge.priority === 'number' ? edge.priority : hasPriority && Number.isFinite(Number(edge.priority)) ? Number(edge.priority) : index;
  const prefix = label || condition;
  const parallelText = hasDuplicateSiblingEdgeLabel(edge, siblingEdges, nodeNameMap) ? '并行执行 · ' : '';
  const priorityText = hasPriority && Number.isFinite(priority) ? ` · 优先级 ${priority}` : '';
  return `${parallelText}${prefix ? `${prefix} -> ` : ''}${targetName}${priorityText}`;
}

function compactEdgeLabel(value: string): string {
  const text = value.replace(/\s+/g, ' ').trim();
  if (text.length <= 24) return text;
  return `${text.slice(0, 21)}...`;
}

function nodeTypeLabel(type: string): string {
  return NODE_TYPE_OPTIONS.find((item) => item.value === type)?.label || type || '节点';
}

function knowledgeScopeLabels(value: unknown): string[] {
  if (!isRecord(value) || Object.keys(value).length === 0) return [];
  return Object.entries(value).map(([key, item]) => `${key}: ${String(item)}`);
}

function compactInputStyle(value: string, minCh = 8, maxCh = 92): CSSProperties {
  const longestLine = String(value || '').split('\n').reduce((max, line) => Math.max(max, visualTextWidth(line)), 0);
  const width = Math.max(minCh, Math.min(maxCh, longestLine + 2));
  return { width: `min(${width}ch, 100%)` };
}

function visualTextWidth(value: string): number {
  return Array.from(value).reduce((total, char) => {
    if (/[\u2e80-\u9fff\uff00-\uffef]/.test(char)) return total + 2;
    return total + 1;
  }, 0);
}

function sourceInputStyle(value: string, multiline = false): CSSProperties {
  const longestLine = String(value || '').split('\n').reduce((max, line) => Math.max(max, visualTextWidth(line)), 0);
  const minCh = multiline ? 34 : 18;
  const maxCh = multiline ? 96 : 72;
  const width = Math.max(minCh, Math.min(maxCh, longestLine + 4));
  return { width: `min(${width}ch, 100%)`, maxWidth: '100%' };
}

function previewSourceText(value: string): string {
  const text = value.replace(/\s+/g, ' ').trim();
  if (!text) return '暂无节点说明';
  return text.length > 96 ? `${text.slice(0, 96)}...` : text;
}

function EditableSourceHeading({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <EditableSourceField>
      <SourceInput
        className={SOURCE_TITLE_INPUT_CLASS}
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
      <div className={SOURCE_STEP_TITLE_EDIT_CLASS}>
        <span>Node {index + 1}:</span>
        <SourceInput
          value={value}
          placeholder={fallback}
          style={compactInputStyle(value || fallback, 10, 88)}
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
  collapsible = false,
  readOnly = false,
  onChange,
}: {
  label: string;
  value: string;
  multiline?: boolean;
  collapsible?: boolean;
  readOnly?: boolean;
  onChange: (value: string) => void;
}) {
  const canCollapse = collapsible && multiline;
  const shouldStartCollapsed = canCollapse && value.trim().length > 90;
  const [collapsed, setCollapsed] = useState(shouldStartCollapsed);

  useEffect(() => {
    if (!canCollapse || value.trim().length <= 90) {
      setCollapsed(false);
    }
  }, [canCollapse, value]);

  return (
    <div className={cn(SOURCE_LINE_CLASS, canCollapse && "collapsible")}>
      <span className={SOURCE_KEY_CLASS}>{label}</span>
      <span className={SOURCE_VALUE_CLASS}>
        <EditableSourceField>
          {canCollapse ? (
            <div className={SOURCE_COLLAPSIBLE_EDITOR_CLASS}>
              <button
                type="button"
                className={SOURCE_COLLAPSIBLE_HEAD_CLASS}
                onClick={() => setCollapsed((current) => !current)}
              >
                <span className={cn(SOURCE_COLLAPSIBLE_PREVIEW_CLASS, !collapsed && SOURCE_COLLAPSIBLE_PREVIEW_MUTED_CLASS)}>
                  {collapsed ? previewSourceText(value) : '正在编辑节点说明'}
                </span>
                <span className={SOURCE_COLLAPSIBLE_TOGGLE_CLASS}>
                  {collapsed ? <RightOutlined /> : <DownOutlined />}
                  {collapsed ? '展开' : '收起'}
                </span>
              </button>
              {!collapsed && (
                <AutoGrowTextarea
                  className={SOURCE_EDIT_INPUT_CLASS}
                  value={value}
                  style={sourceInputStyle(value, true)}
                  minRows={3}
                  readOnly={readOnly}
                  onChange={(event) => {
                    if (!readOnly) onChange(event.target.value);
                  }}
                />
              )}
            </div>
          ) : multiline ? (
            <AutoGrowTextarea
              className={SOURCE_EDIT_INPUT_CLASS}
              value={value}
              style={sourceInputStyle(value, true)}
              minRows={2}
              readOnly={readOnly}
              onChange={(event) => {
                if (!readOnly) onChange(event.target.value);
              }}
            />
          ) : (
            <SourceInput
              className={SOURCE_EDIT_INPUT_CLASS}
              value={value}
              style={sourceInputStyle(value)}
              readOnly={readOnly}
              onChange={(event) => {
                if (!readOnly) onChange(event.target.value);
              }}
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
    <div className={SOURCE_LINE_CLASS}>
      <span className={SOURCE_KEY_CLASS}>{label}</span>
      <span className={SOURCE_VALUE_CLASS}>
        <EditableSourceField>
          <AutoGrowTextarea
            className={SOURCE_EDIT_INPUT_CLASS}
            value={values.join('\n')}
            style={sourceInputStyle(values.join('\n'), true)}
            minRows={1}
            onChange={(event) => onChange(event.target.value)}
          />
        </EditableSourceField>
      </span>
    </div>
  );
}

function EditableSourceSelectLine({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
}) {
  const mergedOptions = options.some((option) => option.value === value) || !value
    ? options
    : [...options, { value, label: value }];
  return (
    <div className={SOURCE_LINE_CLASS}>
      <span className={SOURCE_KEY_CLASS}>{label}</span>
      <span className={SOURCE_VALUE_CLASS}>
        <EditableSourceField>
          <SourceSelect
            className={cn(SOURCE_SELECT_CLASS, "w-[220px]")}
            value={value}
            options={mergedOptions}
            onChange={onChange}
          />
        </EditableSourceField>
      </span>
    </div>
  );
}

function EditableConditionLine({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const presetValue = conditionPresetValue(value);
  const naturalValue = conditionNaturalText(value);
  return (
    <div className={SOURCE_LINE_CLASS}>
      <span className={SOURCE_KEY_CLASS}>{fieldLabel('condition')}</span>
      <span className={SOURCE_VALUE_CLASS}>
        <EditableSourceField>
          <div className={CONDITION_EDITOR_CLASS}>
            <SourceSelect
              className={CONDITION_PRESET_CLASS}
              value={presetValue}
              options={CONDITION_PRESET_OPTIONS}
              onChange={(nextValue) => {
                if (nextValue === '__custom__') {
                  onChange(naturalValue);
                  return;
                }
                onChange(conditionFromPreset(nextValue));
              }}
            />
            <AutoGrowTextarea
              className={cn(SOURCE_EDIT_INPUT_CLASS, CONDITION_INPUT_CLASS)}
              value={naturalValue}
              placeholder="用一句话描述什么时候进入，例如：用户已经提供商品名称后进入"
              minRows={1}
              onChange={(event) => onChange(event.target.value)}
            />
            <span className={CONDITION_READABLE_CLASS}>{conditionReadableText(value)}</span>
          </div>
        </EditableSourceField>
      </span>
    </div>
  );
}

function SourceReadonlyLine({ label, value }: { label: string; value: string }) {
  return (
    <div className={cn(SOURCE_LINE_CLASS, "readonly")}>
      <span className={SOURCE_KEY_CLASS}>{label}</span>
      <span className={cn(SOURCE_VALUE_CLASS, SOURCE_READONLY_VALUE_CLASS)}>{value || '-'}</span>
    </div>
  );
}

function SourceJsonLine({ label, value }: { label: string; value: unknown }) {
  if (!hasReadableSourceObject(value)) return null;
  return (
    <div className={cn(SOURCE_LINE_CLASS, "readonly")}>
      <span className={SOURCE_KEY_CLASS}>{label}</span>
      <span className={SOURCE_VALUE_CLASS}>
        <pre className={SOURCE_JSON_INLINE_CLASS}>{JSON.stringify(value, null, 2)}</pre>
      </span>
    </div>
  );
}

function EditableRetryPolicyLine({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const policy = isRecord(value) ? value : {};
  const attemptKey = Object.prototype.hasOwnProperty.call(policy, 'max_retries') ? 'max_retries' : 'max_attempts';
  const strategyKey = Object.prototype.hasOwnProperty.call(policy, 'strategy') ? 'strategy' : 'on_failure';
  const messageKey = Object.prototype.hasOwnProperty.call(policy, 'message') ? 'message' : 'retry_message';
  const maxAttempts = retryPolicyNumber(policy.max_retries ?? policy.max_attempts);
  const strategy = retryPolicyString(policy.strategy ?? policy.on_failure);
  const retryMessage = retryPolicyString(policy.retry_message ?? policy.message);
  const strategyOptions = mergeSelectOptions(
    RETRY_STRATEGY_OPTIONS,
    strategy ? [{ value: strategy, label: retryStrategyLabel(strategy) }] : [],
  );

  function commit(patch: Record<string, unknown>) {
    const next = { ...policy, ...patch };
    Object.keys(next).forEach((key) => {
      if (next[key] === '' || next[key] === null || next[key] === undefined) delete next[key];
    });
    onChange(next);
  }

  function updateAttempts(nextValue: number | string | null) {
    const nextNumber = Number(nextValue);
    commit(Number.isFinite(nextNumber) && nextNumber > 0
      ? { [attemptKey]: Math.floor(nextNumber) }
      : { max_attempts: undefined, max_retries: undefined });
  }

  function updateStrategy(nextValue?: string) {
    commit(nextValue ? { [strategyKey]: nextValue } : { on_failure: undefined, strategy: undefined });
  }

  function updateMessage(nextValue: string) {
    commit(nextValue.trim() ? { [messageKey]: nextValue } : { retry_message: undefined, message: undefined });
  }

  return (
    <div className={SOURCE_LINE_CLASS}>
      <span className={SOURCE_KEY_CLASS}>重试策略</span>
      <span className={SOURCE_VALUE_CLASS}>
        <EditableSourceField>
          <div className={RETRY_POLICY_EDITOR_CLASS}>
            <label className={RETRY_POLICY_FIELD_CLASS}>
              <span>最多重试</span>
              <SourceNumberInput
                min={0}
                value={maxAttempts}
                placeholder="不限制"
                onChange={updateAttempts}
              />
            </label>
            <label className={RETRY_POLICY_FIELD_CLASS}>
              <span>失败后</span>
              <SourceSelect
                value={strategy || undefined}
                options={strategyOptions}
                placeholder="选择处理方式"
                onChange={(nextValue) => updateStrategy(nextValue)}
              />
            </label>
            <label className={RETRY_POLICY_FIELD_CLASS}>
              <span>追问文案</span>
              <SourceInput
                value={retryMessage}
                placeholder="例如：请补充需要校验的报文内容。"
                onChange={(event) => updateMessage(event.target.value)}
              />
            </label>
          </div>
        </EditableSourceField>
      </span>
    </div>
  );
}

function retryPolicyNumber(value: unknown): number | null {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) && numberValue > 0 ? Math.floor(numberValue) : null;
}

function retryPolicyString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function retryStrategyLabel(value: string): string {
  return RETRY_STRATEGY_OPTIONS.find((item) => item.value === value)?.label || value;
}

function hasReadableSourceObject(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return Object.keys(value).length > 0;
}

function EditableSourceActionLine({
  values,
  options,
  toolDescriptions,
  toolStatuses,
  onChange,
}: {
  values: string[];
  options: SelectOption[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  onChange: (value: string) => void;
}) {
  return (
    <div className={SOURCE_LINE_CLASS}>
      <span className={SOURCE_KEY_CLASS}>{fieldLabel('allowed_actions')}</span>
      <span className={SOURCE_VALUE_CLASS}>
        <EditableSourceField>
          <EditableActionList
            actions={values}
            options={options}
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
  options,
  toolDescriptions,
  toolStatuses,
  onChange,
}: {
  actions: string[];
  options: SelectOption[];
  toolDescriptions: ToolDescriptionMap;
  toolStatuses: ToolStatusMap;
  onChange: (value: string) => void;
}) {
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const mergedOptions = mergeSelectOptions(options, actions.map((action) => ({
    value: action,
    label: actionLabel(action),
  })));

  function writeActions(nextActions: string[]) {
    onChange(nextActions.filter(Boolean).join('\n'));
  }

  function commitAction(index: number, action: string) {
    const nextAction = action.trim();
    const next = [...actions];
    if (!nextAction) {
      if (index < next.length) next.splice(index, 1);
      writeActions(next);
      setEditingIndex(null);
      return;
    }
    const duplicateIndex = next.findIndex((item, itemIndex) => item === nextAction && itemIndex !== index);
    if (duplicateIndex >= 0) {
      notify.info('这个动作已经添加过了');
      setEditingIndex(null);
      return;
    }
    if (index >= next.length) {
      next.push(nextAction);
    } else {
      next[index] = nextAction;
    }
    writeActions(next);
    setEditingIndex(null);
  }

  function removeAction(index: number) {
    const next = [...actions];
    next.splice(index, 1);
    writeActions(next);
    if (editingIndex === index) setEditingIndex(null);
  }

  function actionSelect(index: number, value?: string) {
    return (
      <ActionCombobox
        value={value || undefined}
        options={mergedOptions}
        placeholder="选择一个动作"
        onSelect={(nextValue) => commitAction(index, String(nextValue || ''))}
      />
    );
  }

  return (
    <div className={cn(SOURCE_ACTION_EDITOR_CLASS, "group/action-editor")}>
      <div className={cn(SOURCE_ACTION_LIST_CLASS, SOURCE_ACTION_LIST_EDITABLE_CLASS)}>
        {actions.map((action, index) => (
          editingIndex === index ? (
            <span className={SOURCE_ACTION_PICKER_CLASS} key={`editing_${index}`}>
              {actionSelect(index, action)}
            </span>
          ) : (
            <span className={cn(SOURCE_ACTION_TOKEN_CLASS, "group/token")} key={`${action}_${index}`}>
              <button
                type="button"
                className={SOURCE_ACTION_EDIT_BUTTON_CLASS}
                onClick={() => setEditingIndex(index)}
              >
                <ActionChip action={action} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
              </button>
              <button type="button" className={SOURCE_ACTION_REMOVE_CLASS} onClick={() => removeAction(index)} aria-label={`移除 ${actionLabel(action)}`}>
                ×
              </button>
            </span>
          )
        ))}
        {editingIndex !== null && editingIndex >= actions.length && (
          <span className={SOURCE_ACTION_PICKER_CLASS}>
            {actionSelect(editingIndex)}
          </span>
        )}
        {editingIndex === null && (
          <button type="button" className={SOURCE_ACTION_ADD_CLASS} onClick={() => setEditingIndex(actions.length)}>
            <PlusOutlined />
            新增动作
          </button>
        )}
      </div>
      <span className={SOURCE_EDIT_HINT_CLASS}>每次新增一个动作；点击已有动作可重新选择。</span>
    </div>
  );
}

function EditableFlowRulesLine({
  sourceNodeId,
  edges,
  nodes,
  nodeOptions,
  terminal,
  onAdd,
  onUpdate,
  onDelete,
}: {
  sourceNodeId: string;
  edges: Array<Record<string, unknown>>;
  nodes: Array<Record<string, unknown>>;
  nodeOptions: SelectOption[];
  terminal: boolean;
  onAdd: () => void;
  onUpdate: (edgeIndex: number, patch: Record<string, unknown>) => void;
  onDelete: (edgeIndex: number) => void;
}) {
  const orderedEdges = edges
    .map((edge, index) => ({ edge, index }))
    .sort((a, b) => edgePriority(a.edge, a.index) - edgePriority(b.edge, b.index));
  return (
    <div className={SOURCE_LINE_CLASS}>
      <span className={SOURCE_KEY_CLASS}>流转规则</span>
      <span className={SOURCE_VALUE_CLASS}>
        <EditableSourceField>
          <div className={FLOW_RULE_EDITOR_CLASS}>
            <div className={FLOW_RULE_HEAD_CLASS}>
              <span>从 {nodeDisplayNameById(nodes, sourceNodeId)} 出发</span>
              <UIButton variant="outline" size="sm" className={PILL_OUTLINE_BUTTON_CLASS} onClick={onAdd}>
                <PlusOutlined />
                新增规则
              </UIButton>
            </div>
            {orderedEdges.length === 0 ? (
              <div className={FLOW_RULE_EMPTY_CLASS}>{terminal ? '当前节点是终止节点，默认流程结束。' : '还没有后续节点，请新增流转规则。'}</div>
            ) : (
              <div className={FLOW_RULE_LIST_CLASS}>
                {orderedEdges.map(({ edge, index }) => (
                  <div className={FLOW_RULE_ITEM_CLASS} key={`${String(edge.next_node_id)}_${index}`}>
                    <label className={cn(FLOW_RULE_FIELD_CLASS, FLOW_RULE_FIELD_TARGET_CLASS)}>
                      <span>目标 Node</span>
                      <SourceSelect
                        className={FLOW_RULE_TARGET_CLASS}
                        value={String(edge.next_node_id || '') || undefined}
                        options={nodeOptions}
                        placeholder="选择目标 Node"
                        onChange={(value) => onUpdate(index, { next_node_id: value })}
                      />
                    </label>
                    <label className={cn(FLOW_RULE_FIELD_CLASS, FLOW_RULE_FIELD_LABEL_CLASS)}>
                      <span>规则名称</span>
                      <SourceInput
                        className={FLOW_RULE_LABEL_INPUT_CLASS}
                        value={String(edge.label || '')}
                        placeholder="例如：信息完整后继续"
                        onChange={(event) => onUpdate(index, { label: event.target.value })}
                      />
                    </label>
                    <div className={cn(FLOW_RULE_FIELD_CLASS, FLOW_RULE_FIELD_CONDITION_CLASS)}>
                      <span>进入条件</span>
                      <div className={FLOW_RULE_CONDITION_CONTROLS_CLASS}>
                        <SourceSelect
                          className={CONDITION_PRESET_CLASS}
                          value={conditionPresetValue(String(edge.condition || ''))}
                          options={CONDITION_PRESET_OPTIONS}
                          onChange={(nextValue) => {
                            if (nextValue === '__custom__') {
                              onUpdate(index, { condition: conditionNaturalText(String(edge.condition || '')) });
                              return;
                            }
                            onUpdate(index, { condition: conditionFromPreset(nextValue) });
                          }}
                        />
                        <AutoGrowTextarea
                          className={FLOW_RULE_CONDITION_INPUT_CLASS}
                          value={conditionNaturalText(String(edge.condition || ''))}
                          placeholder="用一句话描述，例如：报文已获取后进入"
                          minRows={1}
                          onChange={(event) => onUpdate(index, { condition: event.target.value })}
                        />
                      </div>
                      <em>{flowRuleConditionText(String(edge.condition || ''))}</em>
                    </div>
                    <label className={cn(FLOW_RULE_FIELD_CLASS, FLOW_RULE_FIELD_PRIORITY_CLASS)}>
                      <span>优先级</span>
                      <SourceNumberInput
                        className={FLOW_RULE_PRIORITY_CLASS}
                        min={0}
                        value={edgePriority(edge, index)}
                        onChange={(value) => onUpdate(index, { priority: Number(value ?? 0) })}
                      />
                    </label>
                    <UIButton variant="destructive" size="icon" className={FLOW_RULE_DELETE_CLASS} onClick={() => onDelete(index)}>
                      <DeleteOutlined />
                    </UIButton>
                  </div>
                ))}
              </div>
            )}
          </div>
        </EditableSourceField>
      </span>
    </div>
  );
}

function EditableSourceField({ children }: { children: ReactNode }) {
  function stop(event: MouseEvent<HTMLDivElement> | KeyboardEvent<HTMLDivElement>) {
    event.stopPropagation();
  }

  return (
    <div className={SOURCE_EDIT_FIELD_CLASS} onMouseDown={stop} onClick={stop} onDoubleClick={stop} onKeyDown={stop}>
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
  if (visibleActions.length === 0 && removed.length === 0) return <span className={ACTION_EMPTY_CLASS}>-</span>;
  return (
    <div className={ACTION_LIST_CLASS}>
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
  if (actions.length === 0) return <span className={ACTION_EMPTY_CLASS}>-</span>;
  return (
    <div className={ACTION_LIST_CLASS}>
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
  const variant = className.includes('removed')
    ? 'removed'
    : className.includes('added')
      ? className.includes('typing')
        ? 'typing'
        : className.includes('settled')
          ? 'settled'
          : 'added'
      : undefined;

  return (
    <span
      className={actionChipClass({ toolName: toolName || undefined, status, variant })}
      title={description || undefined}
    >
      {actionLabel(action)}
    </span>
  );
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
          <div className={cn(SAVE_REVIEW_ACTION_DIFF_CLASS, SAVE_REVIEW_ACTION_DIFF_OLD_CLASS)}>
            <span className={cn(SAVE_REVIEW_DIFF_SIGN_CLASS, SAVE_REVIEW_DIFF_SIGN_OLD_CLASS)}>-</span>
            <ActionList actions={removedActions} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
          </div>
        )}
        {diff.inserted && (
          <div className={cn(SAVE_REVIEW_ACTION_DIFF_CLASS, SAVE_REVIEW_ACTION_DIFF_NEW_CLASS)}>
            <span className={cn(SAVE_REVIEW_DIFF_SIGN_CLASS, SAVE_REVIEW_DIFF_SIGN_NEW_CLASS)}>+</span>
            <ActionList actions={insertedActions} toolDescriptions={toolDescriptions} toolStatuses={toolStatuses} />
          </div>
        )}
      </>
    );
  }
  return (
    <>
      {diff.removed && <div><span className={DIFF_OLD_CLASS}>- {diff.removed}</span></div>}
      {diff.inserted && <div><span className={DIFF_NEW_CLASS}>+ {diff.inserted}</span></div>}
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
        {diff.removed ? <span className={INLINE_REMOVE_CLASS}>{diff.removed}</span> : null}
        {diff.suffix}
      </>
    );
  }
  const typedInsert = diff.inserted.slice(0, Math.ceil(diff.inserted.length * diff.progress));
  return (
    <>
      {diff.prefix}
      {typedInsert ? <span className={cn(INLINE_ADD_CLASS, diff.phase === 'settled' && INLINE_ADD_SETTLED_CLASS)}>{typedInsert}</span> : null}
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
  return { title: '新SOP', raw_content: text.trim() };
}

function createStreamingDraftSeed(payload: { title: string; raw_content: string }): SkillCard {
  return {
    skill_id: `skill_${slugSegment(payload.title) || 'preview'}`,
    name: payload.title || '新SOP',
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
      name: stringValue(draft.name, '新SOP'),
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
  return filename.replace(/\.[^.]+$/, '').trim() || '新SOP';
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

function toolSuggestionStatusClass(suggestion: ToolSuggestionItem): ToolStatusBadgeVariant {
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

function compactWarningItems(
  warnings: string[],
  _toolSuggestions: ToolSuggestionItem[] | undefined,
): Array<{ text: string; title: string }> {
  const items: Array<{ text: string; title: string }> = [];
  for (const warning of warnings) {
    const text = warning.trim();
    if (!text) continue;
    const existing = items.find((item) => item.text === text);
    if (existing) {
      existing.title = `${existing.title}\n${warning}`;
      continue;
    }
    items.push({ text, title: warning });
  }
  return items;
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

function removeDistillCache(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Cache cleanup is best-effort.
  }
}

function isBlankDistillWorkspace(snapshot: DistillCacheSnapshot): boolean {
  return !snapshot.draft && !snapshot.loadedSkill && !snapshot.lastSavedDraft;
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


function mergePaths(current: string[], next: string[]): string[] {
  return Array.from(new Set([...current, ...next]));
}

function cloneSkill(skill: SkillCard): SkillCard {
  return JSON.parse(JSON.stringify(skill)) as SkillCard;
}

function uniqueDraftSkillId(skillId: string): string {
  const normalized = (skillId || 'skill')
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, '_')
    .replace(/^_+|_+$/g, '')
    || 'skill';
  return `${normalized}_${Date.now().toString(36)}`;
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

function integrateToolSuggestionsIntoDraft(
  skill: SkillCard,
  suggestions: ToolSuggestionItem[],
  fallbackPaths: string[] = [],
): SkillCard {
  const next = cloneSkill(skill);
  const nodes = normalizeSkillNodes(next);
  const fallbackIndexes = fallbackPaths
    .map(stepIndexFromPath)
    .filter((index): index is number => index !== null && index >= 0 && index < nodes.length);

  suggestions.forEach((suggestion) => {
    const toolName = suggestion.name.trim();
    if (!toolName) return;
    const action = `call_tool:${toolName}`;
    const existingIndexes = nodes
      .map((node, index) => (asStringList(node.allowed_actions).includes(action) ? index : -1))
      .filter((index) => index >= 0);
    const targetIndexes = existingIndexes.length > 0
      ? existingIndexes
      : toolSuggestionTargetIndexes(nodes, suggestion, fallbackIndexes);

    targetIndexes.forEach((nodeIndex) => {
      const node = nodes[nodeIndex];
      const actions = asStringList(node.allowed_actions);
      if (actions.includes(action)) return;
      node.allowed_actions = [...actions, action];
    });
  });

  next.nodes = nodes;
  return next;
}

function toolSuggestionTargetIndexes(
  nodes: Array<Record<string, unknown>>,
  _suggestion: ToolSuggestionItem,
  fallbackIndexes: number[],
): number[] {
  const uniqueFallbacks = Array.from(new Set(fallbackIndexes));
  if (uniqueFallbacks.length === 1) return uniqueFallbacks;
  const toolNodeIndexes = nodes
    .map((node, index) => (String(node.type || '') === 'tool_call' ? index : -1))
    .filter((index) => index >= 0);
  if (toolNodeIndexes.length === 1) return toolNodeIndexes;
  return [];
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
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

function upsertToolRead(current: ToolRead[], nextTool: ToolRead): ToolRead[] {
  const exists = current.some((tool) => tool.name === nextTool.name);
  return exists
    ? current.map((tool) => (tool.name === nextTool.name ? { ...tool, ...nextTool, id: nextTool.id || tool.id } : tool))
    : [...current, nextTool];
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
  return BASE_ACTION_OPTIONS.find((item) => item.value === action)?.label || action;
}

function buildActionOptions(
  toolDescriptions: ToolDescriptionMap,
  toolStatuses: ToolStatusMap,
  steps: Array<Record<string, unknown>>,
): SelectOption[] {
  const toolNames = Array.from(new Set([
    ...Object.keys(toolDescriptions),
    ...Object.keys(toolStatuses),
  ])).filter(Boolean).sort((a, b) => a.localeCompare(b));
  const toolOptions = toolNames.map((toolName) => ({
    value: `call_tool:${toolName}`,
    label: `调用工具：${toolName}`,
  }));
  const currentActionOptions = steps
    .flatMap((step) => asStringList(step.allowed_actions))
    .filter(Boolean)
    .map((action) => ({ value: action, label: actionLabel(action) }));
  return mergeSelectOptions(BASE_ACTION_OPTIONS, toolOptions, currentActionOptions);
}

function mergeSelectOptions(...groups: SelectOption[][]): SelectOption[] {
  const seen = new Set<string>();
  const output: SelectOption[] = [];
  groups.flat().forEach((option) => {
    if (!option.value || seen.has(option.value)) return;
    seen.add(option.value);
    output.push(option);
  });
  return output;
}

function conditionPresetValue(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === 'always' || trimmed === 'true') return '__always__';
  if (CONDITION_PRESET_OPTIONS.some((option) => option.value === trimmed)) return trimmed;
  const naturalMatch = Object.entries(CONDITION_PRESET_TEXT).find(([, text]) => text === trimmed);
  if (naturalMatch) return naturalMatch[0];
  return '__custom__';
}

function conditionFromPreset(value: string): string {
  if (value === '__always__') return '';
  return CONDITION_PRESET_TEXT[value] || '';
}

function conditionNaturalText(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === 'always' || trimmed === 'true') return '';
  if (CONDITION_PRESET_TEXT[trimmed]) return CONDITION_PRESET_TEXT[trimmed];
  const presetMatch = Object.entries(CONDITION_PRESET_TEXT).find(([, text]) => text === trimmed);
  if (presetMatch) return presetMatch[1];
  return trimmed;
}

function conditionReadableText(value: string): string {
  const natural = conditionNaturalText(value);
  return natural ? `模型理解：${natural}。` : '模型理解：没有额外限制，流程可以从这里继续。';
}

function flowRuleConditionText(value: string): string {
  const natural = conditionNaturalText(value);
  return natural ? `进入条件：${natural}。` : '进入条件：总是进入。';
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
