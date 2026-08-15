// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { TeamRead } from '@/types';

import EmployeeGalleryPage from './EmployeeGalleryPage';

const team: TeamRead = {
  id: 'team-1',
  tenant_id: 'tenant_demo',
  name: '增长团队',
  description: '负责增长实验与内容投放',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [
    { id: 'm-1', team_id: 'team-1', agent_id: 'agent-1', role: 'leader', agent_name: '小艾', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-2', team_id: 'team-1', agent_id: 'agent-2', role: 'member', agent_name: '小北', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-3', team_id: 'team-1', agent_id: 'agent-3', role: 'member', agent_name: '小南', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-4', team_id: 'team-1', agent_id: 'agent-4', role: 'member', agent_name: '小西', created_at: '2026-08-01T00:00:00Z' },
  ],
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

function stubGalleryFetch(teams: TeamRead[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'POST' && url.includes('/tl/session')) {
      return jsonResponse({ session_id: 'session-tl-1' });
    }
    if (url.includes('/api/enterprise/teams')) return jsonResponse(teams);
    if (url.includes('/api/enterprise/agents')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function renderGallery() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/workspace/gallery']}>
        <Routes>
          <Route path="/workspace/gallery" element={<EmployeeGalleryPage />} />
          <Route path="/workspace/chat/:sessionId" element={<LocationEcho />} />
          <Route path="/enterprise/teams/:teamId" element={<LocationEcho />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('EmployeeGalleryPage teams tab', () => {
  it('renders team chat cards with member count, project leader and avatar stack', async () => {
    const user = userEvent.setup();
    stubGalleryFetch([team]);
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '团队对话' }));

    const section = await screen.findByRole('region', { name: '团队' });
    expect(within(section).getByText('增长团队')).toBeTruthy();
    expect(within(section).getByText('负责增长实验与内容投放')).toBeTruthy();
    expect(within(section).getByText('4 名成员')).toBeTruthy();
    expect(within(section).getByText('项目领导：小艾')).toBeTruthy();
    // 前 3 个成员头像叠放，其余折叠为 +N
    expect(within(section).getByText('+1')).toBeTruthy();
  });

  it('opens the team room inside the team workspace', async () => {
    const user = userEvent.setup();
    stubGalleryFetch([team]);
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '团队对话' }));
    const section = await screen.findByRole('region', { name: '团队' });
    await user.click(within(section).getByRole('button', { name: '增长团队' }));

    expect((await screen.findByTestId('location')).textContent).toBe('/enterprise/teams/team-1?view=chat');
  });

  it('does not render the team section on employee tabs', async () => {
    const user = userEvent.setup();
    stubGalleryFetch([team]);
    renderGallery();

    await screen.findByText('暂无数字员工');
    expect(screen.queryByRole('region', { name: '团队' })).toBeNull();

    await user.click(screen.getByRole('tab', { name: '我的数字员工' }));
    expect(screen.queryByRole('region', { name: '团队' })).toBeNull();
  });

  it('shows the teams empty state when there are no teams', async () => {
    const user = userEvent.setup();
    stubGalleryFetch([]);
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '团队对话' }));

    expect(await screen.findByText('暂无团队')).toBeTruthy();
  });
});
