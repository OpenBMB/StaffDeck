import { describe, expect, it, vi } from 'vitest'
import {
  MutableSessionEventSource, type SessionLiveEventEntry,
} from '../src/client/contract/events.ts'

function entry(seq: number): SessionLiveEventEntry {
  return {
    type: 'event',
    event: {
      type: 'turn/start',
      seq,
      time: seq,
      data: { turn: seq },
    },
  }
}

describe('Client Session contracts', () => {
  it('publishes exact replace, prepend, and append event-window changes', () => {
    const feed = new MutableSessionEventSource()
    const listener = vi.fn()
    const dispose = feed.subscribe(listener)
    const first = entry(1)
    const older = entry(0)
    const live = entry(2)

    feed.replace([first], true)
    expect(feed.getSnapshot()).toEqual({
      entries: [first],
      hasMore: true,
      revision: 1,
      change: { kind: 'replace', entries: [first] },
    })

    feed.prepend([older], false)
    expect(feed.getSnapshot()).toEqual({
      entries: [older, first],
      hasMore: false,
      revision: 2,
      change: { kind: 'prepend', entries: [older] },
    })

    feed.append(live)
    expect(feed.getSnapshot()).toEqual({
      entries: [older, first, live],
      hasMore: false,
      revision: 3,
      change: { kind: 'append', entries: [live] },
    })
    expect(listener).toHaveBeenCalledTimes(3)

    dispose()
    feed.append(entry(3))
    expect(listener).toHaveBeenCalledTimes(3)
  })

  it('does not traverse the complete event window while appending', () => {
    const feed = new MutableSessionEventSource()
    const first = entry(1)
    const base = [first]
    const iterate = vi.fn(Array.prototype[Symbol.iterator].bind(base))
    Object.defineProperty(base, Symbol.iterator, { value: iterate })
    feed.replace(base, false)
    iterate.mockClear()

    const before = feed.getSnapshot()
    const live = entry(2)
    feed.append(live)
    const after = feed.getSnapshot()

    expect(iterate).not.toHaveBeenCalled()
    expect(before.entries).toEqual([first])
    expect(after.entries).toEqual([first, live])
    expect(after.entries).toBe(after.entries)
    expect(iterate).toHaveBeenCalledOnce()
  })

})
