// @vitest-environment jsdom

import { createElement, useState } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ModelCombobox, { type ModelComboboxOption } from './ModelCombobox';

afterEach(() => {
  cleanup();
});

const OPTIONS = [
  { value: 'gpt-4o', label: 'GPT-4o' },
  { value: 'gpt-4o-mini', label: 'GPT-4o mini' },
];

// A real usage feeds onChange's value back in as `value` — this wrapper does the
// same so typing tests exercise the actual controlled-input round trip instead
// of a `value` prop that never changes.
function ControlledCombobox({
  options,
  onChange,
}: {
  options: ModelComboboxOption[];
  onChange: (value: string) => void;
}) {
  const [value, setValue] = useState('');
  return createElement(ModelCombobox, {
    value,
    options,
    onChange: (next: string) => {
      setValue(next);
      onChange(next);
    },
  });
}

describe('ModelCombobox', () => {
  it('fills the input with the option value when an option is chosen', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(createElement(ModelCombobox, { value: '', onChange, options: OPTIONS }));

    await user.click(screen.getByRole('textbox'));
    await user.click(await screen.findByText('GPT-4o mini'));

    expect(onChange).toHaveBeenCalledWith('gpt-4o-mini');
  });

  it('lets the user type a value that is not in the options list (manual fallback)', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(createElement(ControlledCombobox, { options: [], onChange }));

    await user.type(screen.getByRole('textbox'), 'my-custom-model');

    expect(onChange).toHaveBeenLastCalledWith('my-custom-model');
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('my-custom-model');
  });

  it('filters the option list by the current input text', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(createElement(ModelCombobox, { value: '', onChange, options: OPTIONS }));

    await user.click(screen.getByRole('textbox'));
    await user.type(screen.getByRole('textbox'), 'mini');

    expect(screen.getByText('GPT-4o mini')).toBeTruthy();
    expect(screen.queryByText('GPT-4o', { selector: 'button *' })).toBeNull();
  });

  it('shows a loading indicator instead of the option list while loading', async () => {
    const user = userEvent.setup();
    render(createElement(ModelCombobox, { value: '', onChange: vi.fn(), options: [], loading: true }));

    await user.click(screen.getByRole('textbox'));

    expect(screen.getByText('正在获取模型列表…')).toBeTruthy();
  });

  it('is disabled when disabled is true', () => {
    render(createElement(ModelCombobox, { value: '', onChange: vi.fn(), options: OPTIONS, disabled: true }));
    expect((screen.getByRole('textbox') as HTMLInputElement).disabled).toBe(true);
  });
});
