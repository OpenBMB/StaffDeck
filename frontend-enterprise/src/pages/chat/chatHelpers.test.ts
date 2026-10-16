import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import type { ChatMessage } from '@/types';

import {
  STREAM_TERMINAL_EVENTS,
  MarkdownMessage,
  a2uiFormForMessage,
  canRateMessage,
  formatTraceDuration,
  harnessEventTraceLine,
  harnessWorkspaceArtifacts,
  knowledgeCitations,
  knowledgeCitationsHidden,
  messageAttachments,
  renderInlineMarkdown,
  scheduledDraftForMessage,
  shouldDeferPersistedEventToLiveStream,
  slotSubmissionText,
  stripTrailingCitationSummary,
} from './chatHelpers';

function message(patch: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'msg-assistant-1',
    role: 'assistant',
    content: 'Answer [1] and [2]',
    created_at: '2026-07-27T00:00:00Z',
    metadata: {},
    ...patch,
  };
}

describe('formatTraceDuration', () => {
  it('renders sub-minute durations with one decimal in seconds', () => {
    expect(formatTraceDuration(0)).toBe('0.0s');
    expect(formatTraceDuration(420)).toBe('0.4s');
    expect(formatTraceDuration(3240)).toBe('3.2s');
    expect(formatTraceDuration(59900)).toBe('59.9s');
  });

  it('renders minute-scale durations as minutes plus zero-padded seconds', () => {
    expect(formatTraceDuration(60000)).toBe('1m 00s');
    expect(formatTraceDuration(83000)).toBe('1m 23s');
    expect(formatTraceDuration(605_000)).toBe('10m 05s');
  });

  it('returns empty label for invalid inputs', () => {
    expect(formatTraceDuration(Number.NaN)).toBe('');
    expect(formatTraceDuration(-5)).toBe('');
  });
});

