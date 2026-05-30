import {
  BranchesOutlined,
  CheckOutlined,
  CodeOutlined,
  DownOutlined,
  LoadingOutlined,
  RightOutlined,
  SaveOutlined,
  SendOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { Alert, Button, Card, Empty, Input, Modal, Space, Typography, message } from 'antd';
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, streamPost, TENANT_ID } from '../api/client';
import type { SkillCard, SkillRead } from '../types';

type ChatItem = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  thinking?: 'running' | 'done';
  thinkingDetails?: string[];
  thinkingOpen?: boolean;
  actionState?: 'pending' | 'confirmed' | 'rejected';
};

type TargetSelection = {
  path: string;
  label: string;
};

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

const DEFAULT_TARGET_PATHS = ['basic'];

export default function DistillPage() {
  const [searchParams] = useSearchParams();
  const skillId = searchParams.get('skill_id');
  const [draft, setDraft] = useState<SkillCard | null>(null);
  const [loadedSkill, setLoadedSkill] = useState<SkillRead | null>(null);
  const [lastSavedDraft, setLastSavedDraft] = useState<SkillCard | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [messages, setMessages] = useState<ChatItem[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content: '请粘贴原始技能说明，或点击右侧某一块后告诉我需要怎样改写。',
    },
  ]);
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
  const [viewMode, setViewMode] = useState<ViewMode>('source');
  const [loading, setLoading] = useState(false);
  const [streamStatus, setStreamStatus] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const animationTimersRef = useRef<number[]>([]);

  useEffect(() => {
    if (!skillId) {
      setDraft(null);
      setLoadedSkill(null);
      setLastSavedDraft(null);
      setWarnings([]);
      setSelectedPaths(DEFAULT_TARGET_PATHS);
      setPendingChange(null);
      setHighlightedPaths([]);
      setUpdatingPaths([]);
      setDirtyPaths([]);
      setTextDiffs([]);
      setSaveDraftSnapshot(null);
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
        setSaveDraftSnapshot(null);
        setMessages([
          {
            id: 'loaded',
            role: 'assistant',
            content: `已加载「${result.name}」。你可以在右侧选择一个或多个区域，然后在这里描述需要怎样改写。`,
          },
        ]);
      })
      .catch((error) => message.error(error instanceof Error ? error.message : '加载技能失败'));
  }, [skillId]);

  useEffect(() => () => {
    abortRef.current?.abort();
    clearAnimationTimers();
  }, []);

  const allPaths = useMemo(() => (draft ? allTargetPaths(draft) : DEFAULT_TARGET_PATHS), [draft]);
  const allSelected = draft ? selectedPaths.length > 0 && allPaths.every((path) => selectedPaths.includes(path)) : false;
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
    return collectTextDiffs(baseDraft, saveReviewDraft, changedPaths);
  }, [lastSavedDraft, saveReviewDraft]);

  async function send() {
    const text = input.trim();
    if (!text || loading) return;
    const confirmedDraft = pendingChange?.nextDraft || draft;
    confirmPendingChange(false);
    setInput('');
    pushMessage('user', text);
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
          if (item.event === 'complete') {
            const draftSkill = item.data.draft_skill as SkillCard;
            const nextWarnings = Array.isArray(item.data.warnings) ? item.data.warnings.map(String) : [];
            appendThinkingDetail(assistantId, `已生成技能草稿：${draftSkill.name}`);
            animateDraftChange(blankSkillForAnimation(draftSkill), draftSkill, allTargetPaths(draftSkill), 120);
            setWarnings(nextWarnings);
            setSelectedPaths(DEFAULT_TARGET_PATHS);
            updateMessage(
              assistantId,
              `已生成「${draftSkill.name}」草稿。你可以在右侧选择一个或多个区域继续改写。`,
              { thinking: 'done' },
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

  async function rewriteSelectedTarget(text: string, currentDraft: SkillCard | null = draft) {
    if (!currentDraft) return;
    const previousDraft = cloneSkill(currentDraft);
    const targets = selectedPaths.length > 0 ? selectedPaths : allTargetPaths(currentDraft);
    const scopeLabel = targetLabel(targets, currentDraft);
    setLoading(true);
    setStreamStatus('正在改写选中内容');
    const assistantId = pushMessage('assistant', '', {
      thinking: 'running',
      thinkingDetails: [`改写范围：${scopeLabel}`],
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
            const changedPaths = diffTargetPaths(previousDraft, nextDraft, targets);
            const changedLabel = changedPaths.length > 0 ? targetLabel(changedPaths, nextDraft) : '未检测到结构变化';
            appendThinkingDetail(assistantId, `模型返回改写结果：${changedLabel}`);
            appendThinkingDetail(assistantId, '右侧已更新预览，等待确认或拒绝');
            animateDraftChange(previousDraft, nextDraft, changedPaths);
            setPendingChange({ assistantId, previousDraft, nextDraft, changedPaths });
            setSelectedPaths((current) => reconcileSelectedPaths(current, nextDraft));
            setWarnings(nextWarnings);
            setStreamStatus('改写完成');
            if (!receivedMessageChunk) {
              updateMessage(assistantId, String(item.data.assistant_message || '已完成局部改写。'), {
                thinking: 'done',
                actionState: 'pending',
              });
            } else {
              updateMessage(assistantId, undefined, { thinking: 'done', actionState: 'pending' });
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

  function openSaveReview() {
    const targetDraft = pendingChange?.nextDraft || draft;
    if (!targetDraft) return;
    confirmPendingChange(false);
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
      setDirtyPaths([]);
      setSaveReviewOpen(false);
      message.success('草稿已保存');
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

  function closeSaveReview() {
    setSaveReviewOpen(false);
    setSaveDraftSnapshot(null);
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
    setHighlightedPaths([]);
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
          const clearTimer = window.setTimeout(() => {
            setHighlightedPaths([]);
            setTextDiffs([]);
          }, 1800);
          animationTimersRef.current.push(clearTimer);
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
    <>
      <div className="page-title">
        <Typography.Title level={3}>技能改写</Typography.Title>
      </div>
      <div className="skill-workbench">
        <Card className="skill-chat-card">
          <div className="skill-chat-panel">
            <div className="skill-chat-messages">
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
                    {item.content ? <div>{item.content}</div> : item.thinking === 'running' ? null : '正在处理...'}
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
            <div className="skill-chat-composer">
              <Input.TextArea
                value={input}
                onChange={(event) => setInput(event.target.value)}
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
                  {loading && (
                    <Button icon={<StopOutlined />} onClick={stopStream}>
                      停止
                    </Button>
                  )}
                  <Button type="primary" icon={<SendOutlined />} loading={loading} onClick={() => void send()}>
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
            <Button disabled={!draft || loading} icon={<SaveOutlined />} onClick={openSaveReview}>
              保存草稿
            </Button>
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
          {warnings.map((warning) => (
            <Alert key={warning} type="warning" message={warning} showIcon className="skill-warning" />
          ))}
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
              onToggle={toggleTarget}
            />
          )}
        </Card>
      </div>
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
                {diff.removed && <div><span className="diff-old">- {diff.removed}</span></div>}
                {diff.inserted && <div><span className="diff-new">+ {diff.inserted}</span></div>}
              </div>
            ))
          )}
        </div>
      </Modal>
    </>
  );
}

function SkillSource({
  skill,
  selectedPaths,
  highlightedPaths,
  updatingPaths,
  dirtyPaths,
  textDiffs,
  onToggle,
}: {
  skill: SkillCard;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  dirtyPaths: string[];
  textDiffs: TextDiffAnimation[];
  onToggle: (target: TargetSelection) => void;
}) {
  return (
    <div className="skill-source-md">
      <div className="skill-source-group-title">基础信息</div>
      <SelectableTarget
        className={targetClass('skill-source-section', 'basic', selectedPaths, highlightedPaths, updatingPaths, dirtyPaths)}
        target={{ path: 'basic', label: '基础信息' }}
        onToggle={onToggle}
      >
        {selectedPaths.includes('basic') && <span className="selection-mark"><CheckOutlined /></span>}
        <div className="skill-source-code">
          <div className="skill-source-line"># <InlineDiffText path="basic" field="name" value={skill.name} diffs={textDiffs} /></div>
          <SourceTextLine path="basic" field="skill_id" label="- skill_id: `" value={skill.skill_id} suffix="`" diffs={textDiffs} />
          <SourceTextLine path="basic" field="version" label="- version: `" value={skill.version} suffix="`" diffs={textDiffs} />
          <SourceTextLine path="basic" field="business_domain" label="- business_domain: " value={skill.business_domain || '-'} diffs={textDiffs} />
          <SourceTextLine path="basic" field="description" label="- description: " value={skill.description || '-'} diffs={textDiffs} />
          <SourceListLine path="basic" field="trigger_intents" label="- trigger_intents: " values={skill.trigger_intents} diffs={textDiffs} />
          <SourceListLine path="basic" field="user_utterance_examples" label="- user_utterance_examples: " values={skill.user_utterance_examples} diffs={textDiffs} />
          <SourceListLine path="basic" field="goal" label="- goal: " values={skill.goal} diffs={textDiffs} />
          <SourceListLine path="basic" field="required_info" label="- required_info: " values={skill.required_info} diffs={textDiffs} />
          <SourceListLine path="basic" field="response_rules" label="- response_rules: " values={skill.response_rules} diffs={textDiffs} />
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
              <div className="skill-source-code">
                <div className="skill-source-line">
                  ### Step {index + 1}: <InlineDiffText path={path} field="name" value={String(step.name || '-')} diffs={textDiffs} />
                </div>
                <SourceTextLine path={path} field="step_id" label="- step_id: `" value={stepId} suffix="`" diffs={textDiffs} />
                <SourceTextLine path={path} field="instruction" label="- instruction: " value={String(step.instruction || '-')} diffs={textDiffs} />
                <SourceListLine path={path} field="expected_user_info" label="- expected_user_info: " values={asStringList(step.expected_user_info)} diffs={textDiffs} />
                <SourceListLine path={path} field="allowed_actions" label="- allowed_actions: " values={asStringList(step.allowed_actions)} diffs={textDiffs} />
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
  onToggle,
}: {
  skill: SkillCard;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  dirtyPaths: string[];
  textDiffs: TextDiffAnimation[];
  onToggle: (target: TargetSelection) => void;
}) {
  return (
    <div className="skill-flow">
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
          <em>业务域 {skill.business_domain || '-'}</em>
          <em>必填 {joinPlain(skill.required_info)}</em>
          <em>意图 {joinPlain(skill.trigger_intents)}</em>
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
                <em>字段 {joinPlain(asStringList(step.expected_user_info))}</em>
                <em>动作 {joinPlain(asStringList(step.allowed_actions))}</em>
              </div>
            </SelectableTarget>
            {toolActions.length > 0 && (
              <div className="skill-flow-tools">
                {toolActions.map((action) => (
                  <div className="skill-flow-tool" key={String(action)}>
                    {String(action).replace('call_tool:', '')}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function SourceTextLine({
  path,
  field,
  label,
  value,
  suffix = '',
  diffs,
}: {
  path: string;
  field: string;
  label: string;
  value: string;
  suffix?: string;
  diffs: TextDiffAnimation[];
}) {
  return (
    <div className="skill-source-line">
      <span>{label}</span>
      <InlineDiffText path={path} field={field} value={value} diffs={diffs} />
      <span>{suffix}</span>
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
      <span>{label}</span>
      <InlineDiffText path={path} field={field} value={joinList(values)} diffs={diffs} />
    </div>
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

function parseInitialSkillPrompt(text: string): { title: string; raw_content: string } {
  const titleMatch = text.match(/标题[:：]\s*([^\n，,]+)/);
  const rawMatch = text.match(/原始(?:SOP|技能)?文本[:：]?\s*([\s\S]+)/);
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
  const title = titleMatch?.[1]?.trim() || lines[0]?.slice(0, 32) || '新技能';
  const rawContent = rawMatch?.[1]?.trim() || lines.slice(titleMatch ? 0 : 1).join('\n') || text;
  return { title, raw_content: rawContent };
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
  return `${major}.${minor + 5}.0`;
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
