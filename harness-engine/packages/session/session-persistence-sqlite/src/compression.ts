/**
 * Fixed physical-record compression for SQLite. Schema-owned functions
 * encode logical events and decode tagged rows before persistence consumers
 * observe them.
 * @module @deepseek-ai/dsh-session-persistence-sqlite/compression
 */

import { readFileSync } from 'node:fs'
import { TextDecoder } from 'node:util'
import { constants, zstdCompressSync, zstdDecompressSync } from 'node:zlib'
import type { SessionEvent, SurfaceEventType } from '@deepseek-ai/dsh-session'
import {
  decodeSerializedChunkRow,
  type ChunkRow,
  MAX_PACKED_DATA_BYTES,
  type StorageRecord,
} from './codec.ts'
import type { EventRow } from './schema.ts'

/** One physical row ready for SQLite parameter binding. */
export interface BoundRecord {
  readonly seq: number
  readonly type: string
  readonly time: number
  readonly data: string | Uint8Array
  readonly sourceEventSeqs: Uint8Array | null
  readonly surfaceOp: string | null
  readonly ignorable: number | null
}

const UTF8_DECODER = new TextDecoder('utf-8', { fatal: true })
const ZSTD_COMPRESSION_LEVEL = 3
const DELTA_TAG = 0
const RUN_TAG = 1
const MAX_SAFE_INTEGER = BigInt(Number.MAX_SAFE_INTEGER)
const MAX_ZIGZAG_INTEGER = MAX_SAFE_INTEGER * 2n
const PACKED_ROW_SENTINEL = 0
/**
 * Schema-20 raw-content zstd dictionary for independently decodable data rows.
 * Its exact bytes are part of the physical format; changing the resource
 * requires a schema-version bump.
 */
const ZSTD_DICTIONARY = readFileSync(new URL('../resources/zstd-dictionary.bin', import.meta.url))

/** Compress options shared by every data-column frame. */
const DATA_ZSTD_OPTIONS = {
  dictionary: ZSTD_DICTIONARY,
  params: { [constants.ZSTD_c_compressionLevel]: ZSTD_COMPRESSION_LEVEL },
} as const
const CHUNK_TAGS = ['text-chunks', 'reasoning-chunks', 'tool-call-chunks'] as const
type ChunkTag = typeof CHUNK_TAGS[number]

function isChunkTag(value: string): value is ChunkTag {
  return (CHUNK_TAGS as readonly string[]).includes(value)
}

/**
 * Decode one physical SQLite row into its complete logical event span.
 * @param row - detached SQLite event row.
 * @returns every logical event represented by the row.
 */
export function decodeRow(row: EventRow): SessionEvent[] {
  if (row.ignorable !== PACKED_ROW_SENTINEL) return [decodeScalarRow(row)]
  if (!isChunkTag(row.type)) {
    throw new Error(`malformed ${row.type} storage row: packed discriminator requires a chunk tag`)
  }
  if (row.source_event_seqs !== null || row.surface_op !== null) {
    throw new Error(`malformed ${row.type} storage row: packed surface fields must be null`)
  }
  return decodeSerializedChunkRow(
    row.type,
    row.seq,
    row.time,
    decodeData(row.data, MAX_PACKED_DATA_BYTES),
  )
}

/**
 * Convert a storage record to SQLite column values.
 * @param record - scalar event or packed chunk record.
 * @returns column values for one physical insert.
 */
export function bindRecord(record: StorageRecord): BoundRecord {
  if (isChunkRow(record)) {
    return {
      seq: record.seq0,
      type: record.type,
      time: record.time0,
      data: encodeData(JSON.stringify(record.data)),
      sourceEventSeqs: null,
      surfaceOp: null,
      ignorable: PACKED_ROW_SENTINEL,
    }
  }
  const event = record
  const surface = event as SessionEvent<SurfaceEventType>
  return {
    seq: event.seq,
    type: event.type,
    time: event.time,
    data: encodeData(JSON.stringify(event.data)),
    sourceEventSeqs: surface.sourceEventSeqs === undefined
      ? null
      : encodeSourceEventSeqs(surface.sourceEventSeqs),
    surfaceOp: surface.surfaceOp === undefined ? null : JSON.stringify(surface.surfaceOp),
    ignorable: event.ignorable === true ? 1 : null,
  }
}

