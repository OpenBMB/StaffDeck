// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentProfileRead } from '@/types';

import EmployeeApiKeyDialog from './EmployeeApiKeyDialog';

const agent: AgentProfileRead = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  is_overall: false,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-29T00:00:00Z',
  updated_at: '2026-08-29T00:00:00Z',
};

/** 构造客户端请求所需的成功 JSON 响应。 */
function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

/** 模拟员工密钥生命周期接口，并让刷新请求返回最新状态。 */
function stubEmployeeCredentialFetch() {
  let status = 'active';
  let exists = true;
  const credential = () => ({
    id: 'employee-key-1',
    agent_id: agent.id,
    name: '员工运行密钥',
    access: 'runtime',
    key_prefix: 'sd_live_test…',
    scopes: ['runs:*'],
    status,
    created_at: '2026-08-29T00:00:00Z',
  });
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (
      init?.method === 'POST'
      && url.endsWith('/api/enterprise/agents/agent-1/api-credentials/employee-key-1/revoke?tenant_id=tenant_demo')
    ) {
      status = 'revoked';
      return jsonResponse(credential());
    }
    if (
      init?.method === 'DELETE'
      && url.endsWith('/api/enterprise/agents/agent-1/api-credentials/employee-key-1?tenant_id=tenant_demo')
    ) {
      exists = false;
      return jsonResponse({});
    }
    if (url.endsWith('/api/enterprise/agents/agent-1/api-credentials?tenant_id=tenant_demo')) {
      return jsonResponse(exists ? [credential()] : []);
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('EmployeeApiKeyDialog', () => {
  it('uses an in-application confirmation before revoking an employee credential', async () => {
    const user = userEvent.setup();
    const fetchMock = stubEmployeeCredentialFetch();

    render(<EmployeeApiKeyDialog agent={agent} open onClose={vi.fn()} />);

    await screen.findByText('员工运行密钥');
    await user.click(screen.getByRole('button', { name: '禁用' }));
    expect(await screen.findByText('确认禁用 API 密钥')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: '确认禁用' }));

    await waitFor(() => expect(screen.getByText('已禁用')).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/enterprise/agents/agent-1/api-credentials/employee-key-1/revoke?tenant_id=tenant_demo',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('cancels or permanently deletes an employee credential only after confirmation', async () => {
    const user = userEvent.setup();
    const fetchMock = stubEmployeeCredentialFetch();

    render(<EmployeeApiKeyDialog agent={agent} open onClose={vi.fn()} />);

    await screen.findByText('员工运行密钥');
    await user.click(screen.getByRole('button', { name: '删除' }));
    expect(await screen.findByText('确认删除 API 密钥')).toBeTruthy();
    expect(screen.getByText(/删除后无法恢复/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText('员工运行密钥')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '删除' }));
    await user.click(screen.getByRole('button', { name: '永久删除' }));

    await waitFor(() => expect(screen.queryByText('员工运行密钥')).toBeNull());
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/enterprise/agents/agent-1/api-credentials/employee-key-1?tenant_id=tenant_demo',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});
