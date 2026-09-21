// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { saveBlob } from './download';

type BridgePayload = Record<string, unknown>;

const originalCreateObjectUrl = window.URL.createObjectURL;
const originalRevokeObjectUrl = window.URL.revokeObjectURL;

function setWebkit(value: unknown) {
  Object.defineProperty(window, 'webkit', { configurable: true, value });
}

function setCreateObjectUrl(value: (blob: Blob) => string) {
  Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value });
}

function setRevokeObjectUrl(value: (url: string) => void) {
  Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value });
}

function downloadResultSink() {
  return (
    window as unknown as { __staffdeckDownloadResult?: (value: BridgePayload) => void }
  ).__staffdeckDownloadResult;
}

type PostMessageMock = ReturnType<typeof vi.fn> & {
  mock: { calls: [BridgePayload][] };
};

/** jsdom 的 Blob 没有 arrayBuffer()，这里只补上下载逻辑真正用到的那部分。 */
function blobOf(bytes: Uint8Array, type = 'application/octet-stream'): Blob {
  return {
    type,
    arrayBuffer: async () =>
      bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer,
  } as unknown as Blob;
}

/** Registers a fake shell bridge; `reply` returns the result to push back (or null). */
function installNativeBridge(
  reply: (endPayload: BridgePayload) => BridgePayload | null,
): PostMessageMock {
  const postMessage = vi.fn((payload: BridgePayload) => {
    if (payload.phase !== 'end') return;
    const result = reply(payload);
    if (result) downloadResultSink()?.(result);
  }) as PostMessageMock;
  setWebkit({ messageHandlers: { staffdeckDownload: { postMessage } } });
  return postMessage;
}

function payloadsOf(postMessage: PostMessageMock, phase: string): BridgePayload[] {
  return postMessage.mock.calls
    .map((call) => call[0])
    .filter((payload) => payload.phase === phase);
}

async function waitForPhase(postMessage: PostMessageMock, phase: string): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (payloadsOf(postMessage, phase).length > 0) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`bridge never posted phase ${phase}`);
}

function reassembleChunks(postMessage: PostMessageMock): Uint8Array {
  const chunks = payloadsOf(postMessage, 'chunk').map((payload) => {
    const binary = atob(String(payload.data));
    return Uint8Array.from(binary, (char) => char.charCodeAt(0));
  });
  const merged = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged;
}

afterEach(() => {
  setWebkit(undefined);
  setCreateObjectUrl(originalCreateObjectUrl);
  setRevokeObjectUrl(originalRevokeObjectUrl);
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('saveBlob outside the desktop shell', () => {
  it('clicks a download link and keeps the object URL readable afterwards', async () => {
    vi.useFakeTimers();
    const revoke = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    setCreateObjectUrl(() => 'blob:artifact');
    setRevokeObjectUrl(revoke);

    await expect(saveBlob(blobOf(new Uint8Array([1, 2, 3]), 'text/plain'), 'report.txt')).resolves.toEqual({
      status: 'browser',
    });

    expect(click).toHaveBeenCalledTimes(1);
    // 立刻 revoke 会让 {@link saveBlob} 之外的读取方（桌面壳）拿不到内容。
    expect(revoke).not.toHaveBeenCalled();
    vi.advanceTimersByTime(60_000);
    expect(revoke).toHaveBeenCalledWith('blob:artifact');
  });
});

describe('saveBlob through the desktop shell bridge', () => {
  it('streams base64 chunks and resolves once the shell reports the file saved', async () => {
    const payload = new Uint8Array(768 * 1024 + 11);
    for (let index = 0; index < payload.length; index += 1) payload[index] = index % 251;
    const postMessage = installNativeBridge((endPayload) => ({
      id: endPayload.id,
      status: 'saved',
      path: '/Users/me/Downloads/报告.xlsx',
    }));

    const outcome = await saveBlob(blobOf(payload), '报告.xlsx');

    expect(outcome).toEqual({ status: 'saved', path: '/Users/me/Downloads/报告.xlsx' });
    expect(postMessage.mock.calls.map((call) => call[0].phase)).toEqual([
      'begin',
      'chunk',
      'chunk',
      'end',
    ]);
    const [started] = payloadsOf(postMessage, 'begin');
    expect(started).toMatchObject({ name: '报告.xlsx', size: payload.length });
    expect(reassembleChunks(postMessage)).toEqual(payload);
  });

  it('sends a single empty transfer for an empty file', async () => {
    const postMessage = installNativeBridge((endPayload) => ({
      id: endPayload.id,
      status: 'saved',
      path: '/tmp/empty.txt',
    }));

    await expect(saveBlob(blobOf(new Uint8Array([])), 'empty.txt')).resolves.toEqual({
      status: 'saved',
      path: '/tmp/empty.txt',
    });
    expect(postMessage.mock.calls.map((call) => call[0].phase)).toEqual(['begin', 'end']);
  });

  it('reports a dismissed save panel as cancelled', async () => {
    installNativeBridge((endPayload) => ({ id: endPayload.id, status: 'cancelled' }));

    await expect(saveBlob(blobOf(new Uint8Array([100])), 'x.bin')).resolves.toEqual({ status: 'cancelled' });
  });

  it('throws the shell message when the file cannot be written', async () => {
    installNativeBridge((endPayload) => ({
      id: endPayload.id,
      status: 'error',
      message: '写入文件失败：No space left on device',
    }));

    await expect(saveBlob(blobOf(new Uint8Array([100])), 'x.bin')).rejects.toThrow('写入文件失败');
  });

  it('ignores a reply that belongs to another transfer', async () => {
    const postMessage = installNativeBridge(() => null);
    const pending = saveBlob(blobOf(new Uint8Array([100])), 'x.bin');
    await waitForPhase(postMessage, 'end');
    const [endPayload] = payloadsOf(postMessage, 'end');

    downloadResultSink()?.({ id: 'someone-else', status: 'error', message: 'wrong transfer' });
    downloadResultSink()?.({ id: endPayload.id, status: 'saved', path: '/tmp/right' });

    await expect(pending).resolves.toEqual({ status: 'saved', path: '/tmp/right' });
  });
});
