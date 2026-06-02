import {
  BranchesOutlined,
  CheckOutlined,
  CodeOutlined,
  CloseOutlined,
  DownOutlined,
  FileTextOutlined,
  LoadingOutlined,
  RightOutlined,
  SaveOutlined,
  SendOutlined,
  StopOutlined,
  UploadOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import { Button, Card, Empty, Input, Modal, Space, Tooltip, Typography, Upload, message } from 'antd';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, streamPost, TENANT_ID } from '../api/client';
import type { SkillCard, SkillRead, ToolRead, ToolSuggestion } from '../types';

type ChatItem = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  thinking?: 'running' | 'done';
  thinkingDetails?: string[];
  thinkingOpen?: boolean;
  warnings?: string[];
  toolSuggestions?: ToolSuggestionItem[];
  actionState?: 'pending' | 'confirmed' | 'rejected';
};

type ToolSuggestionItem = ToolSuggestion & {
  status?: 'pending' | 'created' | 'rejected';
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

type ViewMode = 'source' | 'flow';
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

const DEFAULT_TARGET_PATHS: string[] = [];
const DEFAULT_DISTILL_MESSAGES: ChatItem[] = [
  {
    id: 'welcome',
    role: 'assistant',
    content: '请粘贴原始技能说明，或点击右侧某一块后告诉我需要怎样改写。',
  },
];

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
};

