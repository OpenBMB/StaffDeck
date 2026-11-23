// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useToolTest } from './useToolTest';

const mocks = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock('../api/client', () => ({ TENANT_ID: 'tenant_demo', api: { post: mocks.post, get: mocks.get } }));
vi.mock('../auth', () => ({ getEnterpriseAuthSession: () => ({ user: { id: 'admin' } }) }));

beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('async tool test tracking', () => {
  it('submits once, polls through completion and exposes provider result', async () => {
    mocks.post.mockResolvedValue({ success: true, data: { accepted: true, task_id: 'task1' } });
    mocks.get.mockResolvedValueOnce({ id: 'task1', status: 'working', result: null })
      .mockResolvedValueOnce({ id: 'task1', status: 'completed', result: { answer: 42 } });
    const { result } = renderHook(() => useToolTest('tool1'));
    await act(async () => { await result.current.submit('/api/enterprise/tools/tool1/test', { value: 1 }); });
    expect(result.current.busy).toBe(true);
    // Polling starts immediately with the accepted task id.
    expect(result.current.status).toBe('working');
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(result.current.status).toBe('completed');
    expect(result.current.result).toMatchObject({ id: 'task1', status: 'completed', result: { answer: 42 } });
    expect(mocks.post).toHaveBeenCalledTimes(1);
    expect(mocks.get).toHaveBeenCalledTimes(2);
  });

  it('sends the tenant id and tool id while polling the same task id', async () => {
    mocks.post.mockResolvedValue({ success: true, data: { accepted: true, task_id: 'task1' } });
    mocks.get.mockResolvedValue({ id: 'task1', status: 'completed' });
    const { result } = renderHook(() => useToolTest('tool1'));
    await act(async () => { await result.current.submit('/api/enterprise/tools/tool1/test', { value: 1 }); });
    expect(mocks.post.mock.calls[0][1]).toMatchObject({ tenant_id: 'tenant_demo', arguments: { value: 1 } });
    const pollUrl = mocks.get.mock.calls[0][0] as string;
    expect(pollUrl).toContain('/api/enterprise/external-business-tasks/task1');
    expect(pollUrl).toContain('tenant_id=tenant_demo');
    expect(pollUrl).toContain('tool_id=tool1');
  });

  it('never re-submits after a failed status query; it keeps polling the same task', async () => {
    mocks.post.mockResolvedValue({ success: true, data: { accepted: true, task_id: 'task1' } });
    mocks.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ id: 'task1', status: 'completed' });
    const { result } = renderHook(() => useToolTest('tool1'));
    await act(async () => { await result.current.submit('/api/enterprise/tools/tool1/test', { value: 1 }); });
    expect(result.current.queryError).toContain('offline');
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(result.current.status).toBe('completed');
    expect(mocks.post).toHaveBeenCalledTimes(1);
    expect(mocks.get).toHaveBeenCalledTimes(2);
  });

  it('does not enable re-submission for an unknown Provider outcome', async () => {
    mocks.post.mockResolvedValue({ success: true, data: { accepted: true, task_id: 'task1' } });
    mocks.get.mockResolvedValue({ id: 'task1', status: 'outcome_unknown' });
    const { result } = renderHook(() => useToolTest('tool1'));
    await act(async () => { await result.current.submit('/api/enterprise/tools/tool1/test', { value: 1 }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(result.current.status).toBe('outcome_unknown');
    expect(result.current.busy).toBe(false);
    // The lock stays held for tracking_blocked only; unknown outcome must not wedge the button.
    expect(mocks.post).toHaveBeenCalledTimes(1);
  });

  it('blocks rapid double clicks before state updates', async () => {
    let resolvePost: (value: unknown) => void = () => {};
    mocks.post.mockReturnValue(new Promise((resolve) => { resolvePost = resolve; }));
    const { result } = renderHook(() => useToolTest('tool1'));
    const first = result.current.submit('/api/enterprise/tools/tool1/test', { value: 1 });
    const second = result.current.submit('/api/enterprise/tools/tool1/test', { value: 1 });
    resolvePost({ success: true, data: { accepted: true, task_id: 'task1' } });
    await act(async () => { await Promise.all([first, second]); });
    expect(mocks.post).toHaveBeenCalledTimes(1);
  });

  it('exposes a failed submission status and rethrows for the caller', async () => {
    mocks.post.mockRejectedValue(new Error('boom'));
    const { result } = renderHook(() => useToolTest('tool1'));
    let caught: unknown;
    await act(async () => {
      try { await result.current.submit('/api/enterprise/tools/tool1/test', { value: 1 }); } catch (error) { caught = error; }
    });
    expect(caught).toBeInstanceOf(Error);
    expect(result.current.status).toBe('failed');
    expect(result.current.busy).toBe(false);
    // A later attempt is allowed again after failure.
    mocks.post.mockResolvedValue({ success: true, data: { accepted: true, task_id: 'task1' } });
    mocks.get.mockResolvedValue({ id: 'task1', status: 'completed' });
    await act(async () => { await result.current.submit('/api/enterprise/tools/tool1/test', { value: 2 }); });
    expect(mocks.post).toHaveBeenCalledTimes(2);
  });
});
