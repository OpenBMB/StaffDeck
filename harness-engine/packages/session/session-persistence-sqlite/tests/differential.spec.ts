import { afterEach, describe, expect, it } from 'vitest'
import fc from 'fast-check'
import { Context } from '@deepseek-ai/cordis'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { ToolCallId, type StreamChunk } from '@deepseek-ai/dsh-llm'
import SessionStore, { type SessionEvent } from '@deepseek-ai/dsh-session'
import type { SessionPersistence } from '@deepseek-ai/dsh-session-persistence'
import SessionPersistenceJsonl from '@deepseek-ai/dsh-session-persistence-jsonl'
import SessionPersistenceSqlite from '@deepseek-ai/dsh-session-persistence-sqlite'
import { meta } from '../../session-persistence/tests/contract.ts'
import { testSql } from './test-sql.ts'

type BackendName = 'jsonl-zstd' | 'sqlite'

interface MountedBackend {
  readonly persistence: SessionPersistence
  dispose(): Promise<void>
}

const directories: string[] = []
afterEach(async () => {
  for (const directory of directories.splice(0)) {
    await rm(directory, { recursive: true, force: true })
  }
})

async function freshDirectory(prefix: string): Promise<string> {
  const directory = await mkdtemp(join(tmpdir(), prefix))
  directories.push(directory)
  return directory
}

async function mount(name: BackendName, root: string): Promise<MountedBackend> {
  const ctx = new Context()
  await ctx.plugin(SessionStore)
  switch (name) {
    case 'jsonl-zstd': {
      const fiber = await ctx.plugin(SessionPersistenceJsonl, { root: join(root, 'jsonl') })
      return { persistence: ctx.sessionPersistence, dispose: async () => { await fiber.dispose() } }
    }
    case 'sqlite': {
      const fiber = await ctx.plugin(SessionPersistenceSqlite, { path: join(root, 'sessions.db') })
      return { persistence: ctx.sessionPersistence, dispose: async () => { await fiber.dispose() } }
    }
  }
}

