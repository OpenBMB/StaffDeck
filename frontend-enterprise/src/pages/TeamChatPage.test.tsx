// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import TeamChatPage from './TeamChatPage';

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('TeamChatPage legacy redirect', () => {
  it('opens the persistent team group in the chat app', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      text: async () => JSON.stringify({ session_id: 'session-team-1' }),
    } as Response));
    vi.stubGlobal('fetch', fetchMock);
    render(
      <MemoryRouter initialEntries={['/enterprise/teams/team-1/chat']}>
        <Routes>
          <Route path="/enterprise/teams/:teamId/chat" element={<TeamChatPage />} />
          <Route path="/workspace/chat/:sessionId" element={<LocationEcho />} />
        </Routes>
      </MemoryRouter>,
    );

    expect((await screen.findByTestId('location')).textContent).toBe(
      '/workspace/chat/session-team-1',
    );
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('/teams/team-1/tl/session');
  });
});
