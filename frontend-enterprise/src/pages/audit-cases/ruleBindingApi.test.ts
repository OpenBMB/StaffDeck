// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

const ruleLibraryMocks = vi.hoisted(() => ({
  listRuleSets: vi.fn(),
  listRuleSetVersions: vi.fn(),
}));

vi.mock('../rules/ruleLibraryApi', () => ruleLibraryMocks);

import {
  loadCurrentRuleBindings,
  loadPublishedRuleVersionOptions,
  migrateRuleBindings,
  previewRuleBindingMigration,
  replaceCurrentRuleBindings,
} from './ruleBindingApi';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('rule binding api', () => {
  it('loads current bindings with an encoded case id and tenant query', async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await loadCurrentRuleBindings('case/one');

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/audit-cases/case%2Fone/rule-bindings?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBeUndefined();
  });

  it('loads only published versions while retaining their owning rule-set identity', async () => {
    ruleLibraryMocks.listRuleSets.mockResolvedValue([
      {
        id: 'set-1',
        tenant_id: 'tenant_demo',
        key: 'energy.audit',
        name: '能源审核规则',
        description: '规则集',
        management_systems: ['EnMS'],
        audit_types: ['recertification'],
        business_domain: 'energy',
        status: 'active',
      },
      {
        id: 'set-2',
        tenant_id: 'tenant_demo',
        key: 'quality.audit',
        name: '质量审核规则',
        description: '规则集',
        management_systems: ['QMS'],
        audit_types: ['initial'],
        business_domain: 'quality',
        status: 'active',
      },
    ]);
    ruleLibraryMocks.listRuleSetVersions.mockImplementation(async (ruleSetId: string) => (
      ruleSetId === 'set-1'
        ? [
          {
            id: 'version-draft',
            tenant_id: 'tenant_demo',
            rule_set_id: 'set-1',
            version: 2,
            status: 'draft',
            content_sha256: 'draft-sha',
            published_by_user_id: null,
            published_at: null,
          },
          {
            id: 'version-published',
            tenant_id: 'tenant_demo',
            rule_set_id: 'set-1',
            version: 1,
            status: 'published',
            content_sha256: 'published-sha',
            published_by_user_id: 'user-1',
            published_at: '2026-09-05T00:00:00Z',
          },
        ]
        : [
          {
            id: 'version-other',
            tenant_id: 'tenant_demo',
            rule_set_id: 'set-2',
            version: 1,
            status: 'archived',
            content_sha256: 'other-sha',
            published_by_user_id: null,
            published_at: null,
          },
        ]
    ));

    const options = await loadPublishedRuleVersionOptions();

    expect(ruleLibraryMocks.listRuleSets).toHaveBeenCalledOnce();
    expect(ruleLibraryMocks.listRuleSetVersions).toHaveBeenCalledWith('set-1');
    expect(ruleLibraryMocks.listRuleSetVersions).toHaveBeenCalledWith('set-2');
    expect(options).toEqual([
      {
        ruleSet: {
          id: 'set-1',
          key: 'energy.audit',
          name: '能源审核规则',
        },
        version: {
          id: 'version-published',
          tenant_id: 'tenant_demo',
          rule_set_id: 'set-1',
          version: 1,
          status: 'published',
          content_sha256: 'published-sha',
          published_by_user_id: 'user-1',
          published_at: '2026-09-05T00:00:00Z',
        },
      },
    ]);
  });

  it('replaces current bindings with version ids and selection source', async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await replaceCurrentRuleBindings('case/one', ['version-1', 'version-2'], 'recommended');

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/audit-cases/case%2Fone/rule-bindings?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PUT');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      version_ids: ['version-1', 'version-2'],
      selection_source: 'recommended',
    });
  });

  it('previews a binding migration with version ids', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({
      added_rule_keys: [],
      removed_rule_keys: [],
      changed_rule_keys: [],
      unchanged_rule_keys: [],
      impacted_information_domains: [],
      impacted_workflow_nodes: [],
    }));
    vi.stubGlobal('fetch', fetchMock);

    await previewRuleBindingMigration('case/one', ['version-2']);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/audit-cases/case%2Fone/rule-bindings/migration-preview?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      version_ids: ['version-2'],
    });
  });

  it('migrates bindings with version ids and a reason', async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await migrateRuleBindings('case/one', ['version-2'], '更新认证范围');

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/audit-cases/case%2Fone/rule-bindings/migrate?tenant_id=tenant_demo',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      version_ids: ['version-2'],
      reason: '更新认证范围',
    });
  });
});
