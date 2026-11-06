import { describe, expect, it, vi } from 'vitest'
import {
  isRemoteFailure,
  RemoteStream,
  RemoteStreamCarrierError,
  type RemoteStreamOptions,
} from '@deepseek-ai/dsh-api-gateway/client'
import { RemoteError } from '@deepseek-ai/dsh-typert-protocol'
import type { RemoteResult } from '@deepseek-ai/dsh-typert-protocol'
import {
  createSessionControlStream,
  SessionEventStream,
  type SessionJournalChange,
  type SessionRemote,
} from '../src/client/index.ts'
import type { SessionRemotes } from '../src/client/sessions/remotes.ts'
import type {
  SessionAddress,
  SessionControlFrame,
  SessionEventEntry,
  SessionFollowFrame,
  SessionFollowRequest,
  SessionHistoryRecord,
  SessionPage,
  SessionPageRequest,
} from '../src/types.ts'

type SessionTransportRemote = Pick<SessionRemote, 'control' | 'follow' | 'page'>

const ADDRESS: SessionAddress = { kind: 'session', sessionId: 'session-1' as never }
const AVAILABLE_CONNECTION = {
  generation: {
    getSnapshot: () => ({ id: 1, host: { home: '/home/fixture' } }),
    subscribe: () => () => {},
  },
}

function entry(seq: number): SessionEventEntry {
  return { type: 'event', event: { type: 'turn/start', seq, time: seq, data: { turn: seq } } }
}

function chunks(seq0: number): SessionHistoryRecord {
  return {
    type: 'chunks',
    event: {
      type: 'chunkrow/text-chunks',
      seq: seq0,
      time: seq0,
      data: { turn: 1, step: 1, index: 0, texts: ['a', 'b', 'c'], dt: [1, 1] },
    },
  }
}

function page(records: readonly SessionHistoryRecord[], hasMore = false): SessionPage {
  return { records, hasMore }
}

function snapshot(
  cursor: number,
  records: readonly SessionHistoryRecord[],
  hasMore = false,
): SessionFollowFrame {
  return {
    type: 'snapshot',
    header: {
      version: 0,
      id: ADDRESS.kind === 'session' ? ADDRESS.sessionId : ADDRESS.childSessionId,
      createdAt: 0,
    },
    cursor,
    records,
    hasMore,
    projections: { asOfSeq: cursor, values: {} },
  }
}

function sessionClient(remote: SessionTransportRemote): SessionRemotes {
  return {
    session: remote as SessionRemote,
    $stream: <Item>(options: RemoteStreamOptions<Item>) => (
      new RemoteStream(AVAILABLE_CONNECTION, options)
    ),
    commands: { execute: () => Promise.reject(new Error('stream tests never run commands')) },
    subagents: {
      list: () => Promise.reject(new Error('stream tests never read the subagent catalog')),
      prompt: () => Promise.reject(new Error('stream tests never prompt a subagent')),
      interruptByParent: () => Promise.reject(new Error('stream tests never interrupt a subagent')),
    },
  }
}

interface FollowGeneration {
  readonly frames: readonly SessionFollowFrame[]
  readonly terminal?: Error
  readonly hold?: boolean
  readonly waitAfterFrames?: Promise<void>
}

class ScriptedSessionRemote implements SessionTransportRemote {
  readonly followRequests: SessionFollowRequest[] = []
  readonly pageRequests: SessionPageRequest[] = []
  readonly signals: AbortSignal[] = []

  constructor(
    private readonly generations: FollowGeneration[],
    private readonly pages: RemoteResult<SessionPage>[],
    private readonly controlFrames: readonly SessionControlFrame[] = [],
    private readonly holdControl = true,
  ) {}

  async *follow(request: SessionFollowRequest, signal = new AbortController().signal): AsyncIterable<SessionFollowFrame> {
    const generation = this.generations.shift()
    if (generation === undefined) throw new Error('no scripted Session generation')
    this.followRequests.push(request)
    this.signals.push(signal)
    for (const frame of generation.frames) yield frame
    await generation.waitAfterFrames
    if (generation.terminal !== undefined) throw generation.terminal
    if (generation.hold === true && !signal.aborted) {
      await new Promise<void>((resolve) => {
        signal.addEventListener('abort', () => { resolve() }, { once: true })
      })
    }
  }

  page(request: SessionPageRequest): Promise<RemoteResult<SessionPage>> {
    this.pageRequests.push(request)
    const result = this.pages.shift()
    if (result === undefined) throw new Error('no scripted Session page')
    return Promise.resolve(result)
  }

