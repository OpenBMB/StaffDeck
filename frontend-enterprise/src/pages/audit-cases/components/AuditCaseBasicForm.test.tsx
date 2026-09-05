// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { useState } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AuditCaseManagementOptions } from '@/types';
import { I18nProvider } from '@/i18n';

import type { AuditCaseCreateRequest } from '../auditCaseTypes';
import { AuditCaseBasicForm } from './AuditCaseBasicForm';

const options: AuditCaseManagementOptions = {
  knowledge_versions: [],
  audit_types: [
    { value: '一阶段审核', label: '一阶段审核', code: 'A' },
    { value: '认证审核', label: '认证审核', code: 'B' },
  ],
  management_systems: [
    { value: '质量管理体系', label: '质量管理体系' },
    { value: '环境管理体系', label: '环境管理体系' },
  ],
  material_types: [],
  supported_extensions: ['.txt'],
  max_material_bytes: 1024,
};

const draft: AuditCaseCreateRequest = {
  tenant_id: 'tenant_demo',
  agent_id: '',
  knowledge_scope_mode: 'custom',
  organization_name: '示例企业',
  report_type: '一阶段审核',
  management_systems: [],
  knowledge_base_version_ids: [],
  member_user_ids: [],
};

function renderForm(
  formDraft: AuditCaseCreateRequest,
  onChange: (patch: Partial<AuditCaseCreateRequest>) => void,
) {
  return render(
    <I18nProvider>
      <AuditCaseBasicForm draft={formDraft} options={options} onChange={onChange} />
    </I18nProvider>,
  );
}

function renderControlledForm(
  initialDraft: AuditCaseCreateRequest,
  onChange: (patch: Partial<AuditCaseCreateRequest>) => void,
) {
  function ControlledForm() {
    const [current, setCurrent] = useState(initialDraft);
    return (
      <AuditCaseBasicForm
        draft={current}
        options={options}
        onChange={(patch) => {
          onChange(patch);
          setCurrent((previous) => ({ ...previous, ...patch }));
        }}
      />
    );
  }
  return render(<I18nProvider><ControlledForm /></I18nProvider>);
}

afterEach(() => cleanup());

describe('AuditCaseBasicForm', () => {
  it('selects one audit type from the server-owned options', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderForm(draft, onChange);

    await user.selectOptions(screen.getByRole('combobox', { name: '审核类型' }), '认证审核');

    expect(onChange).toHaveBeenLastCalledWith({ report_type: '认证审核' });
  });

  it('selects multiple management systems and shows selected tags', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderControlledForm(draft, onChange);

    await user.click(screen.getByRole('button', { name: '选择管理体系' }));
    await user.click(screen.getByRole('checkbox', { name: '质量管理体系' }));
    await user.click(screen.getByRole('checkbox', { name: '环境管理体系' }));

    expect(onChange).toHaveBeenLastCalledWith({
      management_systems: ['质量管理体系', '环境管理体系'],
    });
    expect(screen.getAllByText('质量管理体系').length).toBeGreaterThan(0);
    expect(screen.getAllByText('环境管理体系').length).toBeGreaterThan(0);
  });

  it('keeps an existing historical management-system value selectable', async () => {
    const user = userEvent.setup();
    renderForm({ ...draft, management_systems: ['历史管理体系'] }, vi.fn());

    await user.click(screen.getByRole('button', { name: '选择管理体系' }));

    expect(screen.getByRole('checkbox', { name: '历史管理体系' })).toBeTruthy();
    expect(screen.getAllByText('历史管理体系').length).toBeGreaterThan(0);
  });
});
