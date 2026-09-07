import { afterEach, expect, it, vi } from 'vitest';

afterEach(() => { vi.unstubAllEnvs(); vi.resetModules(); });

it('isolates a prefixed instance without rewriting external URLs or double-prefixing', async () => {
  vi.stubEnv('BASE_URL', '/dsh/');
  const { APP_BASE, appPath } = await import('./app-path');
  const { ENTERPRISE_AUTH_STORAGE_KEY } = await import('../auth');
  expect(APP_BASE).toBe('/dsh');
  expect(appPath('/api/auth/me')).toBe('/dsh/api/auth/me');
  expect(appPath('/dsh/login')).toBe('/dsh/login');
  expect(appPath('https://example.com/a')).toBe('https://example.com/a');
  expect(appPath('//example.com/a')).toBe('//example.com/a');
  expect(ENTERPRISE_AUTH_STORAGE_KEY).toBe('ultrarag_auth:/dsh');
});

it('keeps the root deployment compatible', async () => {
  vi.stubEnv('BASE_URL', '/');
  const { appPath } = await import('./app-path');
  const { ENTERPRISE_AUTH_STORAGE_KEY } = await import('../auth');
  expect(appPath('/api/auth/me')).toBe('/api/auth/me');
  expect(ENTERPRISE_AUTH_STORAGE_KEY).toBe('ultrarag_auth');
});
