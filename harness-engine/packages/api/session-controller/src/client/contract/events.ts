/** Observable contiguous Session event window consumed by domain assemblers. */
import { notifySubscribers, type ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type { SessionEvent } from '@deepseek-ai/dsh-session/types'
import type { ChunkRowEvent } from '../../types.ts'

/** Standard Session event or compact historical Assistant run. */
export type SessionEventLike = SessionEvent | ChunkRowEvent

/** Client history entry retaining its coarse transport discriminator. */
export type SessionEventLikeEntry =
  | { readonly type: 'event'; readonly event: SessionEvent }
  | { readonly type: 'chunks'; readonly event: ChunkRowEvent }

/** Scalar live entry accepted by append-only Client paths. */
export type SessionLiveEventEntry = Extract<SessionEventLikeEntry, { readonly type: 'event' }>

interface EventWindowLeaf {
  readonly kind: 'leaf'
  readonly entries: readonly SessionEventLikeEntry[]
  readonly length: number
}

interface EventWindowConcat {
  readonly kind: 'concat'
  readonly left: EventWindowNode
  readonly right: EventWindowNode
  readonly length: number
}

type EventWindowNode = EventWindowLeaf | EventWindowConcat

function leaf(entries: readonly SessionEventLikeEntry[]): EventWindowLeaf {
  return { kind: 'leaf', entries, length: entries.length }
}

function concat(left: EventWindowNode, right: EventWindowNode): EventWindowConcat {
  return { kind: 'concat', left, right, length: left.length + right.length }
}

function materialize(node: EventWindowNode): readonly SessionEventLikeEntry[] {
  if (node.kind === 'leaf') return node.entries
  const entries = new Array<SessionEventLikeEntry>(node.length)
  const pending: EventWindowNode[] = [node]
  let index = 0
  while (pending.length > 0) {
    const current = pending.pop() as EventWindowNode
    if (current.kind === 'concat') {
      pending.push(current.right, current.left)
      continue
    }
    for (const entry of current.entries) {
      entries[index] = entry
      index += 1
    }
  }
  return entries
}

function windowSnapshot(
  node: EventWindowNode,
  hasMore: boolean,
  revision: number,
  change: SessionEventChange,
): SessionEventWindow {
  let entries: readonly SessionEventLikeEntry[] | undefined
  return {
    get entries() {
      entries ??= materialize(node)
      return entries
    },
    hasMore,
    revision,
    change,
  }
}

/** Exact delta that produced the latest event-window revision. */
export type SessionEventChange =
  | { readonly kind: 'replace'; readonly entries: readonly SessionEventLikeEntry[] }
  | { readonly kind: 'prepend'; readonly entries: readonly SessionEventLikeEntry[] }
  | { readonly kind: 'append'; readonly entries: readonly SessionLiveEventEntry[] }

/** Current contiguous event window and its latest synchronous delta. */
export interface SessionEventWindow {
  readonly entries: readonly SessionEventLikeEntry[]
  readonly hasMore: boolean
  readonly revision: number
  readonly change: SessionEventChange
}

/** Conversation-facing event source exposed by one Session binding. */
export type SessionEventSource = ObservableSnapshot<SessionEventWindow>

/** Session-owned event feed; every accepted window mutation publishes synchronously. */
export class MutableSessionEventSource implements SessionEventSource {
  private readonly listeners = new Set<() => void>()
  private window: EventWindowNode = leaf([])
  private snapshot: SessionEventWindow = windowSnapshot(
    this.window,
    false,
    0,
    { kind: 'replace', entries: [] },
  )

  /** @returns the cached event-window snapshot. */
  getSnapshot(): SessionEventWindow { return this.snapshot }

  /**
   * Subscribe to synchronous window publication.
   * @param listener - invalidation callback.
   * @returns unsubscribe function.
   */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  /**
   * Replace the complete contiguous window.
   * @param entries - complete window.
   * @param hasMore - whether older history remains.
   */
  replace(entries: readonly SessionEventLikeEntry[], hasMore: boolean): void {
    this.window = leaf(entries)
    this.publish(hasMore, { kind: 'replace', entries })
  }

  /**
   * Prepend one older contiguous page.
   * @param entries - newly loaded older entries.
   * @param hasMore - whether still older history remains.
   */
  prepend(entries: readonly SessionEventLikeEntry[], hasMore: boolean): void {
    this.window = concat(leaf(entries), this.window)
    this.publish(hasMore, { kind: 'prepend', entries })
  }

  /**
   * Append one contiguous live entry.
   * @param entry - live tail entry.
   */
  append(entry: SessionLiveEventEntry): void {
    const entries = [entry]
    this.window = concat(this.window, leaf(entries))
    this.publish(this.snapshot.hasMore, {
      kind: 'append',
      entries,
    })
  }

  private publish(
    hasMore: boolean,
    change: SessionEventChange,
  ): void {
    this.snapshot = windowSnapshot(this.window, hasMore, this.snapshot.revision + 1, change)
    notifySubscribers(this.listeners, '[session-controller] event feed')
  }
}