export default function DistillPage() {
  const [searchParams] = useSearchParams();
  const skillId = searchParams.get('skill_id');
  const mode = searchParams.get('mode') || '';
  const cacheKey = `skill-distill:${TENANT_ID}:${skillId || mode || 'new'}`;
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
  const [tools, setTools] = useState<ToolRead[]>([]);
  const [streamStatus, setStreamStatus] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const uploadControllersRef = useRef<Record<string, AbortController>>({});
  const dragDepthRef = useRef(0);
  const animationTimersRef = useRef<number[]>([]);
  const sourceScrollRef = useRef<HTMLDivElement | null>(null);
  const chatMessagesRef = useRef<HTMLDivElement | null>(null);
  const [cacheReady, setCacheReady] = useState(false);
  const [hydratedCacheKey, setHydratedCacheKey] = useState('');

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
      setSaveDraftSnapshot(null);
      setHydratedCacheKey(cacheKey);
      setCacheReady(true);
      return;
    }

    api
      .get<SkillRead>(`/api/enterprise/skills/${encodeURIComponent(skillId)}?tenant_id=${TENANT_ID}`)
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
  }, [cacheKey, skillId]);

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
      streamStatus: loading ? '已中断' : streamStatus,
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
    textDiffs,
    updatingPaths,
    viewMode,
  ]);

  useEffect(() => {
    document.body.classList.add('skill-distill-fixed');
    return () => {
      document.body.classList.remove('skill-distill-fixed');
      abortRef.current?.abort();
      Object.values(uploadControllersRef.current).forEach((controller) => controller.abort());
      clearAnimationTimers();
    };
  }, []);

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
    if (!loading || !sourceScrollRef.current) return;
    sourceScrollRef.current.scrollTop = sourceScrollRef.current.scrollHeight;
  }, [draft, loading, textDiffs, viewMode]);

  const allPaths = useMemo(() => (draft ? allTargetPaths(draft) : DEFAULT_TARGET_PATHS), [draft]);
  const uploadingFile = attachments.some((item) => item.status === 'uploading');
  const readyAttachments = attachments.filter((item) => item.status === 'ready' && item.text?.trim());
  const allSelected = draft ? selectedPaths.length > 0 && allPaths.every((path) => selectedPaths.includes(path)) : false;
  const toolDescriptions = useMemo(() => buildToolDescriptionMap(tools), [tools]);
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

  async function send() {
    const text = buildOutgoingText(input, readyAttachments);
    if (!text || loading || uploadingFile) return;
    const displayText = buildDisplayText(input, readyAttachments);
    const confirmedDraft = pendingChange?.nextDraft || draft;
    confirmPendingChange(false);
    setInput('');
    setAttachments([]);
    pushMessage('user', displayText);
    if (!confirmedDraft) {
      await createDraftFromText(text);
      return;
    }
    await rewriteSelectedTarget(text, confirmedDraft);
  }

  async function createDraftFromText(text: string) {
    const payload = parseInitialSkillPrompt(text);
    setLoading(true);
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
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamPost(
        '/api/enterprise/skills/distill/stream',
        { tenant_id: TENANT_ID, ...payload },
        (item) => {
          if (item.event === 'status') {
            appendThinkingDetail(assistantId, String(item.data.text || '正在处理'));
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
              { thinking: 'done', warnings: nextWarnings, toolSuggestions: nextToolSuggestions },
            );
            setStreamStatus('生成完成');
          }
        },
        controller.signal,
      );
    } catch (error) {
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
  ) {
    if (!currentDraft) return;
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
    const controller = new AbortController();
    let receivedMessageChunk = false;
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
          conversation: messages.map((item) => ({ role: item.role, content: item.content })),
        },
        (item) => {
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
                },
              );
            } else {
              updateMessage(assistantId, undefined, {
                thinking: 'done',
                warnings: nextWarnings,
                toolSuggestions: nextToolSuggestions,
                actionState: 'pending',
              });
            }
          }
        },
        controller.signal,
      );
    } catch (error) {
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
    const finalDraft = saveReviewDraft;
    try {
      let savedSkill: SkillRead;
      if (loadedSkill) {
        savedSkill = await api.put<SkillRead>(`/api/enterprise/skills/${loadedSkill.skill_id}`, {
          tenant_id: TENANT_ID,
          content: finalDraft,
          status: loadedSkill.status,
        });
      } else {
        try {
          savedSkill = await api.post<SkillRead>('/api/enterprise/skills', { tenant_id: TENANT_ID, content: finalDraft, status: 'draft' });
        } catch (error) {
          if (!(error instanceof Error) || !error.message.includes('409')) throw error;
          savedSkill = await api.put<SkillRead>(`/api/enterprise/skills/${finalDraft.skill_id}`, {
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
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setStreamStatus('已停止');
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

  async function confirmToolSuggestion(messageId: string, suggestion: ToolSuggestionItem) {
    if (loading) return;
    const activeDraft = pendingChange?.nextDraft || draft;
    try {
      let createdTool: ToolRead;
      const payload = toolPayloadFromSuggestion(suggestion, activeDraft?.skill_id);
      try {
        createdTool = await api.post<ToolRead>('/api/enterprise/tools', payload);
        message.success('工具已新增');
      } catch (error) {
        if (!(error instanceof Error) || !error.message.includes('409')) throw error;
        createdTool = toolReadFromSuggestion(suggestion, activeDraft?.skill_id);
        message.info('工具已存在，继续更新技能草稿');
      }
      setTools((current) => upsertToolRead(current, createdTool));
      setToolSuggestionStatus(messageId, suggestion.name, 'created');
      if (!activeDraft) return;
      confirmPendingChange(false);
      await rewriteSelectedTarget(
        buildToolIntegrationInstruction(suggestion),
        activeDraft,
        allTargetPaths(activeDraft),
        [
          `已新增工具：${suggestion.display_name || suggestion.name}`,
          '正在判断该工具应接入哪些步骤',
        ],
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : '新增工具或更新技能失败');
    }
  }

  function rejectToolSuggestion(messageId: string, toolName: string) {
    setToolSuggestionStatus(messageId, toolName, 'rejected');
  }

  function setToolSuggestionStatus(messageId: string, toolName: string, status: ToolSuggestionItem['status']) {
    setMessages((current) =>
      current.map((item) =>
        item.id === messageId
          ? {
              ...item,
              toolSuggestions: (item.toolSuggestions || []).map((suggestion) =>
                suggestion.name === toolName ? { ...suggestion, status } : suggestion,
              ),
            }
          : item,
      ),
    );
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
    if (!lastSavedDraft) return true;
    return JSON.stringify(targetDraft) !== JSON.stringify(lastSavedDraft);
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
  }

  function toggleTarget(target: TargetSelection) {
    setSelectedPaths((current) => {
      if (current.includes(target.path)) {
        return current.filter((path) => path !== target.path);
      }
      return [...current, target.path];
    });
  }

  function toggleAllTargets() {
    setSelectedPaths(allSelected ? [] : allPaths);
  }

  function pushMessage(role: ChatItem['role'], content: string, extra: Partial<ChatItem> = {}) {
    const id = `${role}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    setMessages((current) => [...current, { id, role, content, ...extra }]);
    return id;
  }

  function updateMessage(id: string, content?: string, extra: Partial<ChatItem> = {}) {
    setMessages((current) =>
      current.map((item) => (item.id === id ? { ...item, ...(content === undefined ? {} : { content }), ...extra } : item)),
    );
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
        <Typography.Title level={3}>技能改写</Typography.Title>
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
              {messages.map((item) => (
                <div key={item.id} className={`skill-chat-row ${item.role}`}>
                  <div className="skill-chat-bubble">
                    {item.role === 'assistant' && item.thinking && (
                      <div className={`skill-chat-thinking-block ${item.thinking}`}>
                        <button
                          type="button"
                          className="skill-chat-thinking"
                          onClick={() => toggleThinking(item.id)}
                        >
                          {item.thinking === 'running' ? <LoadingOutlined /> : <CheckOutlined />}
                          <span>{item.thinking === 'running' ? '正在思考' : '已完成思考'}</span>
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
                    {item.content ? <div className="skill-chat-content">{item.content}</div> : item.thinking === 'running' ? null : '正在处理...'}
                    {item.warnings && item.warnings.length > 0 && (
                      <div className="skill-chat-warning">
                        <div className="skill-chat-warning-title">
                          <WarningOutlined />
                          <span>提示</span>
                        </div>
                        {compactWarningItems(item.warnings).map((warning, index) => (
                          <div key={`${item.id}_warning_${index}`} className="skill-chat-warning-item" title={warning.title}>
                            {warning.text}
                          </div>
                        ))}
                      </div>
                    )}
                    {item.toolSuggestions && item.toolSuggestions.length > 0 && (
                      <div className="skill-tool-suggestions">
                        {item.toolSuggestions.map((suggestion) => (
                          <div className="skill-tool-suggestion" key={`${item.id}_${suggestion.name}`}>
                            <div>
                              <div className="skill-tool-suggestion-title">
                                建议新增工具：{suggestion.display_name || suggestion.name}
                              </div>
                              <div className="skill-tool-suggestion-desc">
                                {suggestion.reason || suggestion.description || suggestion.name}
                              </div>
                            </div>
                            <Space>
                              <Button size="small" onClick={() => setToolDetail(suggestion)}>
                                详情
                              </Button>
                              {suggestion.status !== 'created' && suggestion.status !== 'rejected' && (
                                <>
                                  <Button size="small" type="primary" onClick={() => void confirmToolSuggestion(item.id, suggestion)}>
                                    确认
                                  </Button>
                                  <Button size="small" onClick={() => rejectToolSuggestion(item.id, suggestion.name)}>
                                    拒绝
                                  </Button>
                                </>
                              )}
                              {suggestion.status === 'created' && <Typography.Text type="secondary">已新增</Typography.Text>}
                              {suggestion.status === 'rejected' && <Typography.Text type="secondary">已拒绝</Typography.Text>}
                            </Space>
                          </div>
                        ))}
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
              <Button disabled={!draft || loading} icon={<SaveOutlined />} onClick={() => openSaveReview()}>
                保存草稿
              </Button>
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
              containerRef={sourceScrollRef}
              onToggle={toggleTarget}
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
            <Input value={saveVersion} onChange={(event) => setSaveVersion(event.target.value)} />
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
                <SaveReviewDiffValue diff={diff} toolDescriptions={toolDescriptions} />
              </div>
            ))
          )}
        </div>
      </Modal>
      <Modal
        open={Boolean(toolDetail)}
        title="工具详情"
        footer={null}
        width={760}
        onCancel={() => setToolDetail(null)}
      >
        {toolDetail && (
          <div className="tool-suggestion-detail">
            <div><strong>工具名：</strong>{toolDetail.name}</div>
            <div><strong>显示名：</strong>{toolDetail.display_name || '-'}</div>
            <div><strong>说明：</strong>{toolDetail.description || '-'}</div>
            <div><strong>方法：</strong>{toolDetail.method}</div>
            <div><strong>URL：</strong>{toolDetail.url}</div>
            <div><strong>原因：</strong>{toolDetail.reason || '-'}</div>
            <Typography.Text strong>输入 Schema</Typography.Text>
            <pre>{JSON.stringify(toolDetail.input_schema || {}, null, 2)}</pre>
            <Typography.Text strong>输出 Schema</Typography.Text>
            <pre>{JSON.stringify(toolDetail.output_schema || {}, null, 2)}</pre>
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
  containerRef: RefObject<HTMLDivElement>;
  onToggle: (target: TargetSelection) => void;
}) {
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
          <h2><InlineDiffText path="basic" field="name" value={skill.name} diffs={textDiffs} /></h2>
          <div className="skill-source-meta-list">
            <SourceTextLine path="basic" field="skill_id" label={fieldLabel('skill_id')} value={skill.skill_id} diffs={textDiffs} />
            <SourceTextLine path="basic" field="version" label={fieldLabel('version')} value={skill.version} diffs={textDiffs} />
            <SourceTextLine path="basic" field="business_domain" label={fieldLabel('business_domain')} value={skill.business_domain || '-'} diffs={textDiffs} />
            <SourceTextLine path="basic" field="description" label={fieldLabel('description')} value={skill.description || '-'} diffs={textDiffs} />
            <SourceListLine path="basic" field="trigger_intents" label={fieldLabel('trigger_intents')} values={skill.trigger_intents} diffs={textDiffs} />
            <SourceListLine path="basic" field="user_utterance_examples" label={fieldLabel('user_utterance_examples')} values={skill.user_utterance_examples} diffs={textDiffs} />
            <SourceListLine path="basic" field="goal" label={fieldLabel('goal')} values={skill.goal} diffs={textDiffs} />
            <SourceListLine path="basic" field="required_info" label={fieldLabel('required_info')} values={skill.required_info} diffs={textDiffs} />
            <SourceListLine path="basic" field="response_rules" label={fieldLabel('response_rules')} values={skill.response_rules} diffs={textDiffs} />
          </div>
        </div>
      </SelectableTarget>
      <div className="skill-source-group-title">详细步骤</div>
      <div className="skill-source-steps">
        {skill.steps.map((step, index) => {
          const stepId = String(step.step_id || `step_${index + 1}`);
          const path = stepTargetPath(index);
          return (
            <SelectableTarget
              key={path}
              className={targetClass('skill-source-section', path, selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
              target={{ path, label: `步骤 ${index + 1}：${step.name || stepId}` }}
              onToggle={onToggle}
            >
              {selectedPaths.includes(path) && <span className="selection-mark"><CheckOutlined /></span>}
              <div className="skill-source-rendered">
                <h3>Step {index + 1}: <InlineDiffText path={path} field="name" value={String(step.name || '-')} diffs={textDiffs} /></h3>
                <div className="skill-source-meta-list">
                  <SourceTextLine path={path} field="step_id" label={fieldLabel('step_id')} value={stepId} diffs={textDiffs} />
                  <SourceTextLine path={path} field="instruction" label={fieldLabel('instruction')} value={String(step.instruction || '-')} diffs={textDiffs} />
                  <SourceListLine path={path} field="expected_user_info" label={fieldLabel('expected_user_info')} values={asStringList(step.expected_user_info)} diffs={textDiffs} />
                  <SourceActionLine path={path} values={asStringList(step.allowed_actions)} diffs={textDiffs} toolDescriptions={toolDescriptions} />
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
  containerRef: RefObject<HTMLDivElement>;
  onToggle: (target: TargetSelection) => void;
}) {
  return (
    <div className="skill-flow" ref={containerRef}>
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
      {skill.steps.map((step, index) => {
        const stepId = String(step.step_id || `step_${index + 1}`);
        const path = stepTargetPath(index);
        const toolActions = asStringList(step.allowed_actions).filter((action) =>
          String(action).startsWith('call_tool:'),
        );
        return (
          <div className="skill-flow-step" key={path}>
            <div className="skill-flow-line" />
            <SelectableTarget
              className={targetClass('skill-flow-node', path, selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
              target={{ path, label: `步骤 ${index + 1}：${step.name || stepId}` }}
              onToggle={onToggle}
            >
              {selectedPaths.includes(path) && <span className="selection-mark"><CheckOutlined /></span>}
              <span>Step {index + 1}</span>
              <strong><InlineDiffText path={path} field="name" value={String(step.name || stepId)} diffs={textDiffs} /></strong>
              <small>{stepId}</small>
              <p><InlineDiffText path={path} field="instruction" value={String(step.instruction || '暂无说明')} diffs={textDiffs} /></p>
              <div className="skill-flow-meta">
                <FlowMetaRow label="期望字段">
                  <PlainChipList values={asStringList(step.expected_user_info)} />
                </FlowMetaRow>
                <FlowMetaRow label="允许动作">
                  <ActionList actions={asStringList(step.allowed_actions)} toolDescriptions={toolDescriptions} />
                </FlowMetaRow>
              </div>
            </SelectableTarget>
            {toolActions.length > 0 && (
              <div className="skill-flow-tools">
                {toolActions.map((action) => (
                  <ActionChip action={String(action)} toolDescriptions={toolDescriptions} className="skill-flow-tool" key={String(action)} />
                ))}
              </div>
            )}
          </div>
        );
      })}
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

function SourceTextLine({
  path,
  field,
  label,
  value,
  diffs,
}: {
  path: string;
  field: string;
  label: string;
  value: string;
  diffs: TextDiffAnimation[];
}) {
  return (
    <div className="skill-source-line">
      <span className="skill-source-key">{label}</span>
      <span className="skill-source-value">
        <InlineDiffText path={path} field={field} value={value} diffs={diffs} />
      </span>
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

function SourceListLine({
  path,
  field,
  label,
  values,
  diffs,
}: {
  path: string;
  field: string;
  label: string;
  values: unknown;
  diffs: TextDiffAnimation[];
}) {
  return (
    <div className="skill-source-line">
      <span className="skill-source-key">{label}</span>
      <span className="skill-source-value">
        <InlineDiffText path={path} field={field} value={joinPlain(values)} diffs={diffs} />
      </span>
    </div>
  );
}

function SourceActionLine({
  path,
  values,
  diffs,
  toolDescriptions,
}: {
  path: string;
  values: string[];
  diffs: TextDiffAnimation[];
  toolDescriptions: ToolDescriptionMap;
}) {
  const activeDiff = diffs.find((diff) => diff.path === path && diff.field === 'allowed_actions');
  return (
    <div className="skill-source-line">
      <span className="skill-source-key">{fieldLabel('allowed_actions')}</span>
      <span className="skill-source-value">
        {activeDiff ? (
          <ActionDiffList diff={activeDiff} currentActions={values} toolDescriptions={toolDescriptions} />
        ) : (
          <ActionList actions={values} toolDescriptions={toolDescriptions} />
        )}
      </span>
    </div>
  );
}

function ActionDiffList({
  diff,
  currentActions,
  toolDescriptions,
}: {
  diff: TextDiffAnimation;
  currentActions: string[];
  toolDescriptions: ToolDescriptionMap;
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
          className="removed"
          key={`removed_${action}_${index}`}
        />
      ))}
      {visibleActions.map((action, index) => (
        <ActionChip
          action={action}
          toolDescriptions={toolDescriptions}
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
}: {
  actions: string[];
  toolDescriptions: ToolDescriptionMap;
}) {
  if (actions.length === 0) return <span className="skill-action-empty">-</span>;
  return (
    <div className="skill-action-list">
      {actions.map((action, index) => (
        <ActionChip
          action={action}
          toolDescriptions={toolDescriptions}
          key={`${action}_${index}`}
        />
      ))}
    </div>
  );
}

function ActionChip({
  action,
  toolDescriptions,
  className = '',
}: {
  action: string;
  toolDescriptions: ToolDescriptionMap;
  className?: string;
}) {
  const toolName = toolNameFromAction(action);
  const description = toolName ? toolDescriptions[toolName] || '当前工具配置中暂无描述' : '';
  const chip = (
    <span
      className={`skill-action-chip ${toolName ? 'tool' : ''} ${className}`.trim()}
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
}: {
  diff: TextDiffAnimation;
  toolDescriptions: ToolDescriptionMap;
}) {
  if (diff.field === 'allowed_actions') {
    const removedActions = actionsFromDiffText(diffFullOldValue(diff));
    const insertedActions = actionsFromDiffText(diffFullNewValue(diff));
    return (
      <>
        {diff.removed && (
          <div className="save-review-action-diff old">
            <span className="save-review-diff-sign">-</span>
            <ActionList actions={removedActions} toolDescriptions={toolDescriptions} />
          </div>
        )}
        {diff.inserted && (
          <div className="save-review-action-diff new">
            <span className="save-review-diff-sign">+</span>
            <ActionList actions={insertedActions} toolDescriptions={toolDescriptions} />
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
    steps: [],
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
  const steps = extractStepPreview(source);
  if (steps.length > 0) next.steps = steps;
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
      steps: Array.isArray(draft.steps) ? draft.steps.filter(isRecord).map(normalizeStepPreview) : [],
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

function extractStepPreview(source: string): Array<Record<string, unknown>> {
  const fragments = extractObjectFragmentsFromArrayField(source, 'steps');
  return fragments
    .map((fragment, index) => parseStepFragment(fragment, index))
    .filter((step): step is Record<string, unknown> => Boolean(step));
}

function parseStepFragment(fragment: string, index: number): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(fragment) as unknown;
    if (isRecord(parsed)) return normalizeStepPreview(parsed, index);
  } catch {
    // Partial object: fall through to field extraction.
  }
  const stepId = extractJsonStringField(fragment, 'step_id') || '';
  const name = extractJsonStringField(fragment, 'name') || '';
  const instruction = extractJsonStringField(fragment, 'instruction') || '';
  const expectedUserInfo = extractJsonStringArrayField(fragment, 'expected_user_info') || [];
  const allowedActions = extractJsonStringArrayField(fragment, 'allowed_actions') || [];
  if (!stepId && !name && !instruction && expectedUserInfo.length === 0 && allowedActions.length === 0) {
    return null;
  }
  return {
    step_id: stepId || `step_${index + 1}`,
    name: name || stepId || `步骤 ${index + 1}`,
    instruction,
    expected_user_info: expectedUserInfo,
    allowed_actions: allowedActions,
  };
}

function normalizeStepPreview(step: Record<string, unknown>, index = 0): Record<string, unknown> {
  const stepId = stringValue(step.step_id, `step_${index + 1}`);
  return {
    step_id: stepId,
    name: stringValue(step.name, stepId),
    instruction: stringValue(step.instruction, ''),
    expected_user_info: asStringList(step.expected_user_info),
    allowed_actions: asStringList(step.allowed_actions),
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

function buildOutgoingText(input: string, attachments: UploadAttachment[]): string {
  const text = input.trim();
  const attachmentText = attachments
    .filter((item) => item.status === 'ready' && item.text?.trim())
    .map((item) => `文件：${item.name}\n${item.text?.trim() || ''}`)
    .join('\n\n');
  return [text, attachmentText ? `上传文档内容：\n${attachmentText}` : ''].filter(Boolean).join('\n\n');
}

function buildDisplayText(input: string, attachments: UploadAttachment[]): string {
  const text = input.trim();
  const fileNames = attachments
    .filter((item) => item.status === 'ready')
    .map((item) => item.name)
    .join('、');
  if (text && fileNames) return `${text}\n\n附件：${fileNames}`;
  if (fileNames) return `附件：${fileNames}`;
  return text;
}

function normalizeToolSuggestions(value: unknown): ToolSuggestionItem[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => isRecord(item))
    .map((item) => ({
      name: String(item.name || '').trim(),
      display_name: typeof item.display_name === 'string' ? item.display_name : undefined,
      description: typeof item.description === 'string' ? item.description : undefined,
      method: typeof item.method === 'string' ? item.method : 'POST',
      url: typeof item.url === 'string' ? item.url : '',
      input_schema: isRecord(item.input_schema) ? item.input_schema : {},
      output_schema: isRecord(item.output_schema) ? item.output_schema : {},
      reason: typeof item.reason === 'string' ? item.reason : '',
      status: 'pending' as const,
    }))
    .filter((item) => item.name);
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
    return `未配置工具 ${toolName}，已生成新增建议。`;
  }
  if (text.includes('没有任何工具支持') || (text.includes('available_tools') && text.includes('工具'))) {
    return '缺少可用工具，需先新增工具后再执行该流程。';
  }
  const replacements: Array<[string, string]> = [
    ['原始改写未包含工具步骤，已按可用工具补充闭环执行步骤。', '已补充工具执行步骤。'],
    ['原始改写缺少执行前确认步骤，已补充确认步骤。', '已补充执行前确认步骤。'],
    ['原始改写缺少最终回复步骤，已补充闭环反馈步骤。', '已补充最终回复步骤。'],
    ['模型未生成步骤，已使用规则生成默认步骤。', '已生成默认步骤。'],
  ];
  const matched = replacements.find(([source]) => source === text);
  if (matched) return matched[1];
  return text.length > 82 ? `${text.slice(0, 82)}...` : text;
}

function compactWarningItems(warnings: string[]): Array<{ text: string; title: string }> {
  const items: Array<{ text: string; title: string }> = [];
  for (const warning of warnings) {
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
    ...skill.steps.map((_step, index) => stepTargetPath(index)),
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
  _highlightedPaths: string[],
  _updatingPaths: string[],
  dirtyPaths: string[],
): string {
  return [
    baseClass,
    selectedPaths.includes(path) ? 'active' : '',
    dirtyPaths.includes(path) ? 'dirty' : '',
  ].filter(Boolean).join(' ');
}

function mergePaths(current: string[], next: string[]): string[] {
  return Array.from(new Set([...current, ...next]));
}

function cloneSkill(skill: SkillCard): SkillCard {
  return JSON.parse(JSON.stringify(skill)) as SkillCard;
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
  blank.steps = skill.steps.map((step) => ({
    ...step,
    step_id: '',
    name: '',
    instruction: '',
    expected_user_info: [],
    allowed_actions: [],
  }));
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
  return JSON.stringify(skill.steps[stepIndex] || null);
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
    ['step_id', 'name', 'instruction', 'expected_user_info', 'allowed_actions'].forEach((field) => {
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
      : skill.steps[stepIndexFromPath(path) ?? -1]?.[field];
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
  if (stepIndex === null || !skill.steps[stepIndex]) return;
  skill.steps[stepIndex][field] = value;
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

function buildToolDescriptionMap(tools: ToolRead[]): ToolDescriptionMap {
  return tools.reduce<ToolDescriptionMap>((acc, tool) => {
    acc[tool.name] = [tool.display_name, tool.description].filter(Boolean).join('：') || tool.name;
    return acc;
  }, {});
}

function toolPayloadFromSuggestion(suggestion: ToolSuggestionItem, skillId?: string): Record<string, unknown> {
  return {
    tenant_id: TENANT_ID,
    name: suggestion.name,
    display_name: suggestion.display_name || suggestion.name,
    description: suggestion.description || suggestion.reason || '',
    method: suggestion.method || 'POST',
    url: suggestion.url || `/api/mock/${suggestion.name.replace(/\./g, '/')}`,
    headers: {},
    auth: {},
    input_schema: suggestion.input_schema || {},
    output_schema: suggestion.output_schema || {},
    allowed_skills: skillId ? [skillId] : [],
    enabled: true,
  };
}

function toolReadFromSuggestion(suggestion: ToolSuggestionItem, skillId?: string): ToolRead {
  return {
    id: suggestion.name,
    tenant_id: TENANT_ID,
    name: suggestion.name,
    display_name: suggestion.display_name || suggestion.name,
    description: suggestion.description || suggestion.reason || '',
    method: suggestion.method || 'POST',
    url: suggestion.url || `/api/mock/${suggestion.name.replace(/\./g, '/')}`,
    headers: {},
    auth: {},
    input_schema: suggestion.input_schema || {},
    output_schema: suggestion.output_schema || {},
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

function buildToolIntegrationInstruction(suggestion: ToolSuggestionItem): string {
  const displayName = suggestion.display_name || suggestion.name;
  const toolDetail = {
    name: suggestion.name,
    display_name: displayName,
    description: suggestion.description || '',
    input_schema: suggestion.input_schema || {},
    output_schema: suggestion.output_schema || {},
  };
  return [
    `工具「${displayName}」（${suggestion.name}）已经新增到工具配置。`,
    `请更新当前技能：判断该工具应接入哪些步骤，并只在确实需要调用该工具的步骤中加入 allowed_actions: call_tool:${suggestion.name}。`,
    '同步改写对应步骤说明，使模型在参数满足时调用该工具，并根据工具结果继续推进或给出最终回复；不要修改无关字段。',
    `工具详情：${JSON.stringify(toolDetail, null, 2)}`,
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
    step_id: '步骤 ID',
    instruction: '步骤说明',
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
      const step = index >= 0 ? skill.steps[index] : null;
      return step ? `步骤 ${index + 1}：${step.name || step.step_id || path}` : path;
    }
    return path;
  });
  return labels.join('、');
}

function stepTargetPath(index: number): string {
  return `steps[${index}]`;
}

function stepIndexFromPath(path: string): number | null {
  const match = path.match(/^steps\[(\d+)\]$/);
  return match ? Number(match[1]) : null;
}
