// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createRuleSet,
  createRuleSetVersion,
  listRuleDefinitions,
  listRuleSets,
  listRuleSetVersions,
  publishRuleSetVersion,
  replaceRuleDefinitions,
  validateRuleSetVersion,
  type RuleDefinitionDraft,
} from './ruleLibraryApi';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

const rule: RuleDefinitionDraft = {
  rule_key: 'scope.required',
  name: '认证范围必填',
  description: '认证范围不能为空',
  workflow_nodes: ['collect'],
  information_domains: ['certification_project'],
  document_types: ['application'],
  field_keys: ['certification_project.scope'],
  execution_level: 'mandatory',
  execution_method: 'deterministic',
  condition: {
    operator: 'required',
    field_key: 'certification_project.scope',
    value: '有效',
  },
  input_requirements: [],
  evidence_requirements: [{ kind: 'project_field' }],
  source_refs: [{ internal_policy_ref: 'POL-001' }],
  sequence: 0,
  enabled: true,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('rule library api', () => {
  it('lists rule sets with the current tenant query', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await listRuleSets();

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/rule-sets?tenant_id=tenant_demo');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBeUndefined();
  });

  it('creates a rule set with the current tenant in the body', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ id: 'set-1' }));
    vi.stubGlobal('fetch', fetchMock);

    await createRuleSet({
      key: 'energy.audit',
      name: '能源审核规则',
      description: '能源管理体系规则',
      management_systems: ['EnMS'],
      audit_types: ['recertification'],
      business_domain: 'energy',
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/rule-sets');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      tenant_id: 'tenant_demo',
      key: 'energy.audit',
      name: '能源审核规则',
      description: '能源管理体系规则',
      management_systems: ['EnMS'],
      audit_types: ['recertification'],
      business_domain: 'energy',
    });
  });

  it('lists versions with encoded rule-set id and tenant query', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await listRuleSetVersions('set/one');

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/rule-sets/set%2Fone/versions?tenant_id=tenant_demo',
    );
  });

  it('creates a draft version with a rules wrapper body', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ id: 'version-1' }));
    vi.stubGlobal('fetch', fetchMock);

    await createRuleSetVersion('set/one', [rule]);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/rule-sets/set%2Fone/versions?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ rules: [rule] });
  });

  it('lists definitions with encoded rule-set and version ids', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await listRuleDefinitions('set/one', 'version/two');

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/rule-sets/set%2Fone/versions/version%2Ftwo/rules?tenant_id=tenant_demo',
    );
  });

  it('replaces draft definitions with a rules wrapper body', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ id: 'version-1' }));
    vi.stubGlobal('fetch', fetchMock);

    await replaceRuleDefinitions('set/one', 'version/two', [rule]);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/rule-sets/set%2Fone/versions/version%2Ftwo/rules?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PUT');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ rules: [rule] });
  });

  it('validates a version through the tenant-scoped action endpoint', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ errors: [] }));
    vi.stubGlobal('fetch', fetchMock);

    await validateRuleSetVersion('set/one', 'version/two');

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/rule-sets/set%2Fone/versions/version%2Ftwo/validate?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBeUndefined();
  });

  it('publishes a version through the tenant-scoped action endpoint', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({ id: 'version-2', status: 'published' }));
    vi.stubGlobal('fetch', fetchMock);

    await publishRuleSetVersion('set/one', 'version/two');

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/rule-sets/set%2Fone/versions/version%2Ftwo/publish?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBeUndefined();
  });
});
