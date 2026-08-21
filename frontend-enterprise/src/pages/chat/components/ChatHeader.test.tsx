// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { ChatSession, TeamRead } from '@/types';

import type { UseChatSession } from '../useChatSession';
import ChatHeader from './ChatHeader';

const team: TeamRead = {
  id: 'team-1',
  tenant_id: 'tenant_demo',
  name: '增长团队',
  description: '',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function buildChat(session: Partial<ChatSession>): UseChatSession {
  return {
    auth: null,
    currentSession: {
      id: 'session-1',
      tenant_id: 'tenant_demo',
      status: 'active',
      updated_at: '2026-08-01T00:00:00Z',
      ...session,
    } as ChatSession,
    openRename: vi.fn(),
    logout: vi.fn(),
  } as unknown as UseChatSession;
}

function renderHeader(chat: UseChatSession) {
  return render(
    <I18nProvider>
      <ChatHeader chat={chat} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ChatHeader team badge', () => {
  it('keeps header controls below the native desktop drag region', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([team])));
    const { container } = renderHeader(buildChat({ title: '计划讨论' }));
    const headerClassName = container.firstElementChild?.className || '';

    expect(headerClassName).toContain('h-[88px]');
    expect(headerClassName).toContain('pt-[32px]');
  });

  it('shows the team name and group-chat badge when present', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([team])));
    renderHeader(buildChat({ title: '计划讨论', team_id: 'team-1', team_name: '增长团队' }));

    expect(screen.getByText('增长团队')).toBeTruthy();
    expect(screen.getByText('群聊')).toBeTruthy();
  });

  it('hides the legacy project-lead session title behind the group identity', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([team])));
    renderHeader(buildChat({ title: '增长团队 · TL 对话', team_id: 'team-1', team_name: '增长团队' }));

    expect(screen.getByText('增长团队')).toBeTruthy();
    expect(screen.getByText('群聊')).toBeTruthy();
    expect(screen.queryByText('增长团队 · TL 对话')).toBeNull();
  });

  it('resolves the team name from the teams list when team_name is missing', async () => {
    const fetchMock = vi.fn(async () => jsonResponse([team]));
    vi.stubGlobal('fetch', fetchMock);
    renderHeader(buildChat({ title: '计划讨论', team_id: 'team-1' }));

    expect((await screen.findByText('增长团队')).textContent).toBeTruthy();
    expect(screen.getByText('群聊')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/enterprise/teams?tenant_id='),
      expect.anything(),
    );
  });

  it('renders no badge for regular employee sessions', () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([team])));
    renderHeader(buildChat({ title: '计划讨论' }));

    expect(screen.queryByText(/团队/)).toBeNull();
  });
});
