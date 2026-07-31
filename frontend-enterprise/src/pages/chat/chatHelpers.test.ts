import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import type { ChatMessage } from '@/types';

import {
  STREAM_TERMINAL_EVENTS,
  canRateMessage,
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
});
