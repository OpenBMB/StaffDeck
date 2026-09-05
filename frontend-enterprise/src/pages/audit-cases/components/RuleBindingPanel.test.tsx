// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import { notify } from '@/components/ui/app-toast';
import { I18nProvider } from '@/i18n';
import type { AuditCaseDocumentRead } from '@/types';

import {
  loadCurrentRuleBindings,
  loadPublishedRuleVersionOptions,
  migrateRuleBindings,
  previewRuleBindingMigration,
  replaceCurrentRuleBindings,
  type PublishedRuleVersionOption,
  type RuleBindingRead,
} from '../ruleBindingApi';
import { RuleBindingPanel } from './RuleBindingPanel';

vi.mock('../ruleBindingApi', () => ({
  loadCurrentRuleBindings: vi.fn(),
  loadPublishedRuleVersionOptions: vi.fn(),
  migrateRuleBindings: vi.fn(),
  previewRuleBindingMigration: vi.fn(),
  replaceCurrentRuleBindings: vi.fn(),
}));

vi.mock('@/components/ui/app-toast', () => ({
  notify: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const ruleSet = {
  id: 'rule-set-1',
  key: 'iso-50001',
  name: '能源管理体系规则',
};

const documentOne: AuditCaseDocumentRead = {
  id: 'document-1',
  audit_case_id: 'case-1',
  document_key: 'policy-1',
  title: '能源方针',
  document_type: 'policy',
  zone: 'system',
  status: 'active',
  active_version_id: 'document-version-1',
  source_material_id: null,
  archive_reason: null,
  created_by_user_id: 'user-1',
  updated_by_user_id: 'user-1',
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  active_version: {
    id: 'document-version-1',
    document_id: 'document-1',
    version: 1,
    content_format: 'markdown',
    content: '# 能源方针',
    content_sha256: 'a'.repeat(64),
    characters: 6,
    created_by_user_id: 'user-1',
    created_at: '2026-09-01T00:00:00Z',
  },
};

function option(
  id: string,
  version: number,
  status: string = 'published',
): PublishedRuleVersionOption {
  return {
    ruleSet,
    version: {
      id,
      tenant_id: 'tenant_demo',
      rule_set_id: ruleSet.id,
      version,
      status,
      content_sha256: `sha-${id}`,
      published_by_user_id: 'user-1',
      published_at: '2026-09-01T00:00:00Z',
    },
  };
}

function binding(versionId: string, priority = 1): RuleBindingRead {
  return {
    id: `binding-${versionId}`,
    tenant_id: 'tenant_demo',
    audit_case_id: 'case-1',
    rule_set_id: ruleSet.id,
    rule_set_version_id: versionId,
    selection_source: 'manual',
    status: 'active',
    priority,
    bound_by_user_id: 'user-1',
    supersedes_binding_id: null,
  };
}

const publishedV1 = option('version-1', 1);
const publishedV2 = option('version-2', 2);
const draftV3 = option('version-3', 3, 'draft');

function notInitializedError(): ApiError {
  return new ApiError(
    404,
    JSON.stringify({ code: 'RULE_BINDING_NOT_INITIALIZED', detail: '尚未初始化规则绑定' }),
    'Not Found',
  );
}

function renderPanel(props: { caseId?: string; disabled?: boolean; documents?: AuditCaseDocumentRead[] } = {}) {
  return render(
    <I18nProvider>
      <RuleBindingPanel caseId={props.caseId || 'case-1'} documents={props.documents} disabled={props.disabled} />
    </I18nProvider>,
  );
}

function mockLoadedState(options: PublishedRuleVersionOption[], current: RuleBindingRead[] | Error) {
  vi.mocked(loadPublishedRuleVersionOptions).mockResolvedValue(options);
  if (current instanceof Error) {
    vi.mocked(loadCurrentRuleBindings).mockRejectedValue(current);
  } else {
    vi.mocked(loadCurrentRuleBindings).mockResolvedValue(current);
  }
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  vi.mocked(replaceCurrentRuleBindings).mockResolvedValue([binding('version-1')]);
  vi.mocked(previewRuleBindingMigration).mockResolvedValue({
    added_rule_keys: ['RULE-NEW'],
    removed_rule_keys: ['RULE-OLD'],
    changed_rule_keys: ['RULE-CHANGED'],
    unchanged_rule_keys: ['RULE-SAME'],
    impacted_information_domains: ['能源绩效'],
    impacted_workflow_nodes: ['现场审核'],
  });
  vi.mocked(migrateRuleBindings).mockResolvedValue([binding('version-2')]);
});

describe('RuleBindingPanel', () => {
  it('requires and sends the selected project document scope for new bindings', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1], notInitializedError());

    renderPanel({ documents: [documentOne] });

    expect((await screen.findByRole('combobox', { name: '规则文件' }) as HTMLSelectElement).value).toBe('document-1');
    await user.click(screen.getByLabelText('能源管理体系规则 · 版本 1'));
    await user.click(screen.getByRole('button', { name: '绑定选中版本' }));

    await waitFor(() => expect(replaceCurrentRuleBindings).toHaveBeenCalledWith(
      'case-1',
      ['version-1'],
      'manual',
      'document-1',
      'document-version-1',
    ));
  });

  it('shows an explicit empty state for an uninitialized project and only published candidates', async () => {
    mockLoadedState([publishedV1, publishedV2, draftV3], notInitializedError());

    renderPanel();

    expect(await screen.findByText('尚未绑定规则版本')).toBeTruthy();
    expect(screen.getByLabelText('能源管理体系规则 · 版本 1')).toBeTruthy();
    expect(screen.getByLabelText('能源管理体系规则 · 版本 2')).toBeTruthy();
    expect(screen.queryByLabelText('能源管理体系规则 · 版本 3')).toBeNull();
  });

  it('explicitly binds selected published versions when the project is unbound', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], notInitializedError());

    renderPanel();
    await user.click(await screen.findByLabelText('能源管理体系规则 · 版本 1'));
    await user.click(screen.getByRole('button', { name: '绑定选中版本' }));

    await waitFor(() => expect(replaceCurrentRuleBindings).toHaveBeenCalledWith('case-1', ['version-1'], 'manual'));
    expect(await screen.findByText('当前绑定')).toBeTruthy();
    expect(screen.getAllByText('能源管理体系规则 · 版本 1')).toHaveLength(2);
    expect(vi.mocked(notify.success)).toHaveBeenCalled();
  });

  it('previews a changed binding and only migrates after a non-empty reason', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], [binding('version-1')]);

    renderPanel();
    const v2 = await screen.findByLabelText('能源管理体系规则 · 版本 2') as HTMLInputElement;
    await user.click(v2);
    await user.click(screen.getByRole('button', { name: '预览迁移影响' }));

    await waitFor(() => expect(previewRuleBindingMigration).toHaveBeenCalledWith('case-1', ['version-1', 'version-2']));
    expect(await screen.findByText(/RULE-NEW/)).toBeTruthy();
    expect(screen.getByText(/RULE-OLD/)).toBeTruthy();
    expect(screen.getByText(/RULE-CHANGED/)).toBeTruthy();
    expect(screen.getByText(/能源绩效/)).toBeTruthy();
    expect(screen.getByText(/现场审核/)).toBeTruthy();

    const confirm = screen.getByRole('button', { name: '确认迁移' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await user.type(screen.getByLabelText('迁移原因'), '新版规则已完成评审');
    expect(confirm.disabled).toBe(false);
    await user.click(confirm);

    await waitFor(() => expect(migrateRuleBindings).toHaveBeenCalledWith('case-1', ['version-1', 'version-2'], '新版规则已完成评审'));
    expect(screen.getAllByText('能源管理体系规则 · 版本 2')).toHaveLength(2);
  });

  it('treats a changed binding priority order as a migration candidate', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], [binding('version-1', 1), binding('version-2', 2)]);

    renderPanel();
    const v1 = await screen.findByLabelText('能源管理体系规则 · 版本 1');
    await user.click(v1);
    await user.click(v1);
    await user.click(screen.getByRole('button', { name: '预览迁移影响' }));

    await waitFor(() => expect(previewRuleBindingMigration).toHaveBeenCalledWith('case-1', ['version-2', 'version-1']));
  });

  it('keeps the exact pinned current version visible without silently selecting a newer version', async () => {
    mockLoadedState([publishedV1, publishedV2], [binding('version-1')]);

    renderPanel();

    expect(await screen.findByText('当前绑定')).toBeTruthy();
    expect(screen.getAllByText('能源管理体系规则 · 版本 1')).toHaveLength(2);
    expect((screen.getByLabelText('能源管理体系规则 · 版本 1') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('能源管理体系规则 · 版本 2') as HTMLInputElement).checked).toBe(false);
    expect(screen.getAllByText(/不会自动切换/)).toHaveLength(2);
  });

  it('disables selection, binding, preview, and migration controls for an archived project', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], [binding('version-1')]);

    renderPanel({ disabled: true });

    const v1 = await screen.findByLabelText('能源管理体系规则 · 版本 1') as HTMLInputElement;
    expect(v1.disabled).toBe(true);
    expect((screen.getByLabelText('能源管理体系规则 · 版本 2') as HTMLInputElement).disabled).toBe(true);
    const previewButton = screen.getByRole('button', { name: '预览迁移影响' }) as HTMLButtonElement;
    const migrateButton = screen.getByRole('button', { name: '确认迁移' }) as HTMLButtonElement;
    expect(previewButton.disabled).toBe(true);
    expect(migrateButton.disabled).toBe(true);
    expect(screen.getByText(/项目已归档/)).toBeTruthy();

    await user.click(v1);
    await user.click(previewButton);
    await user.click(migrateButton);

    expect(v1.checked).toBe(true);
    expect(previewRuleBindingMigration).not.toHaveBeenCalled();
    expect(migrateRuleBindings).not.toHaveBeenCalled();
    expect(replaceCurrentRuleBindings).not.toHaveBeenCalled();
  });

  it('preserves the selected version and notifies when initial binding fails', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], notInitializedError());
    vi.mocked(replaceCurrentRuleBindings).mockRejectedValue(new Error('写入失败'));

    renderPanel();
    const v1 = await screen.findByLabelText('能源管理体系规则 · 版本 1') as HTMLInputElement;
    await user.click(v1);
    await user.click(screen.getByRole('button', { name: '绑定选中版本' }));

    await waitFor(() => expect(notify.error).toHaveBeenCalled());
    expect(v1.checked).toBe(true);
    expect(screen.getByText('尚未绑定规则版本')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('规则版本绑定失败');
  });

  it('locks the previous state when candidate loading fails', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], [binding('version-1')]);
    const view = renderPanel();
    await screen.findByLabelText('能源管理体系规则 · 版本 2');

    vi.mocked(loadPublishedRuleVersionOptions).mockRejectedValue(new Error('候选读取失败'));
    vi.mocked(loadCurrentRuleBindings).mockResolvedValue([binding('version-1')]);
    view.rerender(
      <I18nProvider>
        <RuleBindingPanel caseId="case-2" />
      </I18nProvider>,
    );

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('规则版本候选加载失败'));
    const v2 = screen.getByLabelText('能源管理体系规则 · 版本 2') as HTMLInputElement;
    const previewButton = screen.getByRole('button', { name: '预览迁移影响' }) as HTMLButtonElement;
    expect(v2.disabled).toBe(true);
    expect(previewButton.disabled).toBe(true);
    await user.click(v2);
    expect(previewRuleBindingMigration).not.toHaveBeenCalled();
  });

  it('locks the previous state when current binding loading fails', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], [binding('version-1')]);
    const view = renderPanel();
    await screen.findByLabelText('能源管理体系规则 · 版本 2');

    vi.mocked(loadPublishedRuleVersionOptions).mockResolvedValue([publishedV1, publishedV2]);
    vi.mocked(loadCurrentRuleBindings).mockRejectedValue(new Error('绑定读取失败'));
    view.rerender(
      <I18nProvider>
        <RuleBindingPanel caseId="case-2" />
      </I18nProvider>,
    );

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('当前规则绑定加载失败'));
    const v2 = screen.getByLabelText('能源管理体系规则 · 版本 2') as HTMLInputElement;
    const previewButton = screen.getByRole('button', { name: '预览迁移影响' }) as HTMLButtonElement;
    expect(v2.disabled).toBe(true);
    expect(previewButton.disabled).toBe(true);
    await user.click(v2);
    expect(previewRuleBindingMigration).not.toHaveBeenCalled();
  });

  it('preserves the current binding and selected versions when preview fails', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], [binding('version-1')]);
    vi.mocked(previewRuleBindingMigration).mockRejectedValue(new Error('预览失败'));

    renderPanel();
    const v2 = await screen.findByLabelText('能源管理体系规则 · 版本 2') as HTMLInputElement;
    await user.click(v2);
    await user.click(screen.getByRole('button', { name: '预览迁移影响' }));

    await waitFor(() => expect(notify.error).toHaveBeenCalled());
    expect(screen.getAllByText('能源管理体系规则 · 版本 1')).toHaveLength(2);
    expect((screen.getByLabelText('能源管理体系规则 · 版本 1') as HTMLInputElement).checked).toBe(true);
    expect(v2.checked).toBe(true);
    expect(screen.queryByText('迁移影响预览')).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain('迁移影响预览失败');
  });

  it('preserves visible migration state and notifies when migration fails', async () => {
    const user = userEvent.setup();
    mockLoadedState([publishedV1, publishedV2], [binding('version-1')]);
    vi.mocked(migrateRuleBindings).mockRejectedValue(new Error('网络暂时不可用'));

    renderPanel();
    await user.click(await screen.findByLabelText('能源管理体系规则 · 版本 2'));
    await user.click(screen.getByRole('button', { name: '预览迁移影响' }));
    await screen.findByText(/RULE-CHANGED/);
    await user.type(screen.getByLabelText('迁移原因'), '审查后切换规则');
    await user.click(screen.getByRole('button', { name: '确认迁移' }));

    await waitFor(() => expect(vi.mocked(notify.error)).toHaveBeenCalled());
    expect(screen.getAllByText('能源管理体系规则 · 版本 1')).toHaveLength(2);
    expect((screen.getByLabelText('能源管理体系规则 · 版本 2') as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText(/RULE-CHANGED/)).toBeTruthy();
    expect((screen.getByLabelText('迁移原因') as HTMLTextAreaElement).value).toBe('审查后切换规则');
  });
});