function encodeData(serialized: string): string | Uint8Array {
  const bytes = Buffer.from(serialized)
  const compressed = zstdCompressSync(bytes, DATA_ZSTD_OPTIONS)
  return compressed.length < bytes.length ? compressed : serialized
}

function decodeData(value: string | Uint8Array, maxOutputLength?: number): string {
  if (typeof value === 'string') return value
  const decoded = maxOutputLength === undefined
    ? zstdDecompressSync(value, { dictionary: ZSTD_DICTIONARY })
    : zstdDecompressSync(value, { dictionary: ZSTD_DICTIONARY, maxOutputLength })
  return UTF8_DECODER.decode(decoded)
}

function encodeSourceEventSeqs(values: readonly number[]): Uint8Array {
  if (values.length === 0) return new Uint8Array()
  const deltas = [DELTA_TAG]
  let previous = 0n
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index] as number
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new TypeError('sourceEventSeqs must contain non-negative safe integers')
    }
    const current = BigInt(value)
    const encoded = index === 0
      ? current
      : current >= previous
        ? (current - previous) * 2n
        : ((previous - current) * 2n) - 1n
    appendVarint(deltas, encoded)
    previous = current
  }
  if (!isStrictlyIncreasing(values)) return Uint8Array.from(deltas)

  const runs = [RUN_TAG]
  let start = values[0] as number
  let end = start
  for (let index = 1; index < values.length; index += 1) {
    const value = values[index] as number
    if (value === end + 1) {
      end = value
      continue
    }
    appendVarint(runs, BigInt(start))
    appendVarint(runs, BigInt(end - start + 1))
    start = value
    end = start
  }
  appendVarint(runs, BigInt(start))
  appendVarint(runs, BigInt(end - start + 1))
  return Uint8Array.from(runs.length < deltas.length ? runs : deltas)
}

function isStrictlyIncreasing(values: readonly number[]): boolean {
  return values.every((value, index) => index === 0 || value > (values[index - 1] as number))
}

function appendVarint(bytes: number[], value: bigint): void {
  let remaining = value
  while (remaining >= 0x80n) {
    bytes.push(Number(remaining & 0x7fn) | 0x80)
    remaining >>= 7n
  }
  bytes.push(Number(remaining))
}

function decodeSourceEventSeqs(bytes: Uint8Array, maxEntries: number): number[] {
  if (bytes.length === 0) return []
  if (bytes.length === 1) {
    throw new Error('malformed source_event_seqs storage value: truncated tagged payload')
  }
  switch (bytes[0]) {
    case DELTA_TAG: return decodeDeltaVarints(bytes, 1)
    case RUN_TAG: return decodeRunVarints(bytes, 1, maxEntries)
    default: throw new Error('malformed source_event_seqs storage value: unknown encoding tag')
  }
}

function decodeDeltaVarints(bytes: Uint8Array, offset: number): number[] {
  const values: number[] = []
  let previous = 0n
  while (offset < bytes.length) {
    const first = values.length === 0
    const decoded = readVarint(bytes, offset, first ? MAX_SAFE_INTEGER : MAX_ZIGZAG_INTEGER)
    offset = decoded.offset
    const delta = first
      ? decoded.value
      : (decoded.value & 1n) === 0n
        ? decoded.value / 2n
        : -((decoded.value + 1n) / 2n)
    const value = first ? delta : previous + delta
    if (value < 0n || value > MAX_SAFE_INTEGER) {
      throw new Error('malformed source_event_seqs storage value: decoded seq is out of range')
    }
    values.push(Number(value))
    previous = value
  }
  return values
}

