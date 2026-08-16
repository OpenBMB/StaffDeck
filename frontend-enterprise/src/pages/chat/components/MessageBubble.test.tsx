// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { AgentProfileRead, ChatMessage, ChatSlashCommand, TeamRead } from '@/types';

import type { UseChatSession } from '../useChatSession';
import MessageBubble, { type MessageRender } from './MessageBubble';

const weatherCommand: ChatSlashCommand = {
  kind: 'skill',
  target: 'weather',
  label: 'weather',
  description: '查询天气',
  command: '/skill weather',
};

function renderSlashMessage(content: string, slashCommands: ChatSlashCommand[] = [weatherCommand]) {
  const item: ChatMessage = {
    id: 'message-1',
    role: 'user',
    content,
    created_at: '2026-08-09T00:00:00Z',
  };
  const messageRender: MessageRender = {
    traceTurnId: 'turn-1',
    summary: null,
    details: [],
    expanded: false,
    showInlineTrace: false,
    visibleContent: content,
    citations: [],
    scheduledDraft: null,
    scheduledTaskPrompt: false,
    attachments: [],
    harnessArtifacts: [],
    statusOnly: false,
  };
  const chat = {
    slashCommands,
    toggleTrace: vi.fn(),
    rateMessage: vi.fn(),
    setActiveCitation: vi.fn(),
    confirmScheduledTask: vi.fn(),
    dismissScheduledTaskDraft: vi.fn(),
    removeQueuedTurn: vi.fn(),
    tenantId: 'tenant_demo',
    activeConversationId: 'session-1',
  } as unknown as UseChatSession;
  return render(<MessageBubble chat={chat} item={item} render={messageRender} />);
}

describe('MessageBubble slash command card', () => {
  it('renders a sent slash command as a card beside its request text', () => {
    renderSlashMessage('/skill weather 北京天气如何');

    expect(screen.getByRole('group', { name: '技能 weather' })).toBeTruthy();
    expect(screen.getByText('北京天气如何')).toBeTruthy();
    expect(screen.queryByText('/skill weather 北京天气如何')).toBeNull();
  });

  it('keeps the card when the referenced resource is no longer listed', () => {
    renderSlashMessage('/skill archived_weather 北京天气如何', []);

    expect(screen.getByRole('group', { name: '技能 archived_weather' })).toBeTruthy();
  });
});

describe('MessageBubble responsive sizing', () => {
  it('keeps a short user message out of a shrink-to-fit wrapper', () => {
    renderSlashMessage('确认', []);

    const message = screen.getByText('确认');
    const bubble = message.parentElement?.parentElement;
    expect(bubble?.parentElement?.classList.contains('contents')).toBe(true);
  });
});

describe('MessageBubble team group identity', () => {
  it('shows the project leader avatar and name beside assistant messages', () => {
    const item: ChatMessage = {
      id: 'message-team-1',
      role: 'assistant',
      content: '团队回复',
      created_at: '2026-08-15T00:00:00Z',
    };
    const messageRender: MessageRender = {
      traceTurnId: 'turn-team-1',
      summary: null,
      details: [],
      expanded: false,
      showInlineTrace: false,
      visibleContent: item.content,
      citations: [],
      scheduledDraft: null,
      scheduledTaskPrompt: false,
      attachments: [],
      harnessArtifacts: [],
      statusOnly: false,
    };
    const leader = {
      id: 'agent-leader',
      tenant_id: 'tenant_demo',
      name: '人事',
      is_overall: false,
      status: 'active',
      metadata: { employee_profile: { avatar_text: '人', avatar_preset: 'ops-grid' } },
      resources: [],
      created_at: '2026-08-15T00:00:00Z',
      updated_at: '2026-08-15T00:00:00Z',
    } as AgentProfileRead;
    const chat = {
      displayedTeam: { id: 'team-1', name: '项目组' } as TeamRead,
      displayedAgent: leader,
      slashCommands: [],
      toggleTrace: vi.fn(),
      rateMessage: vi.fn(),
      setActiveCitation: vi.fn(),
      confirmScheduledTask: vi.fn(),
      dismissScheduledTaskDraft: vi.fn(),
      removeQueuedTurn: vi.fn(),
      tenantId: 'tenant_demo',
      activeConversationId: 'session-team-1',
    } as unknown as UseChatSession;

    render(<MessageBubble chat={chat} item={item} render={messageRender} />);

    expect(screen.getByLabelText(/员工头像/)).toBeTruthy();
    expect(screen.getByText('人事')).toBeTruthy();
    expect(screen.getByText('项目领导')).toBeTruthy();
    expect(screen.getByText('团队回复')).toBeTruthy();
  });
});
