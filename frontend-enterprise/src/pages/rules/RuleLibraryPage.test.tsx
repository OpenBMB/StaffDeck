// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '@/App';
import type { EnterpriseAuthUser } from '@/auth';
import { Toaster } from '@/components/ui';
import { EnterpriseRoute } from '@/enums/routes';
import { I18nProvider } from '@/i18n';

import RuleLibraryPage from './RuleLibraryPage';

const adminUser: EnterpriseAuthUser = {
  id: 'admin-1',
  tenant_id: 'tenant_demo',
  username: 'admin',
  display_name: '管理员',
  role: 'admin',
};

const ruleSets = [
  {
    id: 'set-1',
    tenant_id: 'tenant_demo',
    key: 'energy.audit',
    name: '能源审核规则',
    description: '能源管理体系规则',
    management_systems: ['EnMS'],
    audit_types: ['recertification'],
    business_domain: 'energy',
    status: 'active',
  },
  {
    id: 'set-2',
    tenant_id: 'tenant_demo',
    key: 'quality.audit',
    name: '辅助规则集',
    description: '第二套规则',
    management_systems: ['QMS'],
    audit_types: ['initial'],
    business_domain: 'quality',
    status: 'active',
  },
];

const draftVersion = {
  id: 'version-draft',
  tenant_id: 'tenant_demo',
  rule_set_id: 'set-1',
  version: 2,
  status: 'draft',
  content_sha256: '',
  published_by_user_id: null,
  published_at: null,
};

const publishedVersion = {
  ...draftVersion,
  id: 'version-published',
  version: 1,
  status: 'published',
  content_sha256: 'abc123',
  published_by_user_id: 'admin-1',
  published_at: '2026-09-05T00:00:00Z',
};

const rule = {
  id: 'rule-1',
  tenant_id: 'tenant_demo',
  rule_set_version_id: 'version-draft',
  rule_key: 'scope.required',
  name: '认证范围必填',
  description: '认证范围不能为空',
  workflow_nodes: ['collect'],
  information_domains: ['certification_project'],
  document_types: ['application'],
  field_keys: ['certification_project.scope'],
  execution_level: 'guidance',
  execution_method: 'deterministic',
  condition: {
    operator: 'required',
    field_key: 'certification_project.scope',
    value: '',
  },
  input_requirements: [],
  evidence_requirements: [{ kind: 'project_field' }],
  source_refs: [],
  sequence: 0,
  enabled: true,
};

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.ok === false ? 'Service Unavailable' : 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

function installRuleFetch(options: { failSecondSet?: boolean } = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();

    if (url === '/api/rule-sets?tenant_id=tenant_demo' && method === 'GET') {
      return jsonResponse(ruleSets);
    }
    if (url === '/api/rule-sets' && method === 'POST') {
      return jsonResponse({
        ...ruleSets[1],
        id: 'set-created',
        key: 'safety.audit',
        name: '安全审核规则',
        description: '',
        management_systems: [],
        audit_types: [],
        business_domain: '',
      });
    }
    if (url.includes('/rule-sets/set-1/versions?') && method === 'GET') {
      return jsonResponse([publishedVersion, draftVersion]);
    }
    if (url.includes('/rule-sets/set-2/versions?') && method === 'GET') {
      if (options.failSecondSet) {
        return jsonResponse({ detail: '版本服务不可用' }, { ok: false, status: 503 });
      }
      return jsonResponse([]);
    }
    if (url.includes('/rule-sets/set-created/versions?') && method === 'GET') {
      return jsonResponse([]);
    }
    if (url.includes('/versions/version-draft/rules?') && method === 'GET') {
      return jsonResponse([rule]);
    }
    if (url.includes('/versions/version-published/rules?') && method === 'GET') {
      return jsonResponse([{ ...rule, rule_set_version_id: 'version-published' }]);
    }
    if (url.includes('/versions/version-draft/rules?') && method === 'PUT') {
      return jsonResponse(draftVersion);
    }
    if (url.includes('/versions/version-draft/validate?') && method === 'POST') {
      return jsonResponse({ errors: [] });
    }
    if (url.includes('/versions/version-draft/publish?') && method === 'POST') {
      return jsonResponse({
        ...draftVersion,
        status: 'published',
        content_sha256: 'published-sha',
        published_by_user_id: 'admin-1',
        published_at: '2026-09-05T08:00:00Z',
      });
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderPage() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[EnterpriseRoute.Rules]}>
        <RuleLibraryPage currentUser={adminUser} onLogout={() => {}} />
        <Toaster />
      </MemoryRouter>
    </I18nProvider>,
  );
}

function stubBrowserApis() {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
}

beforeEach(() => {
  stubBrowserApis();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.history.pushState({}, '', '/');
});

