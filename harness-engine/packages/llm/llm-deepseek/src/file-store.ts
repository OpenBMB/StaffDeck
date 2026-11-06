/** DeepSeek Files API upload reuse, invalidation, and quota recovery. @module dsh-llm-deepseek/file-store */

import type { RequestImageAttachment } from '@deepseek-ai/dsh-attachment'
import { LlmError } from '@deepseek-ai/dsh-llm'
import { DeepSeekFilesClient, isFilesQuotaError } from './files-api.ts'
import type { DeepSeekFileId } from './file-id.ts'
import { deepSeekFileScope, DeepSeekUploadIndex } from './upload-index.ts'
import type { DeepSeekUploadRecord } from './upload-index.ts'

/** DeepSeek chat accepts at most 32 MiB per image even when it is referenced by file id. */
export const MAX_CHAT_IMAGE_BYTES = 32 * 1024 * 1024
const OWNED_FILE_PREFIX = 'dsh-'

/** Resolved file-store policy from the plugin configuration. */
export interface DeepSeekFilePolicy {
  expiresAfterSeconds: number
  refreshMarginSeconds: number
  quotaCleanupBatch: number
}

/** Connection facts needed by file operations. */
export interface DeepSeekFileConnection {
  baseURL: string
  apiKey: string
}

/** Result of one file-id resolution. */
export interface DeepSeekFileReference {
  record: DeepSeekUploadRecord
  uploaded: boolean
}

interface FileStoreOptions {
  index?: DeepSeekUploadIndex
  now?: () => number
  fetch?: typeof fetch
}

interface SharedUpload {
  controller: AbortController
  promise: Promise<DeepSeekFileReference>
  settled: boolean
  waiters: number
}

function abortReason(signal: AbortSignal): Error {
  const reason: unknown = signal.reason
  return reason instanceof Error
    ? reason
    : new Error('DeepSeek file upload cancelled with a non-Error reason.', { cause: reason })
}

function uploadFailure(error: unknown): Error {
  return error instanceof Error
    ? error
    : new Error('DeepSeek file upload failed with a non-Error reason.', { cause: error })
}

function waitForUpload(operation: SharedUpload, signal: AbortSignal | undefined): Promise<DeepSeekFileReference> {
  signal?.throwIfAborted()
  operation.waiters += 1
  let released = false
  const release = (cancelledReason?: Error): void => {
    if (released) return
    released = true
    operation.waiters -= 1
    if (cancelledReason !== undefined && operation.waiters === 0 && !operation.settled) {
      operation.controller.abort(cancelledReason)
    }
  }
  if (signal === undefined) {
    return operation.promise.finally(() => {
      release()
    })
  }
  return new Promise<DeepSeekFileReference>((resolve, reject) => {
    const abort = (): void => {
      const reason = abortReason(signal)
      release(reason)
      reject(reason)
    }
    signal.addEventListener('abort', abort, { once: true })
    void operation.promise.then((value) => {
      signal.removeEventListener('abort', abort)
      release()
      resolve(value)
    }, (error: unknown) => {
      signal.removeEventListener('abort', abort)
      release()
      reject(uploadFailure(error))
    })
  })
}

function extension(mediaType: RequestImageAttachment['mediaType']): 'png' | 'jpeg' | 'webp' | 'gif' {
  switch (mediaType) {
    case 'image/png': return 'png'
    case 'image/jpeg': return 'jpeg'
    case 'image/webp': return 'webp'
    case 'image/gif': return 'gif'
  }
}

function filename(version: RequestImageAttachment): string {
  const attachment = String(version.attachment.attachmentId).slice('sha256:'.length, 'sha256:'.length + 16)
  const variant = String(version.variantId).slice('sha256:'.length, 'sha256:'.length + 8)
  return `${OWNED_FILE_PREFIX}${attachment}-${variant}.${extension(version.mediaType)}`
}

