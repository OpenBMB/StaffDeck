import { api, isAuthError } from './client';
import type { ModelConfigRead } from '../types';

export const MODEL_CATALOG_TIMEOUT_MS = 10000;

/** A cold chat entry must settle even if fetch/body/auth refresh never resolves. */
export async function loadModelCatalog(tenantId: string, signal: AbortSignal): Promise<ModelConfigRead[]> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cancel = () => {};
    try {
      const rows = await Promise.race([
        api.get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${encodeURIComponent(tenantId)}`, { signal: controller.signal }),
        new Promise<never>((_, reject) => {
          cancel = () => { controller.abort(); reject(new DOMException('Aborted', 'AbortError')); };
          signal.addEventListener('abort', cancel, { once: true });
          timer = setTimeout(() => {
            controller.abort();
            reject(new Error('模型配置加载超时，请重试'));
          }, MODEL_CATALOG_TIMEOUT_MS);
        }),
      ]);
      if (!Array.isArray(rows)) throw new Error('模型配置响应格式错误');
      return rows;
    } catch (error) {
      if (signal.aborted || isAuthError(error) || attempt === 1) throw error;
    } finally {
      clearTimeout(timer);
      signal.removeEventListener('abort', cancel);
    }
  }
  throw new Error('模型配置加载失败');
}