describe('RuleLibraryPage', () => {
  it('shows loading before selecting the newest draft and loading its structured rules', async () => {
    let resolveRuleSets!: (response: Response) => void;
    const firstResponse = new Promise<Response>((resolve) => {
      resolveRuleSets = resolve;
    });
    const normalFetch = installRuleFetch();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/rule-sets?tenant_id=tenant_demo') return firstResponse;
      return normalFetch(input, init);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    expect(screen.getByText('正在加载规则库…')).toBeTruthy();
    await act(async () => resolveRuleSets(jsonResponse(ruleSets)));

    expect(await screen.findByRole('heading', { name: '能源审核规则' })).toBeTruthy();
    expect(await screen.findByDisplayValue('认证范围必填')).toBeTruthy();
    expect((screen.getByLabelText('规则版本') as HTMLElement).textContent).toContain('v2');
  });

  it('creates a rule set and selects it without inventing a version', async () => {
    const user = userEvent.setup();
    const fetchMock = installRuleFetch();
    renderPage();

    await user.click(await screen.findByRole('button', { name: '新建规则集' }));
    await user.type(screen.getByLabelText('规则集标识'), 'safety.audit');
    await user.type(screen.getByLabelText('规则集名称'), '安全审核规则');
    await user.click(screen.getByRole('button', { name: '创建规则集' }));

    expect(await screen.findByRole('heading', { name: '安全审核规则' })).toBeTruthy();
    expect(screen.getByText('暂无版本')).toBeTruthy();
    const createCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST');
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({
      tenant_id: 'tenant_demo',
      key: 'safety.audit',
      name: '安全审核规则',
    });
  });

  it('edits structured draft fields, supports add/remove, and saves mapped rules', async () => {
    const user = userEvent.setup();
    const fetchMock = installRuleFetch();
    renderPage();

    const nameInput = await screen.findByLabelText('规则名称 1');
    await user.clear(nameInput);
    await user.type(nameInput, '认证范围已填写');
    const conditionValue = screen.getByLabelText('条件值 1');
    await user.type(conditionValue, '有效');
    const evidence = screen.getByLabelText('证据要求 1');
    await user.clear(evidence);
    await user.type(evidence, 'project_field,document');
    await user.click(screen.getByRole('switch', { name: '启用规则 1' }));

    await user.click(screen.getByRole('button', { name: '添加规则' }));
    expect(screen.getByLabelText('规则名称 2')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '删除规则 2' }));
    expect(screen.queryByLabelText('规则名称 2')).toBeNull();
    await user.click(screen.getByRole('button', { name: '保存草稿' }));

    await waitFor(() => {
      const saveCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT');
      expect(saveCall).toBeTruthy();
      const body = JSON.parse(String(saveCall?.[1]?.body));
      expect(body.rules).toHaveLength(1);
      expect(body.rules[0]).toMatchObject({
        rule_key: 'scope.required',
        name: '认证范围已填写',
        condition: {
          operator: 'required',
          field_key: 'certification_project.scope',
          value: '有效',
        },
        evidence_requirements: [{ kind: 'project_field' }, { kind: 'document' }],
        enabled: false,
      });
    });
  });

  it('validates and publishes a draft, then removes every editing control', async () => {
    const user = userEvent.setup();
    installRuleFetch();
    renderPage();

    await screen.findByLabelText('规则名称 1');
    await user.click(screen.getByRole('button', { name: '校验规则' }));
    expect(await screen.findByText('校验通过，可以发布。')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '发布版本' }));
    expect(await screen.findByText('已发布版本仅供查看')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '保存草稿' })).toBeNull();
    expect(screen.queryByRole('button', { name: '校验规则' })).toBeNull();
    expect(screen.queryByRole('button', { name: '发布版本' })).toBeNull();
    expect(screen.queryByRole('button', { name: '添加规则' })).toBeNull();
    expect(screen.queryByLabelText('规则名称 1')).toBeNull();
    expect(screen.getByText('认证范围必填')).toBeTruthy();
  });

  it('preserves the visible draft and notifies when selecting another rule set fails', async () => {
    const user = userEvent.setup();
    installRuleFetch({ failSecondSet: true });
    renderPage();

    expect(await screen.findByDisplayValue('认证范围必填')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '辅助规则集' }));

    expect(await screen.findByDisplayValue('认证范围必填')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '能源审核规则' })).toBeTruthy();
    expect(await screen.findByText(/加载规则版本失败，请重试.*版本服务不可用/)).toBeTruthy();
  });

  it('redirects a non-admin visitor to the gallery route', async () => {
    const member = { ...adminUser, id: 'member-1', username: 'member', role: 'member' as const };
    window.localStorage.setItem('ultrarag_auth', JSON.stringify({ token: 'token-1', user: member }));
    window.localStorage.setItem('staffdeck_onboarding_guide_seen', '1');
    window.localStorage.setItem('staffdeck_quick_start_guide_seen', '1');
    window.history.pushState({}, '', EnterpriseRoute.Rules);
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/auth/me')) return jsonResponse(member);
      if (url.includes('/api/enterprise/model-configs')) {
        return jsonResponse([{ id: 'model-1', enabled: true }]);
      }
      return jsonResponse([]);
    }));

    render(<I18nProvider><App /></I18nProvider>);

    await waitFor(() => expect(window.location.pathname).toBe(EnterpriseRoute.Gallery));
    expect(screen.queryByText('规则库管理')).toBeNull();
  });
});
