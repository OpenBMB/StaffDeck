// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AuditCaseManagementOptions } from '@/types';
import { I18nProvider } from '@/i18n';

import { KnowledgeVersionSelector } from './KnowledgeVersionSelector';

const options: AuditCaseManagementOptions = {
  knowledge_versions: [
    {
      id: 'trunk',
      knowledge_base_id: 'kb-energy',
      name: '能源管理体系',
      version: '1.0.0',
      status: 'active',
      document_count: 0,
      chunk_count: 0,
      is_agent_branch: false,
      recommended: false,
      duplicate_group: null,
    },
    {
      id: 'branch',
      knowledge_base_id: 'kb-energy',
      name: '能源管理体系',
      version: '1.0.0-branch.agent_demo.1',
      status: 'active',
      document_count: 1,
      chunk_count: 20,
      is_agent_branch: true,
      recommended: true,
      duplicate_group: 'energy',
    },
    {
      id: 'other',
      knowledge_base_id: 'kb-other',
      name: '质量管理体系',
      version: '1.0.0',
      status: 'active',
      document_count: 3,
      chunk_count: 30,
      is_agent_branch: false,
      recommended: true,
      duplicate_group: 'energy',
    },
  ],
  audit_types: [],
  management_systems: [],
  material_types: [],
  supported_extensions: ['.txt'],
  max_material_bytes: 1024,
};

afterEach(() => cleanup());

describe('KnowledgeVersionSelector', () => {
  it('groups versions, searches by knowledge-base name, and labels branch metadata', async () => {
    const user = userEvent.setup();
    render(<I18nProvider><KnowledgeVersionSelector options={options} selected={[]} onChange={vi.fn()} /></I18nProvider>);

    expect(screen.getAllByText('推荐版本').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/数字员工工作版本/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('疑似重复知识库').length).toBeGreaterThan(0);

    await user.type(screen.getByRole('searchbox', { name: '搜索知识库' }), '质量');

    expect(screen.getByText('质量管理体系')).toBeTruthy();
    expect(screen.queryByText('能源管理体系')).toBeNull();
  });

  it('shows only selected versions when selected-only mode is enabled', async () => {
    const user = userEvent.setup();
    render(<I18nProvider><KnowledgeVersionSelector options={options} selected={['branch']} onChange={vi.fn()} /></I18nProvider>);

    await user.click(screen.getByRole('checkbox', { name: '只看已选' }));

    expect(screen.getByRole('checkbox', { name: /能源管理体系 1\.0\.0-branch/ })).toBeTruthy();
    expect(screen.queryByRole('checkbox', { name: '质量管理体系 1.0.0' })).toBeNull();
  });
});
