// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ChatMessage } from '@/types';

import type { UseChatSession } from '../useChatSession';
import MessageList from './MessageList';

const answerWithCitation: ChatMessage = {
  id: 'msg-assistant-1',
  role: 'assistant',
  content: '身高和体重确实都有要求，部分设备设有体重限制，具体以现场公告为准。[1]',
  created_at: '2026-10-19T00:00:00Z',
  metadata: {
    knowledge_citations: [
      {
        id: 'cite-1',
        label: '[1]',
        title: '乐园手册：乘坐须知',
        document_id: 'doc-1',
        section_path: '第 3 页',
      },
    ],
  },
};

function chatStub(patch: Record<string, unknown> = {}) {
  return {
    displayedMessages: [answerWithCitation],
    turnTraceRef: { current: new Map() },
    uiConfig: { show_thinking_trace: true, show_skill_trace: true, show_tool_trace: true },
    currentStream: {},
    runningTurn: null,
    expandedTraceIds: [],
    collapsedTraceIds: [],
    isCurrentStreamingTrace: () => false,
    dismissedDraftMessageIds: [],
    createdScheduledTasks: {},
    activeConversationId: 'session-1',
    currentScheduledDraft: null,
    hasVisibleMessageScheduledDraft: false,
    confirmScheduledTask: vi.fn(),
    dismissScheduledTaskDraft: vi.fn(),
    chatMessagesRef: { current: null },
    handleChatMessagesScroll: vi.fn(),
    SHOW_DEBUG: false,
    lastTurn: null,
    hideKnowledgeCitations: false,
    setActiveCitation: vi.fn(),
    toggleTrace: vi.fn(),
    rateMessage: vi.fn(),
    removeQueuedTurn: vi.fn(),
    displayedTeam: undefined,
    displayedAgent: undefined,
    slashCommands: [],
    tenantId: 'tenant_demo',
    submitSlotForm: vi.fn(),
    ...patch,
  } as unknown as UseChatSession;
}

describe('MessageList 知识来源开关', () => {
  afterEach(cleanup);

  it('展示来源时保留正文编号与来源卡片', () => {
    render(<MessageList chat={chatStub()} />);

    expect(screen.getByText(/具体以现场公告为准。\[1\]/)).toBeTruthy();
    expect(screen.getByLabelText('知识引用')).toBeTruthy();
  });

  it('全局关掉来源后，正文编号也不再出现', () => {
    render(<MessageList chat={chatStub({ hideKnowledgeCitations: true })} />);

    expect(screen.getByText('身高和体重确实都有要求，部分设备设有体重限制，具体以现场公告为准。')).toBeTruthy();
    expect(screen.queryByText(/\[1\]/)).toBeNull();
    expect(screen.queryByLabelText('知识引用')).toBeNull();
  });
});
