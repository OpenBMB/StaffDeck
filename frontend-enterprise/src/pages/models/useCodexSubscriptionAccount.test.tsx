// @vitest-environment jsdom

import { createElement, type ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/api/client';
import { I18nProvider, useI18n } from '@/i18n';
import type { CodexSubscriptionAccountRead } from '@/types';
import { useCodexSubscriptionAccount } from './useCodexSubscriptionAccount';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    api: { ...actual.api, get: vi.fn(), post: vi.fn() },
  };
});

const mockedGet = vi.mocked(api.get);
const mockedPost = vi.mocked(api.post);

const requiresLogin: CodexSubscriptionAccountRead = {
  status: 'requires_login',
  plan_type: null,
  message: '未登录',
};

const pending: CodexSubscriptionAccountRead = {
  status: 'pending',
  plan_type: null,
  message: '等待登录',
};

const connected: CodexSubscriptionAccountRead = {
  status: 'connected',
  plan_type: 'Plus',
  message: '已连接',
};

/** 为 hook 提供真实的应用本地化上下文。 */
function Wrapper({ children }: { children: ReactNode }) {
  return createElement(I18nProvider, null, children);
}

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  window.localStorage.setItem('staffdeck_locale', 'zh-CN');
});

describe('useCodexSubscriptionAccount', () => {
  it('loads and updates the subscription account in the caller tenant', async () => {
    mockedGet.mockResolvedValueOnce(requiresLogin);
    mockedPost.mockResolvedValueOnce(pending);

    const { result } = renderHook(
      () => useCodexSubscriptionAccount({ tenantId: 'tenant-isolated' }),
      { wrapper: Wrapper },
    );

    await waitFor(() => expect(result.current.account).toEqual(requiresLogin));
    expect(mockedGet).toHaveBeenCalledWith(
      '/api/enterprise/model-configs/codex-subscription/account?tenant_id=tenant-isolated',
    );

    await act(async () => result.current.startLogin());

    expect(mockedPost).toHaveBeenCalledWith(
      '/api/enterprise/model-configs/codex-subscription/login?tenant_id=tenant-isolated',
    );
    expect(result.current.account).toEqual(pending);
  });

  it('polls every two seconds only while login remains pending', async () => {
    vi.useFakeTimers();
    mockedGet.mockResolvedValueOnce(pending).mockResolvedValueOnce(connected);

    const { result } = renderHook(
      () => useCodexSubscriptionAccount({ tenantId: 'tenant-isolated' }),
      { wrapper: Wrapper },
    );

    await act(async () => Promise.resolve());
    expect(result.current.account).toEqual(pending);
    expect(mockedGet).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(result.current.account).toEqual(connected);
    expect(mockedGet).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(mockedGet).toHaveBeenCalledTimes(2);
  });

  it('does not read subscription state when its consumer is disabled', async () => {
    const { result } = renderHook(
      () => useCodexSubscriptionAccount({ tenantId: 'tenant-isolated', enabled: false }),
      { wrapper: Wrapper },
    );

    await act(async () => Promise.resolve());
    expect(result.current.account).toBeNull();
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it('does not expose a stale account response after the caller tenant changes', async () => {
    let resolveFirstRequest: ((account: CodexSubscriptionAccountRead) => void) | undefined;
    mockedGet.mockImplementation((url: unknown) => {
      if (String(url).includes('tenant-first')) {
        return new Promise<CodexSubscriptionAccountRead>((resolve) => {
          resolveFirstRequest = resolve;
        });
      }
      return Promise.resolve(connected);
    });

    const { result, rerender } = renderHook(
      ({ tenantId }) => useCodexSubscriptionAccount({ tenantId }),
      { initialProps: { tenantId: 'tenant-first' }, wrapper: Wrapper },
    );

    rerender({ tenantId: 'tenant-second' });
    await waitFor(() => expect(result.current.account).toEqual(connected));

    await act(async () => {
      resolveFirstRequest?.(requiresLogin);
      await Promise.resolve();
    });
    expect(result.current.account).toEqual(connected);
  });

  it('does not let an older account read overwrite a newer login action for the same tenant', async () => {
    let resolveInitialRead: ((account: CodexSubscriptionAccountRead) => void) | undefined;
    mockedGet.mockImplementationOnce(
      () =>
        new Promise<CodexSubscriptionAccountRead>((resolve) => {
          resolveInitialRead = resolve;
        }),
    );
    mockedPost.mockResolvedValueOnce(pending);

    const { result } = renderHook(
      () => useCodexSubscriptionAccount({ tenantId: 'tenant-isolated' }),
      { wrapper: Wrapper },
    );
    expect(mockedGet).toHaveBeenCalledTimes(1);

    await act(async () => result.current.startLogin());
    expect(result.current.account).toEqual(pending);

    await act(async () => {
      resolveInitialRead?.(requiresLogin);
      await Promise.resolve();
    });
    expect(result.current.account).toEqual(pending);
  });

  it('blocks a queued poll after cancellation starts so the cancellation result remains current', async () => {
    mockedGet.mockResolvedValueOnce(pending).mockResolvedValueOnce(connected);
    mockedPost.mockResolvedValueOnce(requiresLogin);

    const { result } = renderHook(
      () => useCodexSubscriptionAccount({ tenantId: 'tenant-isolated' }),
      { wrapper: Wrapper },
    );
    await waitFor(() => expect(result.current.account).toEqual(pending));

    await act(async () => {
      const cancellation = result.current.cancelLogin();
      const queuedPoll = result.current.reload();
      await Promise.all([cancellation, queuedPoll]);
    });

    expect(mockedGet).toHaveBeenCalledTimes(1);
    expect(result.current.account).toEqual(requiresLogin);
    expect(result.current.loading).toBe(false);
  });

  it('keeps an account action current when the application locale changes', async () => {
    let resolveLogin: ((account: CodexSubscriptionAccountRead) => void) | undefined;
    mockedGet.mockResolvedValueOnce(requiresLogin).mockResolvedValueOnce(connected);
    mockedPost.mockImplementationOnce(
      () =>
        new Promise<CodexSubscriptionAccountRead>((resolve) => {
          resolveLogin = resolve;
        }),
    );

    const { result } = renderHook(
      () => {
        const subscription = useCodexSubscriptionAccount({ tenantId: 'tenant-isolated' });
        const { setLocale } = useI18n();
        return { ...subscription, setLocale };
      },
      { wrapper: Wrapper },
    );
    await waitFor(() => expect(result.current.account).toEqual(requiresLogin));

    let loginPromise: Promise<void> | undefined;
    act(() => {
      loginPromise = result.current.startLogin();
    });
    act(() => result.current.setLocale('en-US'));
    await act(async () => {
      resolveLogin?.(pending);
      await loginPromise;
    });

    expect(mockedGet).toHaveBeenCalledTimes(1);
    expect(result.current.account).toEqual(pending);
    expect(result.current.loading).toBe(false);
  });
});