function decodeRunVarints(bytes: Uint8Array, offset: number, maxEntries: number): number[] {
  const values: number[] = []
  let previousEnd = -1
  while (offset < bytes.length) {
    const start = readVarint(bytes, offset, MAX_SAFE_INTEGER)
    const count = readVarint(bytes, start.offset, MAX_SAFE_INTEGER)
    offset = count.offset
    const first = Number(start.value)
    const length = Number(count.value)
    if (length < 1) {
      throw new Error('malformed source_event_seqs storage value: run count must be positive')
    }
    if (first <= previousEnd || !Number.isSafeInteger(first + length - 1)) {
      throw new Error('malformed source_event_seqs storage value: runs must ascend within safe integers')
    }
    if (length > maxEntries - values.length) {
      throw new Error('malformed source_event_seqs storage value: run exceeds its event sequence')
    }
    for (let index = 0; index < length; index += 1) values.push(first + index)
    previousEnd = first + length - 1
  }
  return values
}

function readVarint(
  bytes: Uint8Array,
  offset: number,
  limit: bigint,
): { readonly value: bigint; readonly offset: number } {
  let value = 0n
  let shift = 0n
  while (offset < bytes.length) {
    const byte = bytes[offset] as number
    offset += 1
    value |= BigInt(byte & 0x7f) << shift
    if ((byte & 0x80) === 0) {
      if (shift > 0n && (byte & 0x7f) === 0) {
        throw new Error('malformed source_event_seqs storage value: non-canonical varint')
      }
      if (value > limit) {
        throw new Error('malformed source_event_seqs storage value: varint is out of range')
      }
      return { value, offset }
    }
    shift += 7n
    if (shift > 56n) {
      throw new Error('malformed source_event_seqs storage value: varint is out of range')
    }
  }
  throw new Error('malformed source_event_seqs storage value: truncated varint')
}

function isChunkRow(record: StorageRecord): record is ChunkRow {
  return isChunkTag(record.type) && 'seq0' in record && !('seq' in record)
}

function decodeScalarRow(row: EventRow): SessionEvent {
  const surfaceFields = {
    ...row.source_event_seqs === null
      ? {}
      : { sourceEventSeqs: decodeSourceEventSeqs(row.source_event_seqs, row.seq) },
    ...row.surface_op === null
      ? {}
      : { surfaceOp: JSON.parse(row.surface_op) as SessionEvent<SurfaceEventType>['surfaceOp'] },
  }
  return {
    type: row.type as SessionEvent['type'],
    seq: row.seq,
    time: row.time,
    data: JSON.parse(decodeData(row.data)) as SessionEvent['data'],
    ...surfaceFields,
    ...row.ignorable === 1 ? { ignorable: true as const } : {},
  } as SessionEvent
}

/**
 * Validate and flatten physical rows into their logical prefix. A malformed
 * row or logical gap is committed corruption when a later valid turn end
 * exists; otherwise it starts a removable physical tail.
 * @param rows - physical rows ordered by their first logical sequence.
 * @param base - logical sequence expected from the first selected row.
 * @returns the contiguous logical prefix and optional physical deletion base.
 */
export function scanRows(
  rows: readonly EventRow[],
  base = 0,
): { preserved: SessionEvent[]; tornFrom?: number } {
  let lastTurnEndRow = -1
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    try {
      if (decodeRow(rows[index] as EventRow).some(event => event.type === 'turn/end')) {
        lastTurnEndRow = index
        break
      }
    } catch {
      // A malformed row cannot prove that an earlier physical prefix committed.
    }
  }

  const preserved: SessionEvent[] = []
  let expected = base
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
    const physical = rows[rowIndex] as EventRow
    let logicalEvents: SessionEvent[] | undefined
    try {
      logicalEvents = decodeRow(physical)
    } catch {
      // The committed-prefix rule below owns whether this invalid row is fatal or repairable.
    }
    if (logicalEvents === undefined) {
      if (rowIndex <= lastTurnEndRow) {
        throw new Error(`corrupt session log: invalid committed physical row at seq ${physical.seq}`)
      }
      return { preserved, tornFrom: physical.seq }
    }
    let contiguous = true
    for (const event of logicalEvents) {
      if (event.seq !== expected) {
        contiguous = false
        break
      }
      expected += 1
    }
    if (!contiguous) {
      if (rowIndex <= lastTurnEndRow) {
        throw new Error(`corrupt session log: invalid committed physical row at seq ${physical.seq}`)
      }
      return { preserved, tornFrom: physical.seq }
    }
    preserved.push(...logicalEvents)
  }
  return { preserved }
}
