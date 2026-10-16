// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest';

import {
  ENTERPRISE_AUTH_STORAGE_KEY,
  ENTERPRISE_REMEMBERED_USERNAME_STORAGE_KEY,
  ENTERPRISE_REMEMBER_LOGIN_STORAGE_KEY,
  getEnterpriseAuthSession,
  isEnterpriseAuthSessionExpired,
  mergeEnterpriseAuthSessionUser,
  readRememberedUsername,
  readRememberLoginPreference,
  setEnterpriseAuthSession,
  writeRememberedUsername,
  writeRememberLoginPreference,
  type EnterpriseAuthUser,
} from './auth';

const user: EnterpriseAuthUser = {
  id: 'user-1',
  tenant_id: 'tenant_demo',
  username: 'chenjl5',
  role: 'member',
};

const nowSeconds = () => Math.floor(Date.now() / 1000);

beforeEach(() => {
  window.localStorage.clear();
});

describe('enterprise auth session expiry', () => {
  it('keeps a remembered session alive through the 30-day window', () => {
    const expires_at = nowSeconds() + 60 * 60 * 24 * 30;
    setEnterpriseAuthSession({ token: 'token-1', user, expires_at });

    const restored = getEnterpriseAuthSession();
    expect(restored?.token).toBe('token-1');
    expect(restored?.expires_at).toBe(expires_at);
    expect(isEnterpriseAuthSessionExpired(restored!)).toBe(false);
  });

  it('drops an expired session instead of booting the app on it', () => {
    setEnterpriseAuthSession({ token: 'token-1', user, expires_at: nowSeconds() - 1 });

    expect(getEnterpriseAuthSession()).toBeNull();
    // 过期记录要连痕迹一起清掉，别留着下次又被读一遍
    expect(window.localStorage.getItem(ENTERPRISE_AUTH_STORAGE_KEY)).toBeNull();
  });

  it('pre-expires within the clock-skew margin so no doomed request is sent', () => {
    setEnterpriseAuthSession({ token: 'token-1', user, expires_at: nowSeconds() + 5 });

    expect(getEnterpriseAuthSession()).toBeNull();
  });

  it('treats a session without expires_at as before (old records keep working)', () => {
    setEnterpriseAuthSession({ token: 'token-1', user });

    expect(getEnterpriseAuthSession()?.token).toBe('token-1');
  });

  it('refresh keeps the token and the expiry window', () => {
    const expires_at = nowSeconds() + 60 * 60 * 24 * 30;
    const merged = mergeEnterpriseAuthSessionUser(
      { token: 'token-1', user, expires_at },
      { ...user, department: '市场部' },
    );

    expect(merged.token).toBe('token-1');
    expect(merged.expires_at).toBe(expires_at);
    expect(merged.user.department).toBe('市场部');
  });
});

describe('remember-account preference', () => {
  it('remembers the account name only while the preference is on', () => {
    writeRememberLoginPreference(true);
    writeRememberedUsername('chenjl5');

    expect(readRememberLoginPreference()).toBe(true);
    expect(readRememberedUsername()).toBe('chenjl5');
    expect(window.localStorage.getItem(ENTERPRISE_REMEMBERED_USERNAME_STORAGE_KEY)).toBe('chenjl5');
  });

  it('clears both flags when the user opts out', () => {
    writeRememberLoginPreference(true);
    writeRememberedUsername('chenjl5');

    writeRememberLoginPreference(false);
    writeRememberedUsername(null);

    expect(readRememberLoginPreference()).toBe(false);
    expect(readRememberedUsername()).toBe('');
    expect(window.localStorage.getItem(ENTERPRISE_REMEMBER_LOGIN_STORAGE_KEY)).toBeNull();
    expect(window.localStorage.getItem(ENTERPRISE_REMEMBERED_USERNAME_STORAGE_KEY)).toBeNull();
  });

  it('defaults to off on a clean browser', () => {
    expect(readRememberLoginPreference()).toBe(false);
    expect(readRememberedUsername()).toBe('');
  });
});
