// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { I18nProvider } from '@/i18n';
import type { AuditCaseManagementOptions } from '@/types';

import { CreateAuditCaseDialog } from './CreateAuditCaseDialog';

const { createAuditCaseMock } = vi.hoisted(() => ({
  createAuditCaseMock: vi.fn(),
}));

vi.mock('../auditCaseApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../auditCaseApi')>()),
  createAuditCase: createAuditCaseMock,
  loadAuditCaseMembers: vi.fn().mockResolvedValue([]),
}));

const options = {
  agent_options: [
    {
      id: 'agent-energy',
      name: '能源管理审核员',
      description: '负责能源管理体系审核',
      knowledge_base_version_ids: ['kbver-energy'],
    },
  ],
  knowledge_versions: [
    {
      id: 'kbver-energy',
      knowledge_base_id: 'kb-energy',
      name: '能源管理审核知识库',
      version: '1.0.0-branch.agent-energy.1',
      status: 'active',
      is_agent_branch: true,
    },
  ],
  audit_types: [{ value: '第一次监督审核', label: '第一次监督审核', code: 'C' }],
  management_systems: [{ value: '能源管理体系', label: '能源管理体系' }],
  material_types: [],
  supported_extensions: ['.pdf'],
  max_material_bytes: 1024,
} as AuditCaseManagementOptions & {
  agent_options: Array<{
    id: string;
    name: string;
    description?: string;
    knowledge_base_version_ids: string[];
  }>;
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('CreateAuditCaseDialog', () => {
  it('shows inherited employee knowledge as a collapsed scope until an admin adjusts it', async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <MemoryRouter>
          <CreateAuditCaseDialog open options={options} onOpenChange={() => undefined} />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect((await screen.findByRole('combobox', { name: '数字员工' }) as HTMLSelectElement).value).toBe('agent-energy');
    expect(screen.getByText('已自动继承 1 个知识库版本')).toBeTruthy();
    expect(screen.queryByRole('searchbox', { name: '搜索知识库' })).toBeNull();

    await user.click(screen.getByRole('button', { name: '调整知识范围' }));

    expect(screen.getByRole('searchbox', { name: '搜索知识库' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '恢复员工默认' })).toBeTruthy();
  });

  it('submits the selected employee and its frozen default knowledge snapshot', async () => {
    createAuditCaseMock.mockResolvedValue({ id: 'case-created' });
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <MemoryRouter>
          <CreateAuditCaseDialog open options={options} onOpenChange={() => undefined} />
        </MemoryRouter>
      </I18nProvider>,
    );

    await user.type(screen.getByRole('textbox', { name: '企业名称' }), '示例能源企业');
    await user.selectOptions(screen.getByRole('combobox', { name: '审核类型' }), '第一次监督审核');
    await user.click(screen.getByRole('button', { name: '创建项目' }));

    expect(createAuditCaseMock).toHaveBeenCalledWith(
      expect.objectContaining({
        agent_id: 'agent-energy',
        knowledge_scope_mode: 'agent_default',
        knowledge_base_version_ids: ['kbver-energy'],
      }),
    );
  });

  it('clears an abandoned custom knowledge draft when the dialog is cancelled', async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <MemoryRouter>
          <CreateAuditCaseDialog open options={options} onOpenChange={() => undefined} />
        </MemoryRouter>
      </I18nProvider>,
    );

    await user.click(await screen.findByRole('button', { name: '调整知识范围' }));
    await user.click(screen.getByRole('checkbox', { name: /能源管理审核知识库/ }));
    expect(screen.getByText('已自定义 0 个知识库版本')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '取消' }));

    expect(screen.getByText('已自动继承 1 个知识库版本')).toBeTruthy();
    expect(screen.queryByRole('searchbox', { name: '搜索知识库' })).toBeNull();
  });

  it('blocks project creation with a clear message when no digital employee is available', async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <MemoryRouter>
          <CreateAuditCaseDialog
            open
            options={{ ...options, agent_options: [] }}
            onOpenChange={() => undefined}
          />
        </MemoryRouter>
      </I18nProvider>,
    );

    await user.type(screen.getByRole('textbox', { name: '企业名称' }), '示例能源企业');
    await user.selectOptions(screen.getByRole('combobox', { name: '审核类型' }), '第一次监督审核');
    await user.click(screen.getByRole('button', { name: '创建项目' }));
    expect((await screen.findByRole('alert')).textContent).toContain('暂无可用数字员工');
  });
});
