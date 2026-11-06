import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from './client';
import { loadModelCatalog, MODEL_CATALOG_TIMEOUT_MS } from './model-catalog';

describe('bounded chat model catalog', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it('loads directly without any management-page event', async () => {
    vi.spyOn(api, 'get').mockResolvedValue([]);
    expect(await loadModelCatalog('tenant', new AbortController().signal)).toEqual([]);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('recovers from a request that never settles', async () => {
    const get = vi.spyOn(api, 'get').mockImplementationOnce(() => new Promise(() => {})).mockResolvedValueOnce([]);
    const result = loadModelCatalog('tenant', new AbortController().signal);
    await vi.advanceTimersByTimeAsync(MODEL_CATALOG_TIMEOUT_MS);
    expect(await result).toEqual([]);
    expect(get).toHaveBeenCalledTimes(2);
    expect(get.mock.calls[0][1]?.signal?.aborted).toBe(true);
  });

  it('settles as an error instead of loading forever when both attempts hang', async () => {
    vi.spyOn(api, 'get').mockImplementation(() => new Promise(() => {}));
    const result = loadModelCatalog('tenant', new AbortController().signal).catch((error: Error) => error);
    await vi.advanceTimersByTimeAsync(2 * MODEL_CATALOG_TIMEOUT_MS);
    expect((await result as Error).message).toBe('模型配置加载超时，请重试');
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('does not retry after unmount/abort, even if the transport ignores cancellation', async () => {
    vi.spyOn(api, 'get').mockImplementation(() => new Promise(() => {}));
    const controller = new AbortController();
    const result = loadModelCatalog('tenant', controller.signal).catch((error: Error) => error);
    controller.abort();
    expect((await result as Error).name).toBe('AbortError');
    await vi.advanceTimersByTimeAsync(30000);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('preserves auth failure for the login flow without retrying', async () => {
    vi.spyOn(api, 'get').mockRejectedValue(new ApiError(401, '', 'Unauthorized'));
    await expect(loadModelCatalog('tenant', new AbortController().signal)).rejects.toMatchObject({ status: 401 });
    expect(api.get).toHaveBeenCalledTimes(1);
  });
});
