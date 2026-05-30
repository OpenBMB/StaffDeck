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
import { Alert, Button, Card, Empty, Input, Space, Typography, message } from 'antd';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
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
  const [textDiffs, setTextDiffs] = useState<TextDiffAnimation[]>([]);
  const [pendingChange, setPendingChange] = useState<PendingChange | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('source');
  const [loading, setLoading] = useState(false);
  const [streamStatus, setStreamStatus] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const animationTimersRef = useRef<number[]>([]);

  useEffect(() => {
    if (!skillId) {
      setDraft(null);
      setLoadedSkill(null);
      setWarnings([]);
      setSelectedPaths(DEFAULT_TARGET_PATHS);
      setPendingChange(null);
      setHighlightedPaths([]);
      setUpdatingPaths([]);
      setTextDiffs([]);
      return;
    }
    api
      .get<SkillRead>(`/api/enterprise/skills/${encodeURIComponent(skillId)}?tenant_id=${TENANT_ID}`)
      .then((result) => {
        setDraft(result.content);
        setLoadedSkill(result);
        setSelectedPaths(DEFAULT_TARGET_PATHS);
        setPendingChange(null);
        setHighlightedPaths([]);
        setUpdatingPaths([]);
        setTextDiffs([]);
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
      thinkingDetails: ['准备生成技能草稿'],
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
            setDraft(draftSkill);
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
      thinkingDetails: [`改写范围：${scopeLabel}`, '准备发送模型改写请求'],
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

  async function saveDraft() {
    if (!draft) return;
    try {
      if (loadedSkill) {
        await api.put(`/api/enterprise/skills/${loadedSkill.skill_id}`, {
          tenant_id: TENANT_ID,
          content: draft,
          status: loadedSkill.status,
        });
      } else {
        try {
          await api.post('/api/enterprise/skills', { tenant_id: TENANT_ID, content: draft, status: 'draft' });
        } catch (error) {
          if (!(error instanceof Error) || !error.message.includes('409')) throw error;
          await api.put(`/api/enterprise/skills/${draft.skill_id}`, {
            tenant_id: TENANT_ID,
            content: draft,
            status: 'draft',
          });
        }
      }
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

  function animateDraftChange(previousDraft: SkillCard, nextDraft: SkillCard, changedPaths: string[]) {
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
          const clearTimer = window.setTimeout(() => {
            setHighlightedPaths([]);
            setTextDiffs([]);
          }, 1800);
          animationTimersRef.current.push(clearTimer);
        }
      }, 38);
      animationTimersRef.current.push(interval);
    }, 520);
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
            <Button disabled={!draft || loading} icon={<SaveOutlined />} onClick={saveDraft}>
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
              textDiffs={textDiffs}
              onToggle={toggleTarget}
            />
          ) : (
            <SkillFlow
              skill={draft}
              selectedPaths={selectedPaths}
              highlightedPaths={highlightedPaths}
              updatingPaths={updatingPaths}
              textDiffs={textDiffs}
              onToggle={toggleTarget}
            />
          )}
        </Card>
      </div>
    </>
  );
}

