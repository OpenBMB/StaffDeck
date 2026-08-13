import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import type { ChatMessage } from '@/types';

import {
  STREAM_TERMINAL_EVENTS,
  MarkdownMessage,
  canRateMessage,
  harnessEventTraceLine,
  harnessWorkspaceArtifacts,
  knowledgeCitations,
  messageAttachments,
  renderInlineMarkdown,
  scheduledDraftForMessage,
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

describe('chat history consumer contract', () => {
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
      '<a href="https://example.org" target="_blank" rel="noreferrer">官网</a>',
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
      '<a href="https://www.baidu.com" target="_blank" rel="noreferrer">www.baidu.com</a>。',
    );
  });

  it('keeps only inline citations, deduplicates content, and orders labels', () => {
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
    expect(knowledgeCitations(item, 'No inline citation markers')).toEqual([]);
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
});
