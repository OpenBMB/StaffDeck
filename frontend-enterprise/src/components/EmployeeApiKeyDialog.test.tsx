// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { copyTextToClipboard } from './EmployeeApiKeyDialog';

describe('copyTextToClipboard', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('uses the Clipboard API when the browser allows it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const execCommand = vi.fn();
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    });

    await copyTextToClipboard('sd_live_example');

    expect(writeText).toHaveBeenCalledWith('sd_live_example');
    expect(execCommand).not.toHaveBeenCalled();
  });

  it('falls back to a temporary textarea when Clipboard API is rejected', async () => {
    const writeText = vi.fn().mockRejectedValue(new DOMException('Not allowed'));
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    });

    await copyTextToClipboard('sd_live_fallback');

    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(document.querySelector('textarea[aria-hidden="true"]')).toBeNull();
  });

  it('reports failure when both copy mechanisms are rejected', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn().mockReturnValue(false),
    });

    await expect(copyTextToClipboard('sd_live_rejected')).rejects.toThrow(
      'Copy command was rejected',
    );
    expect(document.querySelector('textarea[aria-hidden="true"]')).toBeNull();
  });
});