  async *control(signal = new AbortController().signal): AsyncIterable<SessionControlFrame> {
    for (const frame of this.controlFrames) yield frame
    if (this.holdControl && !signal.aborted) {
      await new Promise<void>((resolve) => {
        signal.addEventListener('abort', () => { resolve() }, { once: true })
      })
    }
  }
}

describe('Session Client stream adapters', () => {
  it('validates a packed logical range before publishing one compact Client entry', async () => {
    const row = chunks(1)
    const remote = new ScriptedSessionRemote(
      [{ frames: [snapshot(4, [entry(0), row, entry(4)]), entry(5)], hold: true }],
      [],
    )
    const changes: SessionJournalChange[] = []
    const stream = new SessionEventStream(sessionClient(remote), ADDRESS, {
      publish: (change) => { changes.push(change) },
      failed: vi.fn(),
    })

    await stream.open({})
    await vi.waitFor(() => { expect(changes).toHaveLength(2) })

    expect(changes[0]).toMatchObject({
      type: 'replace',
      entries: [
        entry(0),
        row,
        entry(4),
      ],
    })
    expect(changes[0]?.type === 'replace' ? changes[0].entries[1] : undefined).toBe(row)
    expect(changes[1]).toEqual({ type: 'append', entry: entry(5) })
    await stream.dispose()
  })

  it('rejects a packed record emitted by the live follow path', async () => {
    const failed = vi.fn()
    const remote = new ScriptedSessionRemote(
      [{ frames: [snapshot(-1, []), chunks(0) as SessionFollowFrame], hold: true }],
      [],
    )
    const stream = new SessionEventStream(sessionClient(remote), ADDRESS, {
      publish: vi.fn(),
      failed,
    })

    await stream.open({})
    await vi.waitFor(() => { expect(failed).toHaveBeenCalledOnce() })
    const violation: unknown = failed.mock.calls[0]?.[0]
    expect(isRemoteFailure(violation)).toBe(true)
    expect(violation).toMatchObject({
      code: 'gateway/internal',
      message: 'session live stream emitted a packed history record',
    })
    await stream.dispose()
  })

  it('binds an event journal to one address and publishes replace, append, and prepend changes', async () => {
    const remote = new ScriptedSessionRemote(
      [{
        frames: [
          snapshot(3, [entry(2), entry(3)], true),
          entry(3),
          entry(4),
        ],
        hold: true,
      }],
      [
        { ok: true, value: page([entry(0), entry(1)], false) },
      ],
    )
    const changes: SessionJournalChange[] = []
    const stream = new SessionEventStream(sessionClient(remote), ADDRESS, {
      publish: (change) => { changes.push(change) },
      failed: vi.fn(),
    })

    await stream.open({ maxMessages: 50 })
    await vi.waitFor(() => { expect(changes).toHaveLength(2) })
    await stream.prepend({ beforeSeq: 2, maxMessages: 50 })

    expect(remote.followRequests).toEqual([{ address: ADDRESS, maxMessages: 50 }])
    expect(remote.pageRequests).toEqual([
      { address: ADDRESS, throughSeq: 4, beforeSeq: 2, maxMessages: 50 },
    ])
    expect(changes).toMatchObject([
      { type: 'replace', entries: [entry(2), entry(3)], hasMore: true },
      { type: 'append', entry: entry(4) },
      { type: 'prepend', entries: [entry(0), entry(1)], hasMore: false },
    ])
    await stream.dispose()
    expect(remote.signals[0]?.aborted).toBe(true)
  })

  it('replaces the retained window from each reconnect snapshot', async () => {
    const lost = new RemoteStreamCarrierError('lost')
    const remote = new ScriptedSessionRemote(
      [
        {
          frames: [snapshot(1, [entry(0), entry(1)]), entry(2)],
          terminal: lost,
        },
        { frames: [snapshot(4, [entry(0), entry(1), entry(2), entry(3), entry(4)])], hold: true },
      ],
      [],
    )
    const changes: SessionJournalChange[] = []
    const carrierFailed = vi.fn()
    const stream = new SessionEventStream(sessionClient(remote), ADDRESS, {
      publish: (change) => { changes.push(change) },
      carrierFailed,
      failed: vi.fn(),
    })

    await stream.open({ maxMessages: 50 })
    await vi.waitFor(() => { expect(remote.followRequests).toHaveLength(2) })

    expect(remote.followRequests).toEqual([
      { address: ADDRESS, maxMessages: 50 },
      { address: ADDRESS, maxMessages: 50 },
    ])
    expect(remote.pageRequests).toEqual([])
    expect(changes.map(change => change.type)).toEqual(['replace', 'append', 'replace'])
    expect(carrierFailed).toHaveBeenCalledWith(lost)
    await stream.dispose()
  })

  it('repairs a resumed event stream without an optional message limit', async () => {
    const finish = Promise.withResolvers<undefined>()
    const remote = new ScriptedSessionRemote(
      [
        {
          frames: [snapshot(0, [entry(0)])],
          waitAfterFrames: finish.promise,
          terminal: new RemoteStreamCarrierError('lost'),
        },
        { frames: [snapshot(1, [entry(0), entry(1)])], hold: true },
      ],
      [],
    )
    const stream = new SessionEventStream(sessionClient(remote), ADDRESS, {
      publish: vi.fn(),
      failed: vi.fn(),
    })

    await stream.open({})
    finish.resolve(undefined)
    await vi.waitFor(() => { expect(remote.followRequests).toHaveLength(2) })
    expect(remote.followRequests).toEqual([{ address: ADDRESS }, { address: ADDRESS }])
    expect(remote.pageRequests).toEqual([])
    await stream.dispose()
  })

  it('repairs a live gap without adding an absent message limit', async () => {
    const remote = new ScriptedSessionRemote(
      [{ frames: [snapshot(0, [entry(0)]), entry(2)], hold: true }],
      [{ ok: true, value: page([entry(0), entry(1), entry(2)]) }],
    )
    const changes: SessionJournalChange[] = []
    const stream = new SessionEventStream(sessionClient(remote), ADDRESS, {
      publish: (change) => { changes.push(change) },
      failed: vi.fn(),
    })

    await stream.open({})
    await vi.waitFor(() => { expect(changes).toHaveLength(2) })
    expect(remote.pageRequests).toEqual([{ address: ADDRESS, throughSeq: 2 }])
    await stream.dispose()
  })

  it('turns a pagination failure into a typed stream failure', async () => {
    const failure = new RemoteError('session/not-found', 'missing', { sessionId: 'session-1' as never })
    const remote = new ScriptedSessionRemote(
      [{ frames: [snapshot(-1, [])], hold: true }],
      [{ ok: false, error: failure }],
    )
    const stream = new SessionEventStream(sessionClient(remote), ADDRESS, {
      publish: vi.fn(),
      failed: vi.fn(),
    })

    await stream.open({})
    await expect(stream.prepend({})).rejects.toMatchObject({ code: 'session/not-found' })
    await expect(stream.open({})).rejects.toThrow('already opened')
    expect(remote.signals[0]?.aborted).toBe(false)
    expect(remote.pageRequests).toEqual([{ address: ADDRESS, throughSeq: -1 }])
    await stream.dispose()
    expect(remote.signals[0]?.aborted).toBe(true)
  })

  it('maps the Host-wide control baseline and deltas into one snapshot stream', async () => {
    const baseline: SessionControlFrame = {
      type: 'baseline',
      value: { queues: {}, jobs: {}, projections: {} },
    }
    const update: SessionControlFrame = {
      type: 'queue', sessionId: 'session-1' as never, items: [],
    }
    const remote = new ScriptedSessionRemote([], [], [baseline, update])
    const accept = vi.fn<(frame: SessionControlFrame) => void>()
    const stream = createSessionControlStream(sessionClient(remote), {
      accept,
      failed: vi.fn(),
    })

    stream.start()
    stream.start()
    await vi.waitFor(() => { expect(accept).toHaveBeenCalledTimes(2) })
    expect(accept.mock.calls.map(([frame]) => frame)).toEqual([baseline, update])
    await stream.dispose()
    await stream.dispose()
  })

  it('classifies control streams that end before and after their opening baseline', async () => {
    const beforeFailed = vi.fn()
    const before = createSessionControlStream(
      sessionClient(new ScriptedSessionRemote([], [], [], false)),
      { accept: vi.fn(), failed: beforeFailed },
    )
    before.start()
    await vi.waitFor(() => { expect(beforeFailed).toHaveBeenCalledOnce() })
    expect(beforeFailed.mock.calls[0]?.[0]).toMatchObject({
      message: 'session control stream ended before its opening snapshot',
    })
    await before.dispose()

    const baseline: SessionControlFrame = {
      type: 'baseline',
      value: { queues: {}, jobs: {}, projections: {} },
    }
    const carrierFailed = vi.fn()
    const failed = vi.fn()
    const afterRemote = new ScriptedSessionRemote([], [], [baseline], false)
    const after = createSessionControlStream(sessionClient(afterRemote), {
      accept: vi.fn(),
      carrierFailed: (error) => {
        carrierFailed(error)
        void after.dispose()
      },
      failed,
    })
    after.start()
    await vi.waitFor(() => { expect(carrierFailed).toHaveBeenCalledOnce() })
    expect(carrierFailed.mock.calls[0]?.[0]).toMatchObject({
      message: 'session control stream ended without a terminal result',
    })
    expect(failed).not.toHaveBeenCalled()
    await after.dispose()
  })
})
