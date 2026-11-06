// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SearchableSelect from './SearchableSelect';
import { I18nProvider } from '@/i18n';

const options = [
  { value: 'u1', label: '张三', keywords: ['zhangsan'] },
  { value: 'u2', label: '李四', keywords: ['lisi'] },
  { value: 'u3', label: '王五', keywords: ['wangwu'] },
];

function renderSelect(props: Partial<React.ComponentProps<typeof SearchableSelect>> = {}) {
  const onValueChange = vi.fn();
  return {
    onValueChange,
    ...render(
      <I18nProvider>
        <SearchableSelect
          value=""
          onValueChange={onValueChange}
          options={options}
          placeholder="选择内部成员"
          searchPlaceholder="搜索成员"
          emptyText="无匹配成员"
          {...props}
        />
      </I18nProvider>,
    ),
  };
}

afterEach(() => {
  cleanup();
});

describe('SearchableSelect', () => {
  it('opens the list, focuses search and filters options by label', async () => {
    const user = userEvent.setup();
    const { onValueChange } = renderSelect();

    await user.click(screen.getByRole('combobox'));
    const input = await screen.findByPlaceholderText('搜索成员');
    expect(document.activeElement).toBe(input);
    expect(await screen.findByRole('option', { name: /张三/ })).toBeTruthy();

    await user.type(input, '李');
    await waitFor(() => {
      expect(screen.queryByRole('option', { name: /张三/ })).toBeNull();
    });
    expect(screen.getByRole('option', { name: /李四/ })).toBeTruthy();

    await user.click(screen.getByRole('option', { name: /李四/ }));
    expect(onValueChange).toHaveBeenCalledWith('u2');
    // 选中后关闭
    await waitFor(() => {
      expect(screen.queryByRole('option', { name: /李四/ })).toBeNull();
    });
  });

  it('filters by keywords and shows empty text when nothing matches', async () => {
    const user = userEvent.setup();
    renderSelect();

    await user.click(screen.getByRole('combobox'));
    const input = await screen.findByPlaceholderText('搜索成员');
    await user.type(input, 'wangwu');
    expect(screen.getByRole('option', { name: /王五/ })).toBeTruthy();

    await user.clear(input);
    await user.type(input, '不存在');
    expect(screen.getByText('无匹配成员')).toBeTruthy();
    expect(screen.queryByRole('option')).toBeNull();
  });

  it('supports keyboard navigation and Enter to select', async () => {
    const user = userEvent.setup();
    const { onValueChange } = renderSelect();

    await user.click(screen.getByRole('combobox'));
    const input = await screen.findByPlaceholderText('搜索成员');
    await user.type(input, '张');
    await user.keyboard('{Enter}');
    expect(onValueChange).toHaveBeenCalledWith('u1');
  });

  it('shows the selected label on the trigger', () => {
    renderSelect({ value: 'u2' });
    expect(screen.getByRole('combobox').textContent).toContain('李四');
  });
});
