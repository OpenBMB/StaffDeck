// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AuditCaseRead } from '@/types';

import AuditCasePanel from './AuditCasePanel';

const auditCase: AuditCaseRead = {
  id: 'case-1',
  tenant_id: 'tenant_demo',
  owner_user_id: 'user-1',
  member_user_ids: [],
  organization_name: '示例企业',
  report_type: '再认证',
  management_systems: ['GB/T 23331-2020'],
  status: 'active',
  knowledge_base_version_ids: [],
  created_at: '',
  updated_at: '',
};

afterEach(() => cleanup());

describe('AuditCasePanel', () => {
  it('selects an existing case without asking for files', async () => {
    const onSelect = vi.fn();
    render(
      <AuditCasePanel
        cases={[auditCase]}
        selectedId={null}
        materials={[]}
        coverage={null}
        loading={false}
        onSelect={onSelect}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /示例企业/ }));

    expect(onSelect).toHaveBeenCalledWith(auditCase.id);
    expect(screen.queryByText('请重新上传')).toBeNull();
  });
});
