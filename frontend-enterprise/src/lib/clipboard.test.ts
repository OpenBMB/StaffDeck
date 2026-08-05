// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { copyTextToClipboard } from './clipboard';

const originalClipboard = navigator.clipboard;
const originalExecCommand = document.execCommand;

function setClipboard(value: Clipboard | undefined) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value });
}

function setExecCommand(value: typeof document.execCommand | undefined) {
  Object.defineProperty(document, 'execCommand', { configurable: true, value });
}

function copyEventExecCommand() {
  const setData = vi.fn();
  const execCommand = vi.fn((command: string) => {
    if (command !== 'copy') return false;
    const event = new Event('copy', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: { setData } });
    document.dispatchEvent(event);
    return true;
  });
  return { execCommand, setData };
}

afterEach(() => {
  setClipboard(originalClipboard);
  setExecCommand(originalExecCommand);
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe('copyTextToClipboard', () => {
  it('uses the Clipboard API when available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const execCommand = vi.fn(() => true);
    setClipboard({ writeText } as unknown as Clipboard);
    setExecCommand(execCommand);

    await copyTextToClipboard('hello');

    expect(writeText).toHaveBeenCalledWith('hello');
    expect(execCommand).not.toHaveBeenCalled();
  });

  it('falls back to a selected textarea when Clipboard API rejects', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('permission denied'));
    const { execCommand, setData } = copyEventExecCommand();
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    setClipboard({ writeText } as unknown as Clipboard);
    setExecCommand(execCommand);

    await copyTextToClipboard('fallback text');

    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(setData).toHaveBeenCalledWith('text/plain', 'fallback text');
    expect(document.activeElement).toBe(input);
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('does not report success when execCommand emits no copy event', async () => {
    setClipboard(undefined);
    setExecCommand(vi.fn(() => true));

    await expect(copyTextToClipboard('not actually copied')).rejects.toThrow(
      'Clipboard access is unavailable',
    );
  });

  it('reports failure when neither copy method succeeds', async () => {
    setClipboard(undefined);
    setExecCommand(vi.fn(() => false));

    await expect(copyTextToClipboard('unavailable')).rejects.toThrow('Clipboard access is unavailable');
  });
});
