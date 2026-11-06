import { describe, expect, it } from 'vitest'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { zstdCompressSync } from 'node:zlib'
import type { SessionEvent } from '@deepseek-ai/dsh-session'
import { ToolCallId, type StreamChunk } from '@deepseek-ai/dsh-llm'
import {
  decodeStorageRecord,
  MAX_PACKED_DATA_BYTES,
  MAX_PACKED_ROW_MEMBERS,
  packChunkRuns,
  type StorageRecord,
} from '../src/codec.ts'
import {
  bindRecord,
  decodeRow,
  scanRows,
} from '../src/compression.ts'
import type { EventRow } from '../src/schema.ts'

function chunk(seq: number, text = `token-${seq}`): SessionEvent {
  return {
    type: 'assistant/chunk',
    seq,
    time: 1_000 + seq,
    data: {
      turn: 1,
      step: 1,
      chunk: { type: 'text-delta', index: 0, text },
    },
  }
}

function event(seq: number, time: number, value: StreamChunk, turn = 1, step = 1): SessionEvent {
  return { type: 'assistant/chunk', seq, time, data: { turn, step, chunk: value } }
}

function row(record: StorageRecord): EventRow {
  const bound = bindRecord(record)
  return {
    seq: bound.seq,
    type: bound.type,
    time: bound.time,
    data: bound.data,
    source_event_seqs: bound.sourceEventSeqs,
    surface_op: bound.surfaceOp,
    ignorable: bound.ignorable,
  }
}