describe('chat history consumer contract', () => {
  it('keeps inline citations but removes duplicate trailing citation summaries', () => {
    const content = [
      '请假制度按员工手册执行[1]，办公用品按行政手册执行[5]。',
      '',
      '## 参考来源',
      '',
      '- [1] 人事-员工手册与假期政策：事假',
      '- [5] 行政-行政服务手册：办公用品申领',
      '',
      '参考来源：[1] [5]',
    ].join('\n');

    expect(stripTrailingCitationSummary(content)).toBe(
      '请假制度按员工手册执行[1]，办公用品按行政手册执行[5]。',
    );
  });

  it('removes a trailing citation-label footer without changing the answer', () => {
    expect(stripTrailingCitationSummary('正文保留[1]。\n\n参考资料：[1] [2]')).toBe(
      '正文保留[1]。',
    );
  });

  it('preserves ordinary prose that mentions a source', () => {
    const content = '参考来源：员工手册，具体以最新制度为准。';
    expect(stripTrailingCitationSummary(content)).toBe(content);
  });

  it('continues top-level process numbering across blank lines and bullet details', () => {
    const rendered = renderToStaticMarkup(
      createElement(MarkdownMessage, {
        content: [
          '## 用印审批流程指引',
          '',
          '1. **申请入口**：登录审批系统',
          '',
          '1. **填写表单**：填写以下字段',
          '',
          '- 我方主体名称',
          '- 申请日期',
          '',
          '1. **审批流程**：提交申请',
          '',
          '- 直属上级审批',
          '',
          '1. **用印办理**：前往办公室盖章',
        ].join('\n'),
      }),
    );

    expect(rendered.match(/<ol(?: start="\d+")?>/g)).toEqual([
      '<ol>',
      '<ol start="2">',
      '<ol start="3">',
      '<ol start="4">',
    ]);
  });

  it('restarts an ordered list after regular paragraph content', () => {
    const rendered = renderToStaticMarkup(
      createElement(MarkdownMessage, {
        content: ['1. 第一组', '', '这是新的正文段落。', '', '1. 第二组'].join('\n'),
      }),
    );

    expect(rendered.match(/<ol(?: start="\d+")?>/g)).toEqual(['<ol>', '<ol>']);
  });

  it('renders bare HTTP links without changing existing Markdown links or inline code', () => {
    const rendered = renderToStaticMarkup(
      createElement(
        'div',
        null,
        ...renderInlineMarkdown(
          '详情见 https://example.com/docs?a=1。[官网](https://example.org) `https://internal.test`',
          'test',
        ),
      ),
    );

    expect(rendered).toContain(
      '<a href="https://example.com/docs?a=1" target="_blank" rel="noreferrer">https://example.com/docs?a=1</a>。',
    );
    expect(rendered).toContain(
      '<a href="https://example.org/" target="_blank" rel="noreferrer">官网</a>',
    );
    expect(rendered).toContain('<code>https://internal.test</code>');
    expect(rendered.match(/href=/g)).toHaveLength(2);
  });

  it('makes www links clickable by adding a safe HTTPS target', () => {
    const rendered = renderToStaticMarkup(
      createElement(
        'div',
        null,
        ...renderInlineMarkdown('随便给你一个网站：www.baidu.com。', 'test-www'),
      ),
    );

    expect(rendered).toContain(
      '<a href="https://www.baidu.com/" target="_blank" rel="noreferrer">www.baidu.com</a>。',
    );
  });

  it('does not turn unsafe or malformed Markdown targets into external links', () => {
    const rendered = renderToStaticMarkup(
      createElement(
        'div',
        null,
        ...renderInlineMarkdown(
          '[unsafe](javascript:alert(1)) [broken](https://[invalid) [safe](https://example.com/docs)',
          'test-safe-links',
        ),
      ),
    );

    expect(rendered).not.toContain('href="javascript:');
    expect(rendered).not.toContain('href="https://[invalid');
    expect(rendered).toContain(
      '<a href="https://example.com/docs" target="_blank" rel="noreferrer">safe</a>',
    );
  });

  it('renders safe Markdown images and keeps unsafe image targets as text', () => {
    const rendered = renderToStaticMarkup(
      createElement(MarkdownMessage, {
        content: [
          '![趋势图](https://images.example.com/chart.png)',
          '',
          '![危险图片](javascript:alert(1))',
        ].join('\n'),
      }),
    );

    expect(rendered).toContain('src="https://images.example.com/chart.png"');
    expect(rendered).toContain('alt="趋势图"');
    expect(rendered).toContain('referrerPolicy="no-referrer"');
    expect(rendered).toContain('aria-label="查看图片：趋势图"');
    expect(rendered).toContain('危险图片');
    expect(rendered).not.toContain('危险图片)');
    expect(rendered).not.toContain('src="javascript:');
  });

  it('prefers inline citations, deduplicates content, and falls back to source metadata', () => {
    const item = message({
      metadata: {
        knowledge_citations: [
          { id: 'citation-2', label: '[2]', title: 'Refund policy' },
          { id: 'citation-duplicate', label: '[1]', title: 'Purchase policy' },
          { id: 'citation-1', label: '[1]', title: 'Purchase policy' },
          { id: 'citation-unused', label: '[3]', title: 'Unused policy' },
        ],
      },
    });

    expect(knowledgeCitations(item, item.content)).toEqual([
      expect.objectContaining({ id: 'citation-duplicate', label: '[1]' }),
      expect.objectContaining({ id: 'citation-2', label: '[2]' }),
    ]);
    expect(knowledgeCitations(item, 'No inline citation markers')).toEqual([
      expect.objectContaining({ id: 'citation-duplicate', label: '[1]' }),
      expect.objectContaining({ id: 'citation-2', label: '[2]' }),
      expect.objectContaining({ id: 'citation-unused', label: '[3]' }),
    ]);
  });

  it('merges citations that resolve to the same source title into one card', () => {
    const item = message({
      content: 'Answer [1], [2] and [3]',
      metadata: {
        knowledge_citations: [
          { id: 'citation-1', chunk_id: 'chunk-1', label: '[1]', title: '同一制度', content: '片段一' },
          { id: 'citation-2', chunk_id: 'chunk-2', label: '[2]', title: '同一制度', content: '片段二' },
          { id: 'citation-3', chunk_id: 'chunk-3', label: '[3]', title: '同一制度', content: '片段三' },
        ],
      },
    });

    const merged = knowledgeCitations(item, item.content);
    expect(merged).toHaveLength(1);
    expect(merged[0]).toEqual(
      expect.objectContaining({
        id: 'citation-1',
        label: '[1]',
        mergedCount: 3,
        chunkIds: ['chunk-1', 'chunk-2', 'chunk-3'],
      }),
    );
    expect(merged[0].mergedChunks).toEqual([
      expect.objectContaining({ label: '[1]', excerpt: '片段一' }),
      expect.objectContaining({ label: '[2]', excerpt: '片段二' }),
      expect.objectContaining({ label: '[3]', excerpt: '片段三' }),
    ]);
  });

  it('keeps distinct source titles as separate cards', () => {
    const item = message({
      metadata: {
        knowledge_citations: [
          { id: 'citation-1', chunk_id: 'chunk-1', label: '[1]', title: '报销制度' },
          { id: 'citation-2', chunk_id: 'chunk-2', label: '[2]', title: '差旅制度' },
        ],
      },
    });

    expect(knowledgeCitations(item, item.content)).toEqual([
      expect.objectContaining({ id: 'citation-1', label: '[1]', mergedCount: 1 }),
      expect.objectContaining({ id: 'citation-2', label: '[2]', mergedCount: 1 }),
    ]);
  });

  it('collapses identical chunk passages into a single copy of the content', () => {
    const item = message({
      content: 'Answer [1], [2], [3] and [4]',
      metadata: {
        knowledge_citations: [
          { id: 'citation-1', chunk_id: 'chunk-1', label: '[1]', title: '反商业贿赂', content: '严禁商业贿赂' },
          { id: 'citation-2', chunk_id: 'chunk-2', label: '[2]', title: '反商业贿赂', content: '严禁商业贿赂' },
          { id: 'citation-3', chunk_id: 'chunk-3', label: '[3]', title: '反商业贿赂', content: '严禁商业贿赂' },
          { id: 'citation-4', chunk_id: 'chunk-4', label: '[4]', title: '反商业贿赂', content: '严禁商业贿赂' },
        ],
      },
    });

    const merged = knowledgeCitations(item, item.content);
    expect(merged).toHaveLength(1);
    expect(merged[0]).toEqual(
      expect.objectContaining({
        id: 'citation-1',
        label: '[1]',
        // Badge now reflects distinct passages, not raw chunk count.
        mergedCount: 1,
        chunkIds: ['chunk-1', 'chunk-2', 'chunk-3', 'chunk-4'],
      }),
    );
    // The four identical passages collapse to a single rendered copy.
    expect(merged[0].mergedChunks).toHaveLength(1);
    expect(merged[0].mergedChunks?.[0]).toEqual(
      expect.objectContaining({ label: '[1]', excerpt: '严禁商业贿赂' }),
    );
  });

  it('keeps every merged label so an inline [n] always resolves to a card', () => {
    const item = message({
      content: 'Answer [2] and [3]',
      metadata: {
        knowledge_citations: [
          {
            id: 'kref_1',
            chunk_id: 'chunk-1',
            label: '[2]',
            title: 'PDF 文档 / 第 4 页',
            document_id: 'kdoc_a',
            display_key: 'doc:kdoc_a#PDF 文档 / 第 4 页',
          },
          {
            id: 'kref_2',
            chunk_id: 'chunk-2',
            label: '[3]',
            title: 'PDF 文档 / 第 4 页',
            document_id: 'kdoc_a',
            display_key: 'doc:kdoc_a#PDF 文档 / 第 4 页',
          },
        ],
      },
    });

    const merged = knowledgeCitations(item, item.content);
    expect(merged).toHaveLength(1);
    // 正文引用了 [2] 和 [3]，卡片必须同时列出两个号，否则 [3] 找不到落点。
    expect(merged[0].labels).toEqual(['[2]', '[3]']);
    expect(merged[0].label).toBe('[2]');
  });

  it('renumbers merged cards contiguously when the reply has no inline markers', () => {
    const item = message({
      content: '回答里没有引用编号。',
      metadata: {
        knowledge_citations: [
          { id: 'kref_1', label: '[1]', title: 'PDF 文档 / 第 6 页', display_key: 'doc:kdoc_a#第 6 页' },
          { id: 'kref_2', label: '[2]', title: 'PDF 文档 / 第 4 页', display_key: 'doc:kdoc_a#第 4 页' },
          { id: 'kref_3', label: '[3]', title: 'PDF 文档 / 第 4 页', display_key: 'doc:kdoc_a#第 4 页' },
          { id: 'kref_4', label: '[4]', title: 'PDF 文档 / 第 5 页', display_key: 'doc:kdoc_a#第 5 页' },
        ],
      },
    });

    const merged = knowledgeCitations(item, item.content);
    // 第 4 页的两条合并后不留下空号：[1][2][3] 而不是 [1][2][4]。
    expect(merged.map((citation) => citation.label)).toEqual(['[1]', '[2]', '[3]']);
    expect(merged[1].labels).toEqual(['[2]']);
    expect(merged[1].mergedChunks).toHaveLength(2);
  });

  it('keeps same-titled pages from different documents apart', () => {
    const item = message({
      content: '回答里没有引用编号。',
      metadata: {
        knowledge_citations: [
          {
            id: 'kref_1',
            label: '[1]',
            title: 'PDF 文档 / 第 3 页',
            document_id: 'kdoc_a',
            display_key: 'doc:kdoc_a#第 3 页',
          },
          {
            id: 'kref_2',
            label: '[2]',
            title: 'PDF 文档 / 第 3 页',
            document_id: 'kdoc_b',
            display_key: 'doc:kdoc_b#第 3 页',
          },
        ],
      },
    });

    expect(knowledgeCitations(item, item.content)).toHaveLength(2);
  });

  it('falls back to source path grouping for citations without a display key', () => {
    const item = message({
      content: '回答里没有引用编号。',
      metadata: {
        knowledge_citations: [
          { id: 'kref_1', label: '[1]', title: 'PDF 文档 / 第 3 页', source_path: 'a.pdf / 第 3 页' },
          { id: 'kref_2', label: '[2]', title: 'PDF 文档 / 第 3 页', source_path: 'b.pdf / 第 3 页' },
        ],
      },
    });

    expect(knowledgeCitations(item, item.content)).toHaveLength(2);
  });

  it('restores scheduled drafts and attachments from persisted metadata', () => {
    const draft = {
      should_create: true,
      tenant_id: 'tenant-demo',
      agent_id: 'agent-demo',
      title: 'Daily price check',
      prompt: 'Check the A1 price',
      schedule_type: 'daily' as const,
      schedule: { time: '09:00' },
      timezone: 'Asia/Shanghai',
      confidence: 1,
    };
    const attachment = {
      id: 'attachment-1',
      filename: 'notes.txt',
      content_type: 'text/plain',
      size: 12,
      kind: 'text' as const,
      text: 'Body text',
    };
    const item = message({
      metadata: ({
        scheduled_task_draft: draft,
        attachments: [attachment, { filename: 'missing-id.txt' }],
      } as unknown) as ChatMessage['metadata'],
    });

    expect(scheduledDraftForMessage(item)).toEqual(draft);
    expect(messageAttachments(item)).toEqual([attachment]);
  });

  it('keeps only valid, unique workspace artifacts from persisted metadata', () => {
    const item = message({
      metadata: {
        harness_artifacts: [
          {
            type: 'workspace_file',
            task_frame_id: 'task-1',
            path: 'reports/result.txt',
            size: 12,
            display_name: '季度报告.txt',
            description: '最终版',
            content_type: 'text/plain',
            source: 'harness',
          },
          {
            type: 'workspace_file',
            task_frame_id: 'task-1',
            path: 'reports/result.txt',
            size: 14,
          },
          { type: 'human_handoff', handoff_id: 'handoff-1' },
          { type: 'workspace_file', task_frame_id: '', path: 'invalid.txt' },
        ],
      },
    });

    expect(harnessWorkspaceArtifacts(item)).toEqual([
      {
        type: 'workspace_file',
        task_frame_id: 'task-1',
        path: 'reports/result.txt',
        size: 12,
        display_name: '季度报告.txt',
        description: '最终版',
        content_type: 'text/plain',
        source: 'harness',
      },
    ]);
  });

  it('allows feedback only for committed assistant messages', () => {
    expect(canRateMessage(message())).toBe(true);
    expect(canRateMessage(message({ isStreaming: true }))).toBe(false);
    expect(canRateMessage(message({ isError: true }))).toBe(false);
    expect(canRateMessage(message({ id: '__streaming__' }))).toBe(false);
    expect(canRateMessage(message({ role: 'user' }))).toBe(false);
  });

  it('locks the legacy frontend terminal vocabulary', () => {
    expect([...STREAM_TERMINAL_EVENTS].sort()).toEqual([
      'complete',
      'done',
      'error',
      'error_occurred',
      'stream_cancelled',
      'stream_end',
      'stream_interrupted',
    ]);
  });

  it('replays persisted assistant and terminal events even when an old live stream owns the turn', () => {
    expect(shouldDeferPersistedEventToLiveStream('stream_delta', true)).toBe(true);
    expect(shouldDeferPersistedEventToLiveStream('assistant_message_created', true)).toBe(false);
    expect(shouldDeferPersistedEventToLiveStream('stream_end', true)).toBe(false);
    expect(shouldDeferPersistedEventToLiveStream('complete', true)).toBe(false);
    expect(shouldDeferPersistedEventToLiveStream('stream_delta', false)).toBe(false);
  });

  it('turns the Harness lifecycle into mergeable execution-record lines', () => {
    const started = harnessEventTraceLine('task_frame_started', {
      task_frame_id: 'task-weather',
      kind: 'conversation',
    });
    const action = harnessEventTraceLine('harness_action_created', {
      task_frame_id: 'task-weather',
      iteration: 1,
      action: 'tool',
      tool_name: 'general_skill.weather',
    });
    const completed = harnessEventTraceLine('harness_tool_completed', {
      task_frame_id: 'task-weather',
      iteration: 1,
      tool_name: 'general_skill.weather',
      success: true,
      result: {
        success: true,
        data: { structured_result: { temperature: 29 } },
      },
    });
    const appView = harnessEventTraceLine('harness_mcp_app_view', {
      task_frame_id: 'task-weather',
      tool_name: 'weather.card',
      mcp_app: {
        server_id: 'server-weather',
        resource_uri: 'ui://weather/card',
        tool_name: 'weather.card',
        visibility: ['model', 'app'],
        mime_type: 'text/html;profile=mcp-app',
      },
    });
    const finished = harnessEventTraceLine('task_frame_finished', {
      task_frame_id: 'task-weather',
      status: 'completed',
      action_count: 2,
    });

    expect(started).toMatchObject({
      id: 'harness_frame_task-weather',
      text: '开始执行任务',
      state: 'running',
    });
    expect(action).toMatchObject({
      id: 'harness_action_task-weather_1',
      text: '调用能力 general_skill.weather',
      state: 'running',
    });
    expect(completed).toMatchObject({
      id: 'harness_action_task-weather_1',
      text: '能力调用完成 general_skill.weather',
      state: 'completed',
      outputLanguage: 'json',
      outputTitle: '查看能力结果',
    });
    expect(completed?.output).toContain('"temperature": 29');
    expect(appView).toMatchObject({
      id: 'harness_mcp_app_task-weather_weather.card',
      text: '展示 MCP App weather.card',
      state: 'completed',
      mcpApp: { resource_uri: 'ui://weather/card' },
    });
    expect(finished).toMatchObject({
      id: 'harness_frame_task-weather',
      text: '任务执行完成',
      state: 'completed',
    });
  });

  it('keeps a switched SOP visible while it waits for user input', () => {
    const started = harnessEventTraceLine('task_frame_started', {
      task_frame_id: 'task-purchase',
      kind: 'sop',
      skill_id: 'skill_purchase_001',
      skill_name: '购买商品流程',
      step_id: 'collect_user_name',
    });
    const finished = harnessEventTraceLine('task_frame_finished', {
      task_frame_id: 'task-purchase',
      kind: 'sop',
      skill_id: 'skill_purchase_001',
      skill_name: '购买商品流程',
      step_id: 'collect_user_name',
      status: 'awaiting_user',
      action_count: 1,
    });

    expect(started).toMatchObject({
      id: 'harness_frame_task-purchase',
      text: '开始SOP 购买商品流程',
      state: 'running',
    });
    expect(finished).toMatchObject({
      id: 'harness_frame_task-purchase',
      kind: 'skill',
      text: '等待用户补充 购买商品流程',
      detail: '状态 awaiting_user · 步骤 collect_user_name · 执行 1 个动作',
      state: 'running',
    });
  });

  it('renders a non-zero command exit as a readable, non-failing trace line', () => {
    const line = harnessEventTraceLine('harness_tool_completed', {
      task_frame_id: 'task-probe',
      iteration: 4,
      tool_name: 'exec_command',
      success: false,
      error: {
        code: 'COMMAND_EXIT_NONZERO',
        message: '命令执行完成，但返回非零退出码（exit 2）。',
        hint: '非零退出码只表示进程自己报告了失败：grep/find/ls …',
        exit_code: 2,
      },
      result: {
        tool_name: 'exec_command',
        success: false,
        data: {
          status: 'failed',
          exit_code: 2,
          timed_out: false,
          stdout: '--- tree ---\n.\n./.harness\n',
          stderr: '',
          command_sha256: 'f242ba02200bdf831b32beff56693dc4e5340427b7d43ca63d0d2cdfc8da89ae',
        },
        error: { code: 'COMMAND_EXIT_NONZERO', message: '受控进程执行完成，但返回了非零退出码。' },
      },
    });

    expect(line).toMatchObject({
      id: 'harness_action_task-probe_4',
      kind: 'tool',
      text: '命令返回非零退出码（exit 2） exec_command',
      state: 'completed',
      outputLanguage: 'text',
      outputTitle: '查看命令输出',
    });
    expect(line?.detail).toContain('非零退出码');
    expect(line?.output).toContain('--- tree ---');
    // 原始结果 JSON 不再作为详情展示
    expect(line?.output).not.toContain('command_sha256');
  });

  it('keeps a timed-out command as a failed trace line', () => {
    const line = harnessEventTraceLine('harness_tool_completed', {
      task_frame_id: 'task-probe',
      iteration: 5,
      tool_name: 'exec_command',
      success: false,
      error: { code: 'COMMAND_TIMEOUT', message: '受控进程执行超时。', hint: '拆分任务' },
      result: {
        tool_name: 'exec_command',
        success: false,
        data: { status: 'failed', exit_code: null, timed_out: true, timeout_seconds: 30, stdout: 'partial', stderr: '' },
      },
    });

    expect(line).toMatchObject({
      text: '命令执行超时 exec_command',
      state: 'failed',
    });
    expect(line?.detail).toContain('COMMAND_TIMEOUT');
    expect(line?.detail).toContain('上限 30 秒');
  });

  it('shows a successful command result as command output instead of raw JSON', () => {
    const line = harnessEventTraceLine('harness_tool_completed', {
      task_frame_id: 'task-probe',
      iteration: 7,
      tool_name: 'exec_command',
      success: true,
      result: {
        tool_name: 'exec_command',
        success: true,
        data: { status: 'completed', exit_code: 0, timed_out: false, stdout: 'NO_TOKEN\n', stderr: '' },
      },
    });

    expect(line).toMatchObject({
      text: '命令执行完成 exec_command',
      detail: 'exit 0',
      state: 'completed',
    });
    expect(line?.output).toBe('# stdout\nNO_TOKEN');
  });

  it('surfaces a bounded model-call retry instead of silent waiting', () => {
    const line = harnessEventTraceLine('harness_action_model_retry', {
      task_frame_id: 'task-probe',
      iteration: 14,
      attempt: 2,
      max_attempts: 2,
      code: 'MODEL_TIMEOUT',
    });

    expect(line).toMatchObject({
      id: 'harness_model_retry_task-probe_14',
      kind: 'thinking',
      text: '模型调用超时，正在重试',
      state: 'running',
    });
    expect(line?.detail).toBe('第 2 次尝试 · 最多 2 次 · MODEL_TIMEOUT');
  });
});

