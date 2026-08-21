// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import { EditableCapabilityReferencesLine } from './DistillPage';

describe('SOP capability references', () => {
  it('hides unavailable resources unless the node already references them', async () => {
    const user = userEvent.setup();

    render(
      <I18nProvider>
        <EditableCapabilityReferencesLine
          label="SOP 技能"
          values={[]}
          requiredValues={[]}
          options={[
            {
              value: 'skill_active',
              label: '可用技能',
              unavailableReason: undefined,
            },
            {
              value: 'skill_archived',
              label: '已停用技能',
              unavailableReason: '技能未启用',
            },
          ]}
          emptyText="未指定技能"
          onChange={vi.fn()}
          onRequiredChange={vi.fn()}
        />
      </I18nProvider>,
    );

    await user.click(screen.getByRole('button', { name: /选择/ }));

    expect(screen.getByText('可用技能')).toBeTruthy();
    expect(screen.queryByText('已停用技能')).toBeNull();
  });

  it('removes an unavailable optional reference with one state update', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onRequiredChange = vi.fn();

    render(
      <I18nProvider>
        <EditableCapabilityReferencesLine
          label="SOP 工具"
          values={['tool_missing']}
          requiredValues={[]}
          options={[]}
          emptyText="未指定工具"
          onChange={onChange}
          onRequiredChange={onRequiredChange}
        />
      </I18nProvider>,
    );

    await user.click(screen.getByRole('button', { name: /已选择 1 个/ }));
    await user.click(screen.getByRole('checkbox', { name: '取消选择tool_missing' }));

    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith([]);
    expect(onRequiredChange).not.toHaveBeenCalled();
  });
});
