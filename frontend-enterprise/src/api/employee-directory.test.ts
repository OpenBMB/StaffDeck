// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { api } from './client';
import { loadEmployeeDirectory } from './employee-directory';
import { setEnterpriseAuthSession } from '../auth';

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

it('coalesces overlapping reads only, uses summary and isolates subscriber cancellation', async () => {
  let done!: (rows: []) => void;
  const get = vi.spyOn(api, 'get').mockImplementation(() => new Promise(resolve => { done = resolve; }));
  const controller = new AbortController();
  const cancelled = loadEmployeeDirectory(controller.signal).catch(error => error.name);
  const other = loadEmployeeDirectory();
  expect(get).toHaveBeenCalledTimes(1);
  expect(get.mock.calls[0][0]).toContain('view=summary');
  controller.abort(); done([]);
  expect(await cancelled).toBe('AbortError');
  expect(await other).toEqual([]);
  get.mockResolvedValue([]);
  await loadEmployeeDirectory();
  expect(get).toHaveBeenCalledTimes(2);
});

it('does not reuse another login or retain a failure', async () => {
  const pending: ((value: []) => void)[] = [];
  const get = vi.spyOn(api, 'get').mockImplementation(() => new Promise(resolve => pending.push(resolve)));
  const first = loadEmployeeDirectory();
  setEnterpriseAuthSession({ token: 'second', user: { id: 'u2', username: 'u2', tenant_id: 't', role: 'member' } });
  const second = loadEmployeeDirectory();
  expect(get).toHaveBeenCalledTimes(2);
  pending.forEach(resolve => resolve([]));
  await Promise.all([first, second]);
  get.mockRejectedValueOnce(new Error('unavailable'));
  await expect(loadEmployeeDirectory()).rejects.toThrow('unavailable');
  get.mockResolvedValue([]);
  await loadEmployeeDirectory();
  expect(get).toHaveBeenCalledTimes(4);
});