function SkillSource({
  skill,
  selectedPaths,
  highlightedPaths,
  updatingPaths,
  textDiffs,
  onToggle,
}: {
  skill: SkillCard;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  textDiffs: TextDiffAnimation[];
  onToggle: (target: TargetSelection) => void;
}) {
  return (
    <div className="skill-source-md">
      <div className="skill-source-group-title">基础信息</div>
      <button
        type="button"
        className={targetClass('skill-source-section', 'basic', selectedPaths, highlightedPaths, updatingPaths)}
        onClick={() => onToggle({ path: 'basic', label: '基础信息' })}
      >
        {selectedPaths.includes('basic') && <span className="selection-mark"><CheckOutlined /></span>}
        <div className="skill-source-code">
          <div className="skill-source-line"># <InlineDiffText path="basic" field="name" value={skill.name} diffs={textDiffs} /></div>
          <SourceTextLine path="basic" field="skill_id" label="- skill_id: `" value={skill.skill_id} suffix="`" diffs={textDiffs} />
          <SourceTextLine path="basic" field="version" label="- version: `" value={skill.version} suffix="`" diffs={textDiffs} />
          <SourceTextLine path="basic" field="business_domain" label="- business_domain: " value={skill.business_domain || '-'} diffs={textDiffs} />
          <SourceTextLine path="basic" field="description" label="- description: " value={skill.description || '-'} diffs={textDiffs} />
          <SourceListLine label="- trigger_intents: " values={skill.trigger_intents} />
          <SourceListLine label="- user_utterance_examples: " values={skill.user_utterance_examples} />
          <SourceListLine label="- goal: " values={skill.goal} />
          <SourceListLine label="- required_info: " values={skill.required_info} />
          <SourceListLine label="- response_rules: " values={skill.response_rules} />
        </div>
      </button>
      <div className="skill-source-group-title">详细步骤</div>
      <div className="skill-source-steps">
        {skill.steps.map((step, index) => {
          const stepId = String(step.step_id || `step_${index + 1}`);
          const path = stepTargetPath(index);
          return (
            <button
              type="button"
              key={path}
              className={targetClass('skill-source-section', path, selectedPaths, highlightedPaths, updatingPaths)}
              onClick={() => onToggle({ path, label: `步骤 ${index + 1}：${step.name || stepId}` })}
            >
              {selectedPaths.includes(path) && <span className="selection-mark"><CheckOutlined /></span>}
              <div className="skill-source-code">
                <div className="skill-source-line">
                  ### Step {index + 1}: <InlineDiffText path={path} field="name" value={String(step.name || '-')} diffs={textDiffs} />
                </div>
                <SourceTextLine path={path} field="step_id" label="- step_id: `" value={stepId} suffix="`" diffs={textDiffs} />
                <SourceTextLine path={path} field="instruction" label="- instruction: " value={String(step.instruction || '-')} diffs={textDiffs} />
                <SourceListLine label="- expected_user_info: " values={asStringList(step.expected_user_info)} />
                <SourceListLine label="- allowed_actions: " values={asStringList(step.allowed_actions)} />
              </div>
            </button>
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
  textDiffs,
  onToggle,
}: {
  skill: SkillCard;
  selectedPaths: string[];
  highlightedPaths: string[];
  updatingPaths: string[];
  textDiffs: TextDiffAnimation[];
  onToggle: (target: TargetSelection) => void;
}) {
  return (
    <div className="skill-flow">
      <button
        type="button"
        className={targetClass('skill-flow-node root', 'basic', selectedPaths, highlightedPaths, updatingPaths)}
        onClick={() => onToggle({ path: 'basic', label: '基础信息' })}
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
      </button>
      {skill.steps.map((step, index) => {
        const stepId = String(step.step_id || `step_${index + 1}`);
        const path = stepTargetPath(index);
        const toolActions = asStringList(step.allowed_actions).filter((action) =>
          String(action).startsWith('call_tool:'),
        );
        return (
          <div className="skill-flow-step" key={path}>
            <div className="skill-flow-line" />
            <button
              type="button"
              className={targetClass('skill-flow-node', path, selectedPaths, highlightedPaths, updatingPaths)}
              onClick={() => onToggle({ path, label: `步骤 ${index + 1}：${step.name || stepId}` })}
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
            </button>
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

function SourceListLine({ label, values }: { label: string; values: string[] | undefined }) {
  return (
    <div className="skill-source-line">
      <span>{label}</span>
      <span>{joinList(values)}</span>
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

function joinList(values: string[] | undefined): string {
  return values && values.length > 0 ? values.map((item) => `\`${item}\``).join(', ') : '-';
}

function joinPlain(values: string[] | undefined): string {
  return values && values.length > 0 ? values.join('、') : '-';
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
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
): string {
  return [
    baseClass,
    selectedPaths.includes(path) ? 'active' : '',
  ].filter(Boolean).join(' ');
}

function cloneSkill(skill: SkillCard): SkillCard {
  return JSON.parse(JSON.stringify(skill)) as SkillCard;
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
      ['skill_id', 'name', 'version', 'business_domain', 'description'].forEach((field) => {
        const diff = makeTextDiff(path, field, getTextField(previousDraft, path, field), getTextField(nextDraft, path, field));
        if (diff) diffs.push(diff);
      });
      return;
    }
    const stepIndex = stepIndexFromPath(path);
    if (stepIndex === null) return;
    ['step_id', 'name', 'instruction'].forEach((field) => {
      const diff = makeTextDiff(path, field, getTextField(previousDraft, path, field), getTextField(nextDraft, path, field));
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

function getTextField(skill: SkillCard, path: string, field: string): string {
  if (path === 'basic') {
    const value = (skill as unknown as Record<string, unknown>)[field];
    return typeof value === 'string' ? value : '';
  }
  const stepIndex = stepIndexFromPath(path);
  if (stepIndex === null) return '';
  const value = (skill.steps[stepIndex] || {})[field];
  return typeof value === 'string' ? value : '';
}

function setTextField(skill: SkillCard, path: string, field: string, value: string): void {
  if (path === 'basic') {
    (skill as unknown as Record<string, unknown>)[field] = value;
    return;
  }
  const stepIndex = stepIndexFromPath(path);
  if (stepIndex === null || !skill.steps[stepIndex]) return;
  skill.steps[stepIndex][field] = value;
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