describe('a2uiFormForMessage', () => {
  const payload = {
    kind: 'slot_form',
    skill_id: 'skill_income_cert',
    step_id: 'collect_info',
    step_name: '收集证明需求信息',
    title: '请补充以下信息',
    submit_label: '提交',
    fields: [
      { name: 'employee_id', label: '员工工号', type: 'text', required: true, placeholder: '请输入员工工号' },
      { name: 'include_income', label: '是否含收入项', type: 'boolean', required: true, options: [{ value: true, label: '是' }, { value: false, label: '否' }] },
    ],
  };

  it('解析后端下发的表单描述', () => {
    const form = a2uiFormForMessage(message({ metadata: { a2ui: payload } }));
    expect(form?.kind).toBe('slot_form');
    expect(form?.step_name).toBe('收集证明需求信息');
    expect(form?.fields.map((field) => field.name)).toEqual(['employee_id', 'include_income']);
    expect(form?.fields[1].type).toBe('boolean');
  });

  it('没有表单、kind 不匹配或字段非法时返回 null', () => {
    expect(a2uiFormForMessage(message())).toBeNull();
    expect(a2uiFormForMessage(message({ metadata: { a2ui: { kind: 'other', fields: [] } } }))).toBeNull();
    expect(a2uiFormForMessage(message({ metadata: { a2ui: { kind: 'slot_form', fields: [] } } }))).toBeNull();
    // 未知控件类型退化成文本框，而不是整表单丢弃
    const form = a2uiFormForMessage(message({
      metadata: { a2ui: { kind: 'slot_form', fields: [{ name: 'x', label: 'X', type: 'unknown-widget' }] } },
    }));
    expect(form?.fields[0].type).toBe('text');
  });

  it('表单取值拼成一行人可读文本（提交后仍落一条用户消息）', () => {
    const form = a2uiFormForMessage(message({ metadata: { a2ui: payload } }));
    expect(form).not.toBeNull();
    const text = slotSubmissionText(form!, { employee_id: '3012', include_income: false });
    expect(text).toBe('员工工号：3012；是否含收入项：否');
  });
});

describe('knowledgeCitationsHidden（「知识来源」展示口径）', () => {
  it('默认展示：配置为 true 或字段缺失都展示', () => {
    expect(knowledgeCitationsHidden({ showKnowledgeCitations: true })).toBe(false);
    // 老后端不返回该字段 / 接口结构异常时不能把来源卡片静默吞掉
    expect(knowledgeCitationsHidden({})).toBe(false);
    expect(knowledgeCitationsHidden({ showKnowledgeCitations: undefined })).toBe(false);
  });

  it('超级管理员在租户配置里关掉后不再展示', () => {
    expect(knowledgeCitationsHidden({ showKnowledgeCitations: false })).toBe(true);
  });

  it('分享访客始终不展示，与租户开关无关', () => {
    expect(knowledgeCitationsHidden({ shareMode: true, showKnowledgeCitations: true })).toBe(true);
    expect(knowledgeCitationsHidden({ shareMode: true, showKnowledgeCitations: false })).toBe(true);
  });
});
