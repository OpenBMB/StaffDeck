/** Session-scoped durable image URL cache shared by Conversation targets. */
import type { Context } from '@deepseek-ai/cordis'
import type { ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'
import type { ISessions } from '@deepseek-ai/dsh-api-session-controller/client'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import { bytesToBase64 } from '@deepseek-ai/dsh-util-crypto'

interface ImageUrlEntry {
  readonly sessionId: SessionId
  readonly generation: number
  current?: string
  pending: Promise<string>
}

/** Resolve durable Conversation images and release their browser URLs with Session scope. */
export class HistoricalImageCache {
  private readonly entries = new Map<string, ImageUrlEntry>()
  private readonly generations = new Map<SessionId, number>()
  private readonly scopeDisposers = new Map<SessionId, () => void>()
  private readonly urls = new Set<string>()
  private disposed = false

  /**
   * @param ctx - Owning ui-conversation fiber.
   * @param sessions - Session Controller object layer.
   */
  constructor(ctx: Context, private readonly sessions: ISessions) {
    ctx.effect(() => () => { this.dispose() }, 'ui-conversation historical image cache')
  }

  /**
   * Resolve and cache one session-authorized image URL.
   * @param sessionId - Session authorization and lifetime scope.
   * @param attachment - Durable image reference.
   * @returns browser URL valid until the Session binding is released.
   */
  resolve(sessionId: SessionId, attachment: ImageAttachmentRef): Promise<string> {
    if (this.disposed) return Promise.reject(new Error('ui-conversation image cache is disposed'))
    const key = this.key(sessionId, attachment)
    const cached = this.entries.get(key)
    if (cached !== undefined) return cached.pending
    const binding = this.sessions.binding(sessionId)
    if (binding === undefined) {
      return Promise.reject(new Error(`ui-conversation: unknown session "${sessionId}"`))
    }
    this.bindScope(sessionId, binding.ctx)
    const entry: ImageUrlEntry = {
      sessionId,
      generation: this.generations.get(sessionId) ?? 0,
      pending: Promise.resolve(''),
    }
    this.entries.set(key, entry)
    entry.pending = this.loadCanonical(key, entry, attachment)
    return entry.pending
  }

  /**
   * Return an already-displayable URL without starting a read.
   * @param sessionId - Session authorization and lifetime scope.
   * @param attachment - Durable image reference.
   * @returns current preview or canonical URL when cached.
   */
  peek(sessionId: SessionId, attachment: ImageAttachmentRef): string | undefined {
    return this.entries.get(this.key(sessionId, attachment))?.current
  }

  /**
   * Adopt a submission preview while fetching the durable admitted bytes.
   * The preview is available synchronously, then replaced and revoked when
   * the canonical attachment read completes.
   * @param sessionId - Session authorization and lifetime scope.
   * @param attachment - Durable image reference the URL temporarily displays.
   * @param url - browser URL to adopt.
   * @returns whether the cache took ownership.
   */
  seed(sessionId: SessionId, attachment: ImageAttachmentRef, url: string): boolean {
    if (this.disposed) return false
    const key = this.key(sessionId, attachment)
    if (this.entries.has(key)) return false
    const binding = this.sessions.binding(sessionId)
    if (binding === undefined) return false
    this.bindScope(sessionId, binding.ctx)
    const entry: ImageUrlEntry = {
      sessionId,
      generation: this.generations.get(sessionId) ?? 0,
      current: url,
      pending: Promise.resolve(url),
    }
    this.urls.add(url)
    this.entries.set(key, entry)
    entry.pending = this.loadCanonical(key, entry, attachment).catch((error: unknown) => {
      if (this.entries.get(key) === entry && entry.current === url) {
        this.entries.delete(key)
        this.releaseUrl(url)
      }
      throw error
    })
    // Seed begins the durable read before a transcript image necessarily
    // mounts. Keep that legitimate no-consumer path from becoming an
    // unhandled rejection; resolve() still returns the rejecting promise.
    void entry.pending.catch(() => {})
    return true
  }

  private key(sessionId: SessionId, attachment: ImageAttachmentRef): string {
    return `${sessionId}:${attachment.attachmentId}`
  }

  private loadCanonical(
    key: string,
    entry: ImageUrlEntry,
    attachment: ImageAttachmentRef,
  ): Promise<string> {
    const binding = this.sessions.binding(entry.sessionId)
    if (binding === undefined) return Promise.reject(new Error(`ui-conversation: unknown session "${entry.sessionId}"`))
    return binding.session.readAttachment(attachment.attachmentId)
      .then((result) => {
        if (!result.ok) throw new Error(`${result.error.code}: ${result.error.message}`)
        this.assertLive(key, entry)
        let url: string
        if (typeof URL.createObjectURL !== 'function') {
          url = `data:${result.value.attachment.mediaType};base64,${bytesToBase64(result.value.data)}`
        } else {
          const bytes = Uint8Array.from(result.value.data)
          url = URL.createObjectURL(new Blob([bytes.buffer], { type: result.value.attachment.mediaType }))
        }
        this.assertLive(key, entry)
        this.urls.add(url)
        const previous = entry.current
        entry.current = url
        if (previous !== undefined && previous !== url) this.releaseUrl(previous)
        return url
      })
      .catch((error: unknown) => {
        if (this.entries.get(key) === entry && entry.current === undefined) this.entries.delete(key)
        throw error
      })
  }

  private assertLive(key: string, entry: ImageUrlEntry): void {
    if (this.disposed) throw new Error('ui-conversation image cache was disposed before loading completed')
    if (this.entries.get(key) !== entry
      || (this.generations.get(entry.sessionId) ?? 0) !== entry.generation) {
      throw new Error('ui-conversation image scope was released before loading completed')
    }
  }

  private bindScope(sessionId: SessionId, scope: Context): void {
    if (this.scopeDisposers.has(sessionId)) return
    const dispose = scope.effect(() => () => {
      this.scopeDisposers.delete(sessionId)
      this.release(sessionId)
    }, 'ui-conversation historical image scope')
    this.scopeDisposers.set(sessionId, () => { void dispose() })
  }

  private release(sessionId: SessionId): void {
    this.generations.set(sessionId, (this.generations.get(sessionId) ?? 0) + 1)
    for (const [key, entry] of this.entries) {
      if (entry.sessionId !== sessionId) continue
      this.entries.delete(key)
      if (entry.current !== undefined) this.releaseUrl(entry.current)
    }
  }

  private releaseUrl(url: string): void {
    if (!this.urls.delete(url)) return
    revokeUrl(url)
  }

  private dispose(): void {
    if (this.disposed) return
    this.disposed = true
    for (const dispose of [...this.scopeDisposers.values()]) dispose()
    this.scopeDisposers.clear()
    for (const url of this.urls) revokeUrl(url)
    this.urls.clear()
    this.entries.clear()
  }
}

function revokeUrl(url: string): void {
  if (url.startsWith('blob:')) URL.revokeObjectURL(url)
}
