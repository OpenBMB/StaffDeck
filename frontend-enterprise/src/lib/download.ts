/**
 * Saving generated files from the web UI.
 *
 * The packaged macOS shell hosts the UI in a WKWebView, and this WebKit build never
 * honours `<a download>`: clicking such a link makes the *page itself* navigate to the
 * blob URL, so the whole interface is replaced by the file content. The shell also
 * cannot take over the download natively (no `WKDownloadDelegate` is exposed), so it
 * registers a script-message bridge and writes the bytes to disk itself after asking
 * the user where to put them.
 */

export type SaveBlobOutcome =
  | { status: 'saved'; path: string }
  | { status: 'cancelled' }
  | { status: 'browser' };

type NativeDownloadBridge = {
  postMessage: (payload: Record<string, unknown>) => void;
};

type NativeSaveReply = {
  id?: string;
  status?: string;
  path?: string;
  message?: string;
};

type PendingSave = {
  resolve: (reply: NativeSaveReply) => void;
  timer: ReturnType<typeof setTimeout>;
};

const NATIVE_HANDLER_NAME = 'staffdeckDownload';
/** Chunks must be a multiple of three bytes so each base64 payload decodes on its own. */
const NATIVE_CHUNK_BYTES = 768 * 1024;
const NATIVE_SAVE_TIMEOUT_MS = 10 * 60 * 1000;
/** Revoking later keeps the object URL readable for shells that copy the bytes out. */
const BROWSER_REVOKE_DELAY_MS = 60 * 1000;

const pendingSaves = new Map<string, PendingSave>();
let resultSinkInstalled = false;

function nativeDownloadBridge(): NativeDownloadBridge | null {
  const handlers = (
    window as unknown as {
      webkit?: { messageHandlers?: Record<string, { postMessage?: unknown } | undefined> };
    }
  ).webkit?.messageHandlers;
  const handler = handlers?.[NATIVE_HANDLER_NAME];
  if (!handler || typeof handler.postMessage !== 'function') return null;
  return handler as unknown as NativeDownloadBridge;
}

function installResultSink(): void {
  if (resultSinkInstalled) return;
  resultSinkInstalled = true;
  (window as unknown as { __staffdeckDownloadResult?: (reply: NativeSaveReply) => void })
    .__staffdeckDownloadResult = (reply) => {
      const id = typeof reply?.id === 'string' ? reply.id : '';
      const pending = id ? pendingSaves.get(id) : undefined;
      if (!pending) return;
      pendingSaves.delete(id);
      clearTimeout(pending.timer);
      pending.resolve(reply);
    };
}

function bytesToBase64(bytes: Uint8Array): string {
  const STRING_CHUNK = 0x8000;
  let binary = '';
  for (let index = 0; index < bytes.length; index += STRING_CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(index, index + STRING_CHUNK));
  }
  return btoa(binary);
}

function awaitNativeReply(id: string): Promise<NativeSaveReply> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      pendingSaves.delete(id);
      resolve({ id, status: 'error', message: '保存文件超时，请重试' });
    }, NATIVE_SAVE_TIMEOUT_MS);
    pendingSaves.set(id, { resolve, timer });
  });
}

function saveThroughBrowserDownload(blob: Blob, filename: string): SaveBlobOutcome {
  const objectUrl = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  link.rel = 'noopener';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => window.URL.revokeObjectURL?.(objectUrl), BROWSER_REVOKE_DELAY_MS);
  return { status: 'browser' };
}

async function saveThroughNativeBridge(
  bridge: NativeDownloadBridge,
  blob: Blob,
  filename: string,
): Promise<SaveBlobOutcome> {
  installResultSink();
  const id = `dl-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const reply = awaitNativeReply(id);

  bridge.postMessage({
    phase: 'begin',
    id,
    name: filename,
    mime: blob.type || '',
    size: bytes.byteLength,
  });
  for (let offset = 0; offset < bytes.byteLength; offset += NATIVE_CHUNK_BYTES) {
    bridge.postMessage({
      phase: 'chunk',
      id,
      data: bytesToBase64(bytes.subarray(offset, offset + NATIVE_CHUNK_BYTES)),
    });
    // Yield so large files do not freeze the interface while they are handed over.
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  bridge.postMessage({ phase: 'end', id });

  const result = await reply;
  if (result.status === 'saved') return { status: 'saved', path: result.path ?? '' };
  if (result.status === 'cancelled') return { status: 'cancelled' };
  throw new Error(result.message || '保存文件失败');
}

/**
 * Saves a blob to disk.
 *
 * In the desktop shell this opens the native save panel and only resolves once the file
 * has actually been written (`saved`) or the user dismissed the panel (`cancelled`).
 * In a browser it triggers a regular download and reports `browser`. Failures throw.
 */
export async function saveBlob(blob: Blob, filename: string): Promise<SaveBlobOutcome> {
  const bridge = nativeDownloadBridge();
  if (!bridge) return saveThroughBrowserDownload(blob, filename);
  return saveThroughNativeBridge(bridge, blob, filename);
}