function closedChunkLog(
  entries: readonly { readonly chunk: StreamChunk; readonly time: number; readonly ignorable?: true }[],
): SessionEvent[] {
  const chunks = entries.map(({ chunk, time, ignorable }, index): SessionEvent => ({
    type: 'assistant/chunk',
    seq: index + 2,
    time,
    data: { turn: 1, step: 1, chunk },
    ...ignorable === true ? { ignorable } : {},
  }))
  return [
    { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
    { type: 'step/start', seq: 1, time: 2, data: { turn: 1, step: 1 } },
    ...chunks,
    { type: 'step/end', seq: chunks.length + 2, time: 3, data: { turn: 1, step: 1 } },
    {
      type: 'turn/end',
      seq: chunks.length + 3,
      time: 4,
      data: { turn: 1, reason: { kind: 'completed' } },
    },
  ]
}

function packingMatrixLog(): SessionEvent[] {
  const entries: { chunk: StreamChunk; time: number; ignorable?: true }[] = [
    ...Array.from({ length: 5 }, (_, index) => ({
      chunk: { type: 'text-delta' as const, index: 0, text: `text-${index}` },
      time: 1_000 + index,
    })),
    ...Array.from({ length: 4 }, (_, index) => ({
      chunk: { type: 'reasoning-delta' as const, index: 1, text: `reason-${index}` },
      time: 990 - index,
    })),
    ...Array.from({ length: 4 }, (_, index) => ({
      chunk: {
        type: 'tool-call-delta' as const,
        index: 2,
        id: ToolCallId('named-call'),
        name: 'write',
        argumentsDelta: `{${index}`,
      },
      time: 2_000 + index,
    })),
    ...Array.from({ length: 3 }, (_, index) => ({
      chunk: {
        type: 'tool-call-delta' as const,
        index: 3,
        id: ToolCallId('unnamed-call'),
        argumentsDelta: `${index}}`,
      },
      time: 3_000 + index,
    })),
    { chunk: { type: 'block-start', index: 4, blockType: 'text' }, time: 4_000 },
    { chunk: { type: 'text-delta', index: 4, text: 'short-a' }, time: 4_001 },
    { chunk: { type: 'text-delta', index: 4, text: 'short-b' }, time: 4_002 },
    { chunk: { type: 'text-delta', index: 5, text: 'scalar-envelope' }, time: 4_003, ignorable: true },
    { chunk: { type: 'finish', reason: { kind: 'stop' } }, time: 4_004 },
  ]
  return closedChunkLog(entries)
}

function storageTagCollisionLog(): SessionEvent[] {
  return [
    { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
    ...['text-chunks', 'reasoning-chunks', 'tool-call-chunks'].map((type, index) => ({
      type,
      seq: index + 1,
      time: index + 2,
      data: { future: true },
      ignorable: true as const,
    }) as unknown as SessionEvent),
    { type: 'turn/end', seq: 4, time: 5, data: { turn: 1, reason: { kind: 'completed' } } },
  ]
}

function batches(events: readonly SessionEvent[], sizes: readonly number[]): SessionEvent[][] {
  const result: SessionEvent[][] = []
  let offset = 0
  let index = 0
  while (offset < events.length) {
    const size = sizes[index % sizes.length] as number
    result.push(events.slice(offset, offset + size))
    offset += size
    index += 1
  }
  return result
}

async function verifyBackend(
  name: BackendName,
  root: string,
  events: readonly SessionEvent[],
  sizes: readonly number[],
): Promise<void> {
  const header = { ...meta('differential', '/work'), delegationDepth: 0 }
  let mounted = await mount(name, root)
  try {
    await mounted.persistence.create(header)
    for (const batch of batches(events, sizes)) {
      await mounted.persistence.append(header.id, batch)
    }
    expect(await mounted.persistence.inspect(header.id), name).toEqual({ meta: header, events })
    expect(await mounted.persistence.list(), name).toEqual([header])
    const revision = (await mounted.persistence.listSnapshots())[0]?.revision
    for (let fromSeq = 0; fromSeq <= events.length + 1; fromSeq += 1) {
      expect((await mounted.persistence.readFrom(header.id, fromSeq)).events, `${name} seq ${fromSeq}`)
        .toEqual(events.slice(fromSeq))
    }
    expect((await mounted.persistence.listSnapshots())[0]?.revision, name).toBe(revision)
  } finally {
    await mounted.dispose()
  }

  mounted = await mount(name, root)
  try {
    expect(await mounted.persistence.inspect(header.id), `${name} reopen`).toEqual({ meta: header, events })
  } finally {
    await mounted.dispose()
  }
}

const streamChunkArbitrary: fc.Arbitrary<StreamChunk> = fc.oneof(
  fc.record({ type: fc.constant<'text-delta'>('text-delta'), index: fc.nat(2), text: fc.string() }),
  fc.record({ type: fc.constant<'reasoning-delta'>('reasoning-delta'), index: fc.nat(2), text: fc.string() }),
  fc.record({
    type: fc.constant<'tool-call-delta'>('tool-call-delta'),
    index: fc.nat(2),
    id: fc.constantFrom(ToolCallId('call-1'), ToolCallId('call-2')),
    argumentsDelta: fc.string(),
  }),
  fc.record({
    type: fc.constant<'tool-call-delta'>('tool-call-delta'),
    index: fc.nat(2),
    id: fc.constantFrom(ToolCallId('call-1'), ToolCallId('call-2')),
    name: fc.constantFrom('read', 'write'),
    argumentsDelta: fc.string(),
  }),
  fc.record({
    type: fc.constant<'block-start'>('block-start'),
    index: fc.nat(2),
    blockType: fc.constant<'text'>('text'),
  }),
  fc.record({ type: fc.constant<'finish'>('finish'), reason: fc.constant({ kind: 'stop' as const }) }),
)

const randomWorkload = fc.record({
  entries: fc.array(fc.record({
    chunk: streamChunkArbitrary,
    time: fc.oneof(
      { weight: 4, arbitrary: fc.integer({ min: 0, max: 10_000 }) },
      { weight: 1, arbitrary: fc.integer({ min: Number.MIN_SAFE_INTEGER, max: Number.MAX_SAFE_INTEGER }) },
    ),
    ignorable: fc.option(fc.constant<true>(true), { nil: undefined }),
  }), { maxLength: 30 }),
  batchSizes: fc.array(fc.integer({ min: 1, max: 8 }), { minLength: 1, maxLength: 8 }),
}).map(({ entries, batchSizes }) => ({
  events: JSON.parse(JSON.stringify(closedChunkLog(entries.map(({ chunk, time, ignorable }) => ({
    chunk,
    time,
    ...ignorable === true ? { ignorable } : {},
  }))))) as SessionEvent[],
  batchSizes,
}))

const randomizedDifferentialTimeoutMs = process.platform === 'win32' ? 120_000 : 60_000

describe('SQLite cross-backend differential behavior', () => {
  it('preserves ignorable logical events whose names match physical storage tags', async () => {
    const events = storageTagCollisionLog()
    const directory = await freshDirectory('dsh-sqlite-storage-tag-collision-')
    const root = join(directory, 'sqlite')
    await verifyBackend('sqlite', root, events, [2, 1])
    const db = new DatabaseSync(join(root, 'sessions.db'), { readOnly: true })
    try {
      expect(db.prepare(testSql('count-physical-types')).all()).toEqual([])
      expect(db.prepare(testSql('count-ignorable-events')).get()).toEqual({ count: 3 })
    } finally {
      db.close()
    }
  })

  it('matches JSONL/Zstandard for every packed kind, scalar fallback, suffix, partition, and reopen', async () => {
    const events = packingMatrixLog()
    for (const [partitionIndex, sizes] of [[events.length], [1], [2, 1, 5, 3]].entries()) {
      const directory = await freshDirectory(`dsh-sqlite-matrix-${partitionIndex}-`)
      for (const name of ['jsonl-zstd', 'sqlite'] as const) {
        const root = join(directory, name)
        await verifyBackend(name, root, events, sizes)
        if (name === 'sqlite') {
          const db = new DatabaseSync(join(root, 'sessions.db'), { readOnly: true })
          try {
            expect(db.prepare(testSql('count-physical-types')).all()).toEqual([
              [
                { type: 'reasoning-chunks', count: 1 },
                { type: 'text-chunks', count: 1 },
                { type: 'tool-call-chunks', count: 2 },
              ],
              [],
              [
                { type: 'reasoning-chunks', count: 1 },
                { type: 'text-chunks', count: 1 },
                { type: 'tool-call-chunks', count: 1 },
              ],
            ][partitionIndex])
            expect(db.prepare(testSql('count-ignorable-events')).get())
              .toEqual({ count: 1 })
          } finally {
            db.close()
          }
        }
      }
    }
  }, 30_000)

  it('matches JSONL/Zstandard across randomized logical logs and append partitions', async () => {
    await fc.assert(fc.asyncProperty(randomWorkload, async ({ events, batchSizes }) => {
      const directory = await freshDirectory('dsh-sqlite-property-')
      for (const name of ['jsonl-zstd', 'sqlite'] as const) {
        await verifyBackend(name, join(directory, name), events, batchSizes)
      }
    }), { numRuns: 100, seed: 0x5A17E })
  }, randomizedDifferentialTimeoutMs)

})