/** User-scoped durable file-id reuse for the DeepSeek route. */
export class DeepSeekFileStore {
  private readonly index: DeepSeekUploadIndex
  private readonly now: () => number
  private readonly fetchImpl: typeof fetch | undefined
  private readonly inflight = new Map<string, SharedUpload>()

  /**
   * @param options - testable index, clock, and transport boundaries.
   */
  constructor(options: FileStoreOptions = {}) {
    this.index = options.index ?? new DeepSeekUploadIndex()
    this.now = options.now ?? Date.now
    this.fetchImpl = options.fetch
  }

  private client(connection: DeepSeekFileConnection): DeepSeekFilesClient {
    return new DeepSeekFilesClient({
      baseURL: connection.baseURL,
      apiKey: connection.apiKey,
      ...this.fetchImpl === undefined ? {} : { fetch: this.fetchImpl },
    })
  }

  /**
   * Resolve or upload one deterministic request image. Concurrent calls share one upload while retaining independent waits.
   * @param version - deterministic model-request bytes and complete transformation identity.
   * @param connection - endpoint and API-key snapshot.
   * @param policy - expiry and quota-recovery policy.
   * @param signal - cancellation of this wait; shared transport stops when no waiter remains.
   * @returns a reusable file id and whether this call published a new upload.
   */
  ensureUploaded(
    version: RequestImageAttachment,
    connection: DeepSeekFileConnection,
    policy: DeepSeekFilePolicy,
    signal?: AbortSignal,
  ): Promise<DeepSeekFileReference> {
    signal?.throwIfAborted()
    const scope = deepSeekFileScope(connection.baseURL, connection.apiKey)
    const key = `${scope}\0${version.variantId}`
    let active = this.inflight.get(key)
    if (active?.controller.signal.aborted) {
      this.inflight.delete(key)
      active = undefined
    }
    if (active !== undefined) return waitForUpload(active, signal)
    const controller = new AbortController()
    const shared: SharedUpload = {
      controller,
      settled: false,
      waiters: 0,
      promise: undefined as never,
    }
    shared.promise = this.ensureUploadedOnce(version, connection, policy, controller.signal).then((value) => {
      shared.settled = true
      return value
    }, (error: unknown) => {
      shared.settled = true
      throw uploadFailure(error)
    })
    this.inflight.set(key, shared)
    void shared.promise.finally(() => {
      if (this.inflight.get(key) === shared) this.inflight.delete(key)
    }).catch(() => {})
    return waitForUpload(shared, signal)
  }

  private async ensureUploadedOnce(
    version: RequestImageAttachment,
    connection: DeepSeekFileConnection,
    policy: DeepSeekFilePolicy,
    signal: AbortSignal,
  ): Promise<DeepSeekFileReference> {
    if (version.bytes > MAX_CHAT_IMAGE_BYTES) {
      throw new LlmError('DeepSeek chat image exceeds the 32 MiB per-image limit.', 'INVALID_REQUEST')
    }
    const scope = deepSeekFileScope(connection.baseURL, connection.apiKey)
    const now = this.now()
    const marginMs = policy.refreshMarginSeconds * 1_000
    const cached = await this.index.get(scope, version.variantId, now, marginMs)
    if (cached !== undefined) return { record: cached, uploaded: false }

    const client = this.client(connection)
    const upload = async (): Promise<DeepSeekUploadRecord> => {
      const remote = await client.upload({
        data: version.data,
        mediaType: version.mediaType,
        filename: filename(version),
        expiresAfterSeconds: policy.expiresAfterSeconds,
        signal,
      })
      if (remote.bytes !== version.data.byteLength) {
        throw new LlmError('DeepSeek Files API upload response does not match the submitted image.', 'INVALID_RESPONSE')
      }
      return {
        scope,
        attachmentId: version.attachment.attachmentId,
        variantId: version.variantId,
        fileId: remote.id,
        bytes: remote.bytes,
        createdAt: remote.createdAt * 1_000,
        expiresAt: remote.expiresAt * 1_000,
      }
    }

    let candidate: DeepSeekUploadRecord
    try {
      candidate = await upload()
    } catch (error: unknown) {
      if (!isFilesQuotaError(error)) throw error
      const deleted = await this.reclaimOldestOwned(connection, policy.quotaCleanupBatch, signal)
      if (deleted === 0) throw error
      candidate = await upload()
    }
    const committed = await this.index.commit(candidate, this.now(), marginMs)
    if (!committed.accepted) {
      try {
        await client.delete(candidate.fileId, signal)
      } catch {
        // The winning mapping is durable. A failed duplicate cleanup affects quota only and is retried by recovery.
      }
    }
    return { record: committed.record, uploaded: committed.accepted }
  }