describe('SQLite compression', () => {
  it('pins the schema-20 dictionary bytes', () => {
    const dictionary = readFileSync(new URL('../resources/zstd-dictionary.bin', import.meta.url))
    expect(createHash('sha256').update(dictionary).digest('hex'))
      .toBe('dad18fa0247a8fdd886a62d8552eabd36cbd50c25af172873080d2f0ae770d17')
  })

  it('stores a 100-member run in one row and restores every logical event', () => {
    const events = Array.from({ length: 100 }, (_, index) => chunk(index))
    const records = packChunkRuns(events)
    expect(records).toHaveLength(1)
    expect(records[0]?.type).toBe('text-chunks')
    expect(scanRows(records.map(row)).preserved).toEqual(events)
  })

  it('partitions long and large runs within schema-owned row limits', () => {
    const long = Array.from({ length: MAX_PACKED_ROW_MEMBERS + 3 }, (_, index) => chunk(index))
    const longRecords = packChunkRuns(long)
    expect(longRecords).toHaveLength(2)
    expect(scanRows(longRecords.map(row)).preserved).toEqual(long)

    const large = Array.from({ length: 4 }, (_, index) => chunk(index, 'x'.repeat(300_000)))
    const largeRecords = packChunkRuns(large)
    expect(largeRecords).toHaveLength(2)
    for (const record of largeRecords) {
      if (record.type.endsWith('-chunks')) {
        expect(Buffer.byteLength(JSON.stringify(record.data))).toBeLessThanOrEqual(MAX_PACKED_DATA_BYTES)
      }
    }
    expect(scanRows(largeRecords.map(row)).preserved).toEqual(large)

    const individuallyLarge = Array.from({ length: 3 }, (_, index) => chunk(index, 'x'.repeat(400_000)))
    expect(packChunkRuns(individuallyLarge)).toEqual(individuallyLarge)

    const byteBound = Array.from({ length: 10 }, (_, index) => chunk(index, 'x'.repeat(150_000)))
    const byteBoundRecords = packChunkRuns(byteBound)
    expect(byteBoundRecords.length).toBeGreaterThan(1)
    expect(scanRows(byteBoundRecords.map(row)).preserved).toEqual(byteBound)
  })

  it('packs every owned kind and preserves optional tool-call names', () => {
    const events = [
      ...[0, 1, 2].map(seq => event(seq, seq, { type: 'reasoning-delta', index: 1, text: `${seq}` })),
      ...[3, 4, 5].map(seq => event(seq, seq, {
        type: 'tool-call-delta', index: 2, id: ToolCallId('named'), name: 'write', argumentsDelta: `${seq}`,
      })),
      ...[6, 7, 8].map(seq => event(seq, seq, {
        type: 'tool-call-delta', index: 3, id: ToolCallId('unnamed'), argumentsDelta: `${seq}`,
      })),
    ]
    const records = packChunkRuns(events)
    expect(records.map(record => record.type)).toEqual([
      'reasoning-chunks', 'tool-call-chunks', 'tool-call-chunks',
    ])
    expect(records.flatMap(decodeStorageRecord)).toEqual(events)
  })

  it('keeps every off-format delta scalar and splits incompatible runs', () => {
    const malformed = (seq: number, data: unknown): SessionEvent => ({
      type: 'assistant/chunk', seq, time: 10 + seq, data,
    } as SessionEvent)
    const values: SessionEvent[] = [
      { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
      { ...chunk(1), extra: true } as unknown as SessionEvent,
      { ...chunk(-1), seq: -1 },
      { ...chunk(3), time: 1.5 },
      malformed(4, 'data'),
      malformed(5, { turn: 1, step: 1, chunk: { type: 'text-delta', index: 0, text: 'x' }, extra: 1 }),
      malformed(6, { turn: '1', step: 1, chunk: { type: 'text-delta', index: 0, text: 'x' } }),
      malformed(7, { turn: 1, step: 1, chunk: 'chunk' }),
      malformed(8, { turn: 1, step: 1, chunk: { type: 'text-delta', index: '0', text: 'x' } }),
      malformed(9, { turn: 1, step: 1, chunk: { type: 'text-delta', index: 0, text: 1 } }),
      malformed(10, { turn: 1, step: 1, chunk: { type: 'tool-call-delta', index: 0, id: 1, argumentsDelta: 'x' } }),
      malformed(11, { turn: 1, step: 1, chunk: { type: 'tool-call-delta', index: 0, id: 'id', name: 1, argumentsDelta: 'x' } }),
      malformed(12, { turn: 1, step: 1, chunk: { type: 'usage', index: 0, totalTokens: 1 } }),
    ]
    expect(packChunkRuns(values)).toEqual(values)

    const gap = [chunk(0), chunk(1), chunk(3)]
    const step = [chunk(0), chunk(1), event(2, 2, { type: 'text-delta', index: 0, text: 'x' }, 1, 2)]
    const block = [chunk(0), chunk(1), event(2, 2, { type: 'text-delta', index: 1, text: 'x' })]
    const unsafeTime = [
      event(0, Number.MIN_SAFE_INTEGER, { type: 'text-delta', index: 0, text: 'a' }),
      event(1, Number.MAX_SAFE_INTEGER, { type: 'text-delta', index: 0, text: 'b' }),
      event(2, Number.MAX_SAFE_INTEGER, { type: 'text-delta', index: 0, text: 'c' }),
    ]
    const toolName = [0, 1, 2].map(seq => event(seq, seq, {
      type: 'tool-call-delta', index: 0, id: ToolCallId('id'),
      ...seq === 2 ? {} : { name: 'write' }, argumentsDelta: 'x',
    }))
    for (const events of [gap, step, block, unsafeTime, toolName]) {
      expect(packChunkRuns(events)).toEqual(events)
    }
  })

  it.each([
    ['extra envelope field', { type: 'text-chunks', seq0: 0, time0: 1, data: {}, extra: true }],
    ['negative sequence', { type: 'text-chunks', seq0: -1, time0: 1, data: {} }],
    ['fractional time', { type: 'text-chunks', seq0: 0, time0: 1.5, data: {} }],
    ['primitive data', { type: 'text-chunks', seq0: 0, time0: 1, data: 'bad' }],
    ['text fields', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [], args: [] } }],
    ['non-numeric placement', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: '1', step: 1, index: 0, dt: [0, 0], texts: ['a', 'b', 'c'] } }],
    ['non-array members', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0, 0], texts: 'abc' } }],
    ['too few members', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0], texts: ['a', 'b'] } }],
    ['too many members', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: Array(1_024).fill(0), texts: Array(1_025).fill('a') } }],
    ['non-string member', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0, 0], texts: ['a', 1, 'c'] } }],
    ['invalid gaps', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0, 0.5], texts: ['a', 'b', 'c'] } }],
    ['non-array gaps', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: '00', texts: ['a', 'b', 'c'] } }],
    ['gap arity', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0], texts: ['a', 'b', 'c'] } }],
    ['oversized data', { type: 'text-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0, 0], texts: ['x'.repeat(400_000), 'x'.repeat(400_000), 'x'.repeat(400_000)] } }],
    ['sequence overflow', { type: 'text-chunks', seq0: Number.MAX_SAFE_INTEGER, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0, 0], texts: ['a', 'b', 'c'] } }],
    ['time overflow', { type: 'text-chunks', seq0: 0, time0: Number.MAX_SAFE_INTEGER, data: { turn: 1, step: 1, index: 0, dt: [1, 0], texts: ['a', 'b', 'c'] } }],
    ['tool fields', { type: 'tool-call-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, dt: [0, 0], args: ['a', 'b', 'c'] } }],
    ['tool id', { type: 'tool-call-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, id: 1, dt: [0, 0], args: ['a', 'b', 'c'] } }],
    ['tool name', { type: 'tool-call-chunks', seq0: 0, time0: 1, data: { turn: 1, step: 1, index: 0, id: 'id', name: 1, dt: [0, 0], args: ['a', 'b', 'c'] } }],
  ])('rejects malformed packed data: %s', (_label, record) => {
    expect(() => decodeStorageRecord(record)).toThrow(/malformed .* storage row/)
  })

  it('decodes the schema-20 row vocabulary without another package codec', () => {
    const fixture: EventRow = {
      seq: 7,
      type: 'text-chunks',
      time: 90,
      data: JSON.stringify({ turn: 2, step: 3, index: 1, dt: [2, -1], texts: ['a', 'b', 'c'] }),
      source_event_seqs: null,
      surface_op: null,
      ignorable: 0,
    }
    expect(decodeRow(fixture)).toEqual([
      { ...chunk(7, 'a'), time: 90, data: { turn: 2, step: 3, chunk: { type: 'text-delta', index: 1, text: 'a' } } },
      { ...chunk(8, 'b'), time: 92, data: { turn: 2, step: 3, chunk: { type: 'text-delta', index: 1, text: 'b' } } },
      { ...chunk(9, 'c'), time: 91, data: { turn: 2, step: 3, chunk: { type: 'text-delta', index: 1, text: 'c' } } },
    ])
    expect(decodeStorageRecord('scalar')).toEqual(['scalar'])
    expect(decodeStorageRecord(chunk(0))).toEqual([chunk(0)])
  })

  it('rejects surface columns on packed rows', () => {
    const packed = row(packChunkRuns([chunk(0), chunk(1), chunk(2)])[0]!)
    const invalid: EventRow[] = [
      { ...packed, source_event_seqs: Buffer.alloc(0) },
      { ...packed, surface_op: '"append"' },
    ]
    for (const candidate of invalid) {
      expect(() => decodeRow(candidate)).toThrow(/surface fields must be null/)
    }
  })

  it('rejects the packed discriminator on a scalar event type', () => {
    const scalar = row({ type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } })
    expect(() => decodeRow({ ...scalar, ignorable: 0 }))
      .toThrow(/packed discriminator requires a chunk tag/)
  })

  it.each(['text-chunks', 'reasoning-chunks', 'tool-call-chunks'])(
    'preserves an ignorable logical event named %s as a scalar row',
    (type) => {
      const logical = {
        type,
        seq: 0,
        time: 1,
        data: { future: true },
        ignorable: true,
      } as unknown as SessionEvent
      const physical = row(logical)
      expect(physical.ignorable).toBe(1)
      expect(decodeRow(physical)).toEqual([logical])
    },
  )

  it('compresses small repetitive data with the shared dictionary', () => {
    const event = {
      type: 'tool/result',
      seq: 1,
      time: 2,
      data: { turn: 1, step: 1, message: { content: [{ type: 'text', text: 'hello world '.repeat(40) }] } },
      sourceEventSeqs: [0],
      surfaceOp: 'append',
    } as unknown as SessionEvent
    const bound = bindRecord(event)
    expect(bound.data).toBeInstanceOf(Uint8Array)
    expect(decodeRow(row(event))).toEqual([event])
  })

  it('compresses large data and run-encodes consecutive provenance arrays', () => {
    const sources = Array.from({ length: 2_000 }, (_, index) => index + 10)
    const event = {
      type: 'assistant/message',
      seq: sources.at(-1)! + 1,
      time: 1,
      data: { text: 'x'.repeat(8_192) },
      sourceEventSeqs: sources,
      surfaceOp: 'append',
    } as unknown as SessionEvent
    const bound = bindRecord(event)
    expect(bound.data).toBeInstanceOf(Uint8Array)
    expect(bound.sourceEventSeqs).toBeInstanceOf(Uint8Array)
    expect(bound.sourceEventSeqs?.[0]).toBe(1)
    expect(bound.sourceEventSeqs?.byteLength).toBeLessThan(Buffer.byteLength(JSON.stringify(sources)))
    expect(decodeRow(row(event))).toEqual([event])

    const small = bindRecord({ type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } })
    expect(typeof small.data).toBe('string')
  })

  it('round-trips empty, descending, and maximum-safe provenance deltas', () => {
    for (const sources of [
      [],
      [1, 3, 4, 5, 10],
      [Number.MAX_SAFE_INTEGER - 1, 0, Number.MAX_SAFE_INTEGER - 2],
    ]) {
      const event = {
        type: 'assistant/message',
        seq: Number.MAX_SAFE_INTEGER,
        time: 1,
        data: {},
        sourceEventSeqs: sources,
        surfaceOp: 'append',
      } as unknown as SessionEvent
      expect(decodeRow(row(event))).toEqual([event])
    }
  })

  it('does not impose a persistence-only provenance length limit', () => {
    const sources = Array.from({ length: 1_000_001 }, (_, index) => index)
    const event = {
      type: 'assistant/message',
      seq: sources.length,
      time: 1,
      data: {},
      sourceEventSeqs: sources,
      surfaceOp: 'append',
    } as unknown as SessionEvent
    expect(bindRecord(event).sourceEventSeqs?.[0]).toBe(1)
  })

  it.each([-1, 0.5])('rejects invalid provenance sequence %s before encoding', (sourceSeq) => {
    const event = {
      type: 'assistant/message',
      seq: 1,
      time: 1,
      data: {},
      sourceEventSeqs: [sourceSeq],
      surfaceOp: 'append',
    } as unknown as SessionEvent
    expect(() => bindRecord(event)).toThrow(/non-negative safe integers/)
  })

  it('rejects malformed compressed and delta-encoded values', () => {
    const scalar = row({ type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } })
    expect(() => decodeRow({ ...scalar, data: Buffer.from('not zstd') })).toThrow()
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x00]) }))
      .toThrow(/truncated tagged payload/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x01]) }))
      .toThrow(/truncated tagged payload/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x02, 0x00]) }))
      .toThrow(/unknown encoding tag/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x00, 0x80]) }))
      .toThrow(/truncated varint/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x00, 0x80, 0x00]) }))
      .toThrow(/non-canonical varint/)
    // tag 0, first value 0, then a negative delta (zigzag 0x01) from 0
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x00, 0x00, 0x01]) }))
      .toThrow(/decoded seq is out of range/)
    // tag 0, first value MAX_SAFE_INTEGER, then a positive delta overflowing it
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([
      0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x0f, 0x02,
    ]) })).toThrow(/decoded seq is out of range/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([
      0x00, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x10,
    ]) })).toThrow(/varint is out of range/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.concat([
      Buffer.from([0x00]), Buffer.alloc(9, 0x80),
    ]) }))
      .toThrow(/varint is out of range/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x01, 0x00, 0x01]) }))
      .toThrow(/run exceeds its event sequence/)
    expect(() => decodeRow({ ...scalar, source_event_seqs: Buffer.from([0x01, 0x00, 0x00]) }))
      .toThrow(/run count must be positive/)
    expect(() => decodeRow({ ...scalar, seq: 2, source_event_seqs: Buffer.from([0x01, 0x00, 0x01, 0x00, 0x01]) }))
      .toThrow(/runs must ascend/)
  })

  it('rejects an oversized packed data column before JSON decoding', () => {
    const oversized: EventRow = {
      seq: 0,
      type: 'text-chunks',
      time: 1,
      data: ' '.repeat(MAX_PACKED_DATA_BYTES + 1),
      source_event_seqs: null,
      surface_op: null,
      ignorable: 0,
    }
    expect(() => decodeRow(oversized)).toThrow(/data exceeds/)
  })

  it('bounds packed data while decompressing', () => {
    const serialized = JSON.stringify({
      turn: 1,
      step: 1,
      index: 0,
      dt: [0, 0],
      texts: ['x'.repeat(MAX_PACKED_DATA_BYTES), 'b', 'c'],
    })
    const oversized: EventRow = {
      seq: 0,
      type: 'text-chunks',
      time: 1,
      data: zstdCompressSync(serialized),
      source_event_seqs: null,
      surface_op: null,
      ignorable: 0,
    }
    expect(() => decodeRow(oversized)).toThrow(/Buffer larger than/)
  })

  it('distinguishes removable and committed physical corruption', () => {
    const start = row({ type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } })
    const skipped = row({ type: 'step/start', seq: 2, time: 2, data: { turn: 1, step: 1 } })
    expect(scanRows([start, skipped])).toEqual({ preserved: [
      { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
    ], tornFrom: 2 })

    const end = row({
      type: 'turn/end',
      seq: 3,
      time: 3,
      data: { turn: 1, reason: { kind: 'completed' } },
    })
    expect(() => scanRows([start, skipped, end])).toThrow(/invalid committed physical row at seq 2/)

    const malformed = {
      ...row(packChunkRuns([chunk(0), chunk(1), chunk(2)])[0]!),
      data: '{not json',
    }
    const committedEnd = row({
      type: 'turn/end',
      seq: 1,
      time: 4,
      data: { turn: 1, reason: { kind: 'completed' } },
    })
    expect(() => scanRows([malformed, committedEnd]))
      .toThrow(/invalid committed physical row at seq 0/)
  })

  it('treats a malformed packed tail as one removable physical row', () => {
    const malformed: EventRow = {
      seq: 0,
      type: 'text-chunks',
      time: 1,
      data: JSON.stringify({ turn: 1, step: 1, index: 0, dt: [], texts: ['a', 'b'] }),
      source_event_seqs: null,
      surface_op: null,
      ignorable: 0,
    }
    expect(scanRows([malformed])).toEqual({ preserved: [], tornFrom: 0 })
  })
})
