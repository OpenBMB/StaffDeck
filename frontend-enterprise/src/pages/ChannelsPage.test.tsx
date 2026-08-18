// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { EnterpriseAuthUser } from '@/auth';
import type {
  AgentProfileRead,
  ChannelBindingRead,
  ChannelMetaRead,
  TeamRead,
} from '@/types';

import ChannelsPage from './ChannelsPage';

const adminUser: EnterpriseAuthUser = {
  id: 'user-1',
  tenant_id: 'tenant_demo',
  username: 'admin',
  role: 'admin',
};

const agent: AgentProfileRead = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  is_overall: false,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const team: TeamRead = {
  id: 'team-1',
  tenant_id: 'tenant_demo',
  name: '增长团队',
  description: '负责增长实验',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [
    {
      id: 'member-1',
      team_id: 'team-1',
      agent_id: 'agent-1',
      role: 'leader',
      agent_name: '小艾',
      created_at: '2026-08-01T00:00:00Z',
    },
    {
      id: 'member-2',
      team_id: 'team-1',
      agent_id: 'agent-2',
      role: 'member',
      agent_name: '小北',
      created_at: '2026-08-01T00:00:00Z',
    },
  ],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const channelMetas: ChannelMetaRead[] = [
  { channel: 'wechat', name: '微信', setup: 'qrcode', capabilities: [] },
];

const teamBinding: ChannelBindingRead = {
  id: 'binding-1',
  tenant_id: 'tenant_demo',
  agent_id: '',
  channel: 'wechat',
  status: 'active',
  connected: true,
  agents: [],
  team_id: 'team-1',
  team_name: '增长团队',
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

function makeFetchMock(overrides: { bindings?: unknown; teams?: unknown; agents?: unknown } = {}) {
  const bindings = overrides.bindings ?? [];
  const teams = overrides.teams ?? [team];
  const agents = overrides.agents ?? [agent];
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || 'GET';
    if (method === 'POST' && url.endsWith('/api/enterprise/channels')) {
      return jsonResponse({ ...teamBinding, id: 'binding-new' });
    }
    if (url.includes('/channels/meta')) return jsonResponse(channelMetas);
    if (url.includes('/my-identity-bindings')) return jsonResponse([]);
    if (url.includes('/deliveries/days')) {
      return jsonResponse({ days: [], total_days: 0, offset: 0, limit: 7 });
    }
    if (url.includes('/conversations')) {
      return jsonResponse({ items: [], total: 0, offset: 0, limit: 20 });
    }
    if (url.includes('/api/enterprise/agents')) return jsonResponse(agents);
    if (url.includes('/api/enterprise/teams')) return jsonResponse(teams);
    if (url.includes('/api/enterprise/channels')) return jsonResponse(bindings);
    if (url.includes('/api/auth/users')) return jsonResponse([]);
    return jsonResponse({});
  });
}

function renderPage() {
  return render(
    <I18nProvider>
      <MemoryRouter>
        <ChannelsPage currentUser={adminUser} />
      </MemoryRouter>
    </I18nProvider>,
  );
}

async function openCreateDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getAllByRole('button', { name: /接入渠道/ })[0]);
  await user.click(await screen.findByRole('button', { name: /微信/ }));
  await screen.findByText('选择绑定对象');
}

function createPostBody(fetchMock: ReturnType<typeof makeFetchMock>): Record<string, unknown> {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => init?.method === 'POST' && String(input).endsWith('/api/enterprise/channels'),
  );
  expect(call).toBeTruthy();
  return JSON.parse(String(call?.[1]?.body)) as Record<string, unknown>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ChannelsPage', () => {
  it('creates a binding with agent_id when the agent target is selected', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await openCreateDialog(user);

    await screen.findByText('小艾');
    await user.click(screen.getAllByRole('radio')[0]);
    await user.click(screen.getByRole('button', { name: '创建微信接入' }));

    await waitFor(() => {
      const body = createPostBody(fetchMock);
      expect(body.agent_id).toBe('agent-1');
      expect(body).not.toHaveProperty('team_id');
    });
  });

  it('creates a binding with team_id when the team target is selected', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock();
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await openCreateDialog(user);

    await user.click(screen.getByRole('button', { name: '团队' }));
    await screen.findByText('增长团队');
    expect(screen.getByText('项目领导：小艾')).toBeTruthy();
    expect(screen.getByText('2 名成员')).toBeTruthy();
    await user.click(screen.getAllByRole('radio')[0]);
    await user.click(screen.getByRole('button', { name: '创建微信接入' }));

    await waitFor(() => {
      const body = createPostBody(fetchMock);
      expect(body.team_id).toBe('team-1');
      expect(body).not.toHaveProperty('agent_id');
    });
  });

  it('shows the team as read-only in the detail of a team binding', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock({ bindings: [teamBinding] });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    expect(await screen.findByText('团队 · 增长团队')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /团队 · 增长团队/ }));

    const section = await screen.findByRole('region', { name: '可调度员工' });
    expect(await within(section).findByText('团队：增长团队（项目领导：小艾）')).toBeTruthy();
    expect(within(section).queryByRole('button', { name: '编辑' })).toBeNull();
    expect(within(section).queryByRole('radio')).toBeNull();
  });

  it('shows an empty state with a create-team link when no team exists', async () => {
    const user = userEvent.setup();
    const fetchMock = makeFetchMock({ teams: [] });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await openCreateDialog(user);

    await user.click(screen.getByRole('button', { name: '团队' }));
    expect(await screen.findByText('暂无可用团队')).toBeTruthy();
    expect(screen.getByRole('button', { name: '去创建团队' })).toBeTruthy();
  });
});
