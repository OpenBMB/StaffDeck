// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useToolTest } from './useToolTest';

const mocks = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock('../api/client', () => ({ TENANT_ID: 'tenant_demo', api: { postWithSignal: mocks.post, getWithSignal: mocks.get } }));
vi.mock('../auth', () => ({ getEnterpriseAuthSession: () => ({ user: { id: 'admin' } }) }));

beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('async tool test tracking', () => {
  it('works on HTTP origins without crypto.randomUUID and with unavailable storage', async () => {
    vi.stubGlobal('crypto', {});
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
    mocks.post.mockResolvedValue({ success: true });
    const { result } = renderHook(() => useToolTest('tool1', ''));
    await act(async () => { await result.current.submit({}); });
    expect(result.current.status).toBe('已返回');
    expect(mocks.post.mock.calls[0][1].client_request_id).toMatch(/^[a-z0-9-]+$/);
  });
  it('submits once, polls through completion and exposes provider result', async () => {
    mocks.post.mockResolvedValue({ success: true, data: { detached: true, task_id: 'task1' } });
    mocks.get.mockResolvedValueOnce({ id: 'task1', status: 'working', poll_attempts: 1 })
      .mockResolvedValueOnce({ id: 'task1', status: 'completed', result: 'done', poll_attempts: 2 });
    const { result } = renderHook(() => useToolTest('tool1', '', 1));
    await act(async () => { await result.current.submit({}); });
    expect(result.current.busy).toBe(true);
    expect(result.current.status).toBe('执行中');
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.status).toBe('已完成');
    expect(result.current.result).toContain('done');
    expect(result.current.taskId).toBeNull();
    expect(mocks.post).toHaveBeenCalledTimes(1);
    expect(mocks.get).toHaveBeenCalledTimes(2);
  });

  it('retrying a failed status query never submits another task', async () => {
    mocks.post.mockResolvedValue({ success: true, data: { detached: true, task_id: 'task1' } });
    mocks.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ id: 'task1', status: 'completed' });
    const { result } = renderHook(() => useToolTest('tool1', ''));
    await act(async () => { await result.current.submit({}); });
    expect(result.current.taskId).toBe('task1');
    expect(result.current.error).toContain('不会重新提交');
    await act(async () => { await result.current.submit({}); result.current.retryStatus(); });
    expect(result.current.status).toBe('已完成');
    expect(mocks.post).toHaveBeenCalledTimes(1);
  });

  it('reopens an unfinished task using only GET', async () => {
    sessionStorage.setItem('staffdeck-tool-test:tenant_demo:admin:tool1', JSON.stringify({ taskId: 'task1' }));
    mocks.get.mockResolvedValue({ id: 'task1', status: 'working' });
    const hook = renderHook(() => useToolTest('tool1', ''));
    await act(async () => {});
    expect(hook.result.current.taskId).toBe('task1');
    expect(mocks.post).not.toHaveBeenCalled();
    const signal = mocks.get.mock.calls[0][1] as AbortSignal;
    hook.unmount();
    expect(signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(10000);
    expect(mocks.get).toHaveBeenCalledTimes(1);
  });

  it('preserves idempotency key and original arguments when submission result is unknown', async () => {
    mocks.post.mockRejectedValueOnce(new Error('timeout')).mockResolvedValueOnce({ success: true });
    const { result } = renderHook(() => useToolTest('tool1', ''));
    await act(async () => { await result.current.submit({ value: 1 }); });
    await act(async () => { await result.current.submit({ value: 2 }); });
    expect(mocks.post.mock.calls[1][1]).toEqual(mocks.post.mock.calls[0][1]);
  });

  it('does not enable re-submission for an unknown Provider outcome', async () => {
    mocks.post.mockResolvedValue({ success: true, data: { detached: true, task_id: 'task1' } });
    mocks.get.mockResolvedValue({ id: 'task1', status: 'outcome_unknown' });
    const { result } = renderHook(() => useToolTest('tool1', ''));
    await act(async () => { await result.current.submit({}); });
    expect(result.current.status).toBe('提交结果待核实');
    await act(async () => { await result.current.submit({}); });
    expect(mocks.post).toHaveBeenCalledTimes(1);
  });

  it('blocks rapid double clicks before state updates', async () => {
    mocks.post.mockResolvedValue({ success: true });
    const { result } = renderHook(() => useToolTest('tool1', ''));
    await act(async () => { await Promise.all([result.current.submit({}), result.current.submit({})]); });
    expect(mocks.post).toHaveBeenCalledTimes(1);
  });
});
