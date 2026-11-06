// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HarnessWorkspaceArtifact } from '@/types';

import HarnessArtifactDownloads from './HarnessArtifactDownloads';

const mocks = vi.hoisted(() => ({
  blob: vi.fn(),
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
}));

vi.mock('@/api/client', () => ({
  api: { blob: mocks.blob },
}));

vi.mock('@/components/ui/app-toast', () => ({
  notify: {
    error: mocks.notifyError,
    success: mocks.notifySuccess,
  },
}));

const artifact: HarnessWorkspaceArtifact = {
  type: 'workspace_file',
  task_frame_id: 'task/frame',
  path: 'reports/quarterly summary.txt',
  display_name: 'Q2 财务报告.txt',
  description: '最终交付版',
  size: 2048,
};

beforeEach(() => {
  mocks.blob.mockReset();
  mocks.notifyError.mockReset();
  mocks.notifySuccess.mockReset();
  vi.spyOn(window.URL, 'createObjectURL').mockReturnValue('blob:artifact');
  vi.spyOn(window.URL, 'revokeObjectURL').mockImplementation(() => undefined);
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Harness artifact downloads', () => {
  it('downloads through the authenticated blob API with scoped identifiers', async () => {
    const user = userEvent.setup();
    mocks.blob.mockResolvedValue(new Blob(['artifact']));
    render(
      <HarnessArtifactDownloads
        artifacts={[artifact]}
        tenantId="tenant demo"
        sessionId="session demo"
      />,
    );

    expect(screen.getByText('Q2 财务报告.txt')).toBeTruthy();
    expect(screen.getByText(/最终交付版 · 2\.0 KB$/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: /Q2 财务报告\.txt$/ }));

    await waitFor(() => {
      expect(mocks.blob).toHaveBeenCalledWith(
        '/api/chat/sessions/session%20demo/artifacts/task%2Fframe'
          + '?tenant_id=tenant+demo&path=reports%2Fquarterly+summary.txt',
      );
    });
    expect(window.URL.createObjectURL).toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
    expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:artifact');
    expect(mocks.notifySuccess).toHaveBeenCalledWith(
      expect.stringContaining('Q2 财务报告.txt'),
    );
  });

  it('keeps the action disabled without a persisted session', () => {
    render(
      <HarnessArtifactDownloads
        artifacts={[artifact]}
        tenantId="tenant demo"
        sessionId=""
      />,
    );

    const button = screen.getByRole(
      'button',
      { name: /Q2 财务报告\.txt$/ },
    ) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(mocks.blob).not.toHaveBeenCalled();
  });

  it('loads generated image artifacts through the authenticated API and renders a preview', async () => {
    const imageArtifact: HarnessWorkspaceArtifact = {
      ...artifact,
      path: 'charts/趋势图.png',
      display_name: '趋势图.png',
      content_type: 'image/png',
      size: 4096,
    };
    mocks.blob.mockResolvedValue(new Blob(['image'], { type: 'image/png' }));

    render(
      <HarnessArtifactDownloads
        artifacts={[imageArtifact]}
        tenantId="tenant demo"
        sessionId="session demo"
      />,
    );

    await waitFor(() => {
      expect(mocks.blob).toHaveBeenCalledWith(
        '/api/chat/sessions/session%20demo/artifacts/task%2Fframe'
          + '?tenant_id=tenant+demo&path=charts%2F%E8%B6%8B%E5%8A%BF%E5%9B%BE.png',
      );
      expect(screen.getByRole('img', { name: '趋势图.png' })).toBeTruthy();
    });
    expect(screen.getByRole('link', { name: '查看图片 趋势图.png' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '下载图片 趋势图.png' })).toBeTruthy();
  });

  it('reports a failed download without creating an object URL', async () => {
    const user = userEvent.setup();
    mocks.blob.mockRejectedValue(new Error('Artifact not found'));
    render(
      <HarnessArtifactDownloads
        artifacts={[artifact]}
        tenantId="tenant demo"
        sessionId="session demo"
      />,
    );

    await user.click(screen.getByRole('button', { name: /Q2 财务报告\.txt$/ }));

    await waitFor(() => {
      expect(mocks.notifyError).toHaveBeenCalledWith('Artifact not found');
    });
    expect(window.URL.createObjectURL).not.toHaveBeenCalled();
  });
});
