// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import TeamChatPage from './TeamChatPage';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderRedirect() {
  return render(
    <MemoryRouter initialEntries={['/enterprise/teams/team-1/chat']}>
      <Routes>
        <Route path="/enterprise/teams/:teamId/chat" element={<TeamChatPage />} />
        <Route path="/workspace/chat/:sessionId" element={<LocationEcho />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('TeamChatPage legacy redirect', () => {
  it('creates the team session and moves the conversation to the chat app', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      jsonResponse({ session_id: 'session-team-1' }),
    );
    vi.stubGlobal('fetch', fetchMock);

    renderRedirect();

    expect(screen.getByText('正在前往对话端…')).toBeTruthy();
    expect((await screen.findByTestId('location')).textContent).toBe(
      '/workspace/chat/session-team-1',
    );
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      '/api/enterprise/teams/team-1/tl/session',
    );
  });

  it('keeps a clear error state when the session cannot be opened', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      text: async () => JSON.stringify({ detail: 'upstream failed' }),
    } as Response)));

    renderRedirect();

    await waitFor(() => {
      expect(screen.queryByText('正在前往对话端…')).toBeNull();
    });
    expect(screen.getByText(/upstream failed|HTTP 500/)).toBeTruthy();
  });
});
