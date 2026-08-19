// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  ENTERPRISE_AUTH_STORAGE_KEY,
  type EnterpriseAuthSession,
} from '../auth';
import { I18nProvider } from '../i18n';

import LoginPage from './LoginPage';

const session: EnterpriseAuthSession = {
  token: 'token-1',
  user: {
    id: 'user-1',
    tenant_id: 'tenant_demo',
    username: 'admin',
    role: 'admin',
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Unauthorized',
    text: async () => JSON.stringify(body),
  } as Response;
}

function renderLogin(onLogin = vi.fn()) {
  render(
    <I18nProvider>
      <LoginPage onLogin={onLogin} />
    </I18nProvider>,
  );
  return onLogin;
}

async function showFormAndEnterCredentials(
  user: ReturnType<typeof userEvent.setup>,
  username = 'admin',
  password = 'secret',
) {
  await user.click(screen.getByRole('button', { name: '登录' }));
  await user.type(screen.getByLabelText('账号'), username);
  await user.type(screen.getByLabelText('密码'), password);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('LoginPage', () => {
  it('shows the original landing hero before revealing credentials', async () => {
    const user = userEvent.setup();
    renderLogin();

    expect(screen.getByText('我们来做什么？')).toBeTruthy();
    expect(screen.getByAltText('StaffDeck 产品预览')).toBeTruthy();
    expect(screen.queryByLabelText('账号')).toBeNull();

    await user.click(screen.getByRole('button', { name: '登录' }));

    expect(screen.getByLabelText('账号')).toBeTruthy();
    expect(screen.getByLabelText('密码')).toBeTruthy();
  });

  it('toggles the password between hidden and visible text', async () => {
    const user = userEvent.setup();
    renderLogin();
    await user.click(screen.getByRole('button', { name: '登录' }));
    const password = screen.getByLabelText('密码');

    expect(password.getAttribute('type')).toBe('password');
    await user.click(screen.getByRole('button', { name: '显示密码' }));
    expect(password.getAttribute('type')).toBe('text');
    await user.click(screen.getByRole('button', { name: '隐藏密码' }));
    expect(password.getAttribute('type')).toBe('password');
  });

  it('posts trimmed credentials with the configured tenant', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse(session));
    vi.stubGlobal('fetch', fetchMock);
    renderLogin();

    await showFormAndEnterCredentials(user, '  admin  ', '  secret  ');
    await user.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        tenant_id: 'tenant_demo',
        username: 'admin',
        password: 'secret',
      }),
    }));
  });

  it('stores the session and notifies the caller after a successful login', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(session)));
    const onLogin = renderLogin();

    await showFormAndEnterCredentials(user);
    await user.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => expect(onLogin).toHaveBeenCalledWith(session));
    expect(JSON.parse(window.localStorage.getItem(ENTERPRISE_AUTH_STORAGE_KEY) || 'null'))
      .toEqual(session);
  });
});
