// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';

import TeamChatPage from './TeamChatPage';

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

afterEach(cleanup);

describe('TeamChatPage legacy redirect', () => {
  it('moves old links to the embedded team room without creating another session', async () => {
    render(
      <MemoryRouter initialEntries={['/enterprise/teams/team-1/chat']}>
        <Routes>
          <Route path="/enterprise/teams/:teamId/chat" element={<TeamChatPage />} />
          <Route path="/enterprise/teams/:teamId" element={<LocationEcho />} />
        </Routes>
      </MemoryRouter>,
    );

    expect((await screen.findByTestId('location')).textContent).toBe(
      '/enterprise/teams/team-1?view=chat',
    );
  });
});