  /**
   * Invalidate one exact local mapping after the chat endpoint rejects its remote id.
   * @param version - request-image version whose remote generation failed.
   * @param fileId - exact rejected file id.
   * @param connection - endpoint and API-key snapshot.
   */
  async invalidate(
    version: RequestImageAttachment,
    fileId: DeepSeekFileId,
    connection: DeepSeekFileConnection,
  ): Promise<void> {
    await this.index.remove(
      deepSeekFileScope(connection.baseURL, connection.apiKey),
      version.variantId,
      fileId,
    )
  }

  /**
   * Delete the indexed remote file for one attachment and remove its local mapping.
   * @param version - exact request-image version to release.
   * @param connection - endpoint and API-key snapshot.
   * @param policy - expiry policy used to locate a reusable mapping.
   * @param signal - request cancellation.
   * @returns whether an indexed file existed and was deleted.
   */
  async release(
    version: RequestImageAttachment,
    connection: DeepSeekFileConnection,
    policy: DeepSeekFilePolicy,
    signal?: AbortSignal,
  ): Promise<boolean> {
    const scope = deepSeekFileScope(connection.baseURL, connection.apiKey)
    const record = await this.index.get(
      scope,
      version.variantId,
      this.now(),
      policy.refreshMarginSeconds * 1_000,
    )
    if (record === undefined) return false
    await this.client(connection).delete(record.fileId, signal)
    await this.index.remove(scope, version.variantId, record.fileId)
    return true
  }

  /**
   * Delete the oldest provider files whose names identify harness ownership.
   * @param connection - endpoint and API-key snapshot.
   * @param count - positive maximum number of files to delete.
   * @param signal - request cancellation.
   * @returns number of successfully deleted files.
   */
  async reclaimOldestOwned(
    connection: DeepSeekFileConnection,
    count: number,
    signal?: AbortSignal,
  ): Promise<number> {
    const client = this.client(connection)
    let after: DeepSeekFileId | undefined
    const owned: DeepSeekFileId[] = []
    while (owned.length < count) {
      const page = await client.list({
        ...after === undefined ? {} : { after },
        limit: 1_000,
        order: 'asc',
        ...signal === undefined ? {} : { signal },
      })
      for (const file of page.data) {
        if (!file.filename.startsWith(OWNED_FILE_PREFIX)) continue
        owned.push(file.id)
        if (owned.length === count) break
      }
      if (!page.hasMore || page.lastId === undefined || page.lastId === after) break
      after = page.lastId
    }
    for (const fileId of owned) await client.delete(fileId, signal)
    return owned.length
  }

  /**
   * Delete every remote harness-owned file in the active API-key namespace and clear its index.
   * @param connection - endpoint and API-key snapshot.
   * @param signal - request cancellation.
   * @returns number of deleted files.
   */
  async releaseAll(connection: DeepSeekFileConnection, signal?: AbortSignal): Promise<number> {
    let total = 0
    for (;;) {
      const deleted = await this.reclaimOldestOwned(connection, 1_000, signal)
      total += deleted
      if (deleted < 1_000) break
    }
    await this.index.clear(deepSeekFileScope(connection.baseURL, connection.apiKey))
    return total
  }
}
