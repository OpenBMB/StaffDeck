/**
 * Schema-20 physical chunk-row codec. This package owns the durable tags,
 * validation, and row-size limits independently from other persistence formats.
 * @module @deepseek-ai/dsh-session-persistence-sqlite/codec
 */

import type { StreamChunk } from '@deepseek-ai/dsh-llm'
import type { SessionEvent } from '@deepseek-ai/dsh-session'

/* jscpd:ignore-start -- schema 20 deliberately owns a frozen physical codec;
 * importing or sharing the JSONL codec would let that format mutate this database interpreter. */
type DeltaKind = 'text-delta' | 'reasoning-delta' | 'tool-call-delta'
type DeltaEvent = SessionEvent<'assistant/chunk'>

interface RunDataBase {
  readonly turn: number
  readonly step: number
  readonly index: number
  readonly dt: number[]
}

interface TextRunData extends RunDataBase {
  readonly texts: string[]
}

interface ToolCallRunData extends RunDataBase {
  readonly id: Extract<StreamChunk, { type: 'tool-call-delta' }>['id']
  readonly name?: string
  readonly args: string[]
}

/** One schema-20 packed physical record. */
export type ChunkRow =
  | { readonly type: 'text-chunks'; readonly seq0: number; readonly time0: number; readonly data: TextRunData }
  | { readonly type: 'reasoning-chunks'; readonly seq0: number; readonly time0: number; readonly data: TextRunData }
  | { readonly type: 'tool-call-chunks'; readonly seq0: number; readonly time0: number; readonly data: ToolCallRunData }

/** One scalar event or schema-20 packed physical record. */
export type StorageRecord = SessionEvent | ChunkRow

/** Minimum eligible members in a packed physical record. */
export const MIN_PACKED_ROW_MEMBERS = 3
/** Maximum logical members represented by one packed physical record. */
export const MAX_PACKED_ROW_MEMBERS = 1_024
/** Maximum UTF-8 bytes in one packed physical record's data column. */
export const MAX_PACKED_DATA_BYTES = 1_048_576

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function hasExactKeys(value: object, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
}

function classify(event: SessionEvent): DeltaKind | undefined {
  if (event.type !== 'assistant/chunk') return undefined
  if (!hasExactKeys(event, ['type', 'seq', 'time', 'data'])) return undefined
  if (!Number.isSafeInteger(event.seq) || event.seq < 0 || !Number.isSafeInteger(event.time)) return undefined
  const data: unknown = event.data
  if (!isRecord(data) || !hasExactKeys(data, ['turn', 'step', 'chunk'])) return undefined
  if (typeof data.turn !== 'number' || typeof data.step !== 'number') return undefined
  const chunk = data.chunk
  if (!isRecord(chunk) || typeof chunk.index !== 'number') return undefined
  switch (chunk.type) {
    case 'text-delta':
    case 'reasoning-delta':
      return hasExactKeys(chunk, ['type', 'index', 'text']) && typeof chunk.text === 'string'
        ? chunk.type
        : undefined
    case 'tool-call-delta': {
      const validKeys = hasExactKeys(chunk, ['type', 'index', 'id', 'argumentsDelta'])
        || (hasExactKeys(chunk, ['type', 'index', 'id', 'name', 'argumentsDelta'])
          && typeof chunk.name === 'string')
      return validKeys && typeof chunk.id === 'string' && typeof chunk.argumentsDelta === 'string'
        ? chunk.type
        : undefined
    }
    default:
      return undefined
  }
}

function toolCallOf(event: DeltaEvent): { readonly id: string; readonly name?: string } {
  return event.data.chunk as { readonly id: string; readonly name?: string }
}

function indexOf(event: DeltaEvent): number {
  return (event.data.chunk as { readonly index: number }).index
}

function continues(previous: DeltaEvent, next: DeltaEvent, kind: DeltaKind): boolean {
  if (next.seq !== previous.seq + 1 || !Number.isSafeInteger(next.time - previous.time)) return false
  if (next.data.turn !== previous.data.turn || next.data.step !== previous.data.step) return false
  if (indexOf(next) !== indexOf(previous)) return false
  if (kind !== 'tool-call-delta') return true
  const left = toolCallOf(previous)
  const right = toolCallOf(next)
  return left.id === right.id
    && Object.hasOwn(left, 'name') === Object.hasOwn(right, 'name')
    && left.name === right.name
}

function buildRow(kind: DeltaKind, run: readonly DeltaEvent[]): ChunkRow {
  const first = run[0] as DeltaEvent
  const base = {
    turn: first.data.turn,
    step: first.data.step,
    index: indexOf(first),
    dt: run.slice(1).map((event, index) => event.time - (run[index] as DeltaEvent).time),
  }
  const envelope = { seq0: first.seq, time0: first.time }
  if (kind === 'tool-call-delta') {
    const call = toolCallOf(first)
    return {
      type: 'tool-call-chunks',
      ...envelope,
      data: {
        ...base,
        id: call.id as Extract<StreamChunk, { type: 'tool-call-delta' }>['id'],
        ...Object.hasOwn(call, 'name') ? { name: call.name as string } : {},
        args: run.map(event => (event.data.chunk as { readonly argumentsDelta: string }).argumentsDelta),
      },
    }
  }
  const data = {
    ...base,
    texts: run.map(event => (event.data.chunk as { readonly text: string }).text),
  }
  return kind === 'text-delta'
    ? { type: 'text-chunks', ...envelope, data }
    : { type: 'reasoning-chunks', ...envelope, data }
}

function packedDataBytes(row: ChunkRow): number {
  return Buffer.byteLength(JSON.stringify(row.data))
}

function emitBoundedRun(out: StorageRecord[], kind: DeltaKind, completeRun: readonly DeltaEvent[]): void {
  let offset = 0
  while (completeRun.length - offset >= MIN_PACKED_ROW_MEMBERS) {
    let low = MIN_PACKED_ROW_MEMBERS
    let high = Math.min(completeRun.length - offset, MAX_PACKED_ROW_MEMBERS)
    const largest = buildRow(kind, completeRun.slice(offset, offset + high))
    if (packedDataBytes(largest) <= MAX_PACKED_DATA_BYTES) {
      out.push(largest)
      offset += high
      continue
    }
    high -= 1
    let accepted = 0
    let acceptedRow: ChunkRow | undefined
    while (low <= high) {
      const middle = Math.floor((low + high) / 2)
      const candidate = buildRow(kind, completeRun.slice(offset, offset + middle))
      if (packedDataBytes(candidate) <= MAX_PACKED_DATA_BYTES) {
        accepted = middle
        acceptedRow = candidate
        low = middle + 1
      } else {
        high = middle - 1
      }
    }
    if (accepted === 0) {
      out.push(completeRun[offset] as DeltaEvent)
      offset += 1
      continue
    }
    /* v8 ignore next -- accepted is set only with its same-branch candidate. */
    out.push(acceptedRow ?? malformed(kind, 'bounded encoder lost its accepted row'))
    offset += accepted
  }
  out.push(...completeRun.slice(offset))
}

/**
 * Pack eligible logical chunk runs into bounded schema-20 records.
 * @param events - logical events in sequence order.
 * @returns scalar and packed physical records in equivalent order.
 */
export function packChunkRuns(events: readonly SessionEvent[]): StorageRecord[] {
  const out: StorageRecord[] = []
  let kind: DeltaKind | undefined
  let run: DeltaEvent[] = []
  const flush = (): void => {
    if (kind === undefined) out.push(...run)
    else emitBoundedRun(out, kind, run)
    kind = undefined
    run = []
  }
  for (const event of events) {
    const nextKind = classify(event)
    if (nextKind === undefined) {
      flush()
      out.push(event)
      continue
    }
    const delta = event as DeltaEvent
    const previous = run.at(-1)
    if (nextKind === kind && previous !== undefined && continues(previous, delta, nextKind)) {
      run.push(delta)
      continue
    }
    flush()
    kind = nextKind
    run = [delta]
  }
  flush()
  return out
}

function malformed(tag: string, reason: string): never {
  throw new Error(`malformed ${tag} storage row: ${reason}`)
}

function validateRunData(
  tag: string,
  data: Record<string, unknown>,
  payloadKey: 'texts' | 'args',
  serializedBytes?: number,
): string[] {
  if (typeof data.turn !== 'number' || typeof data.step !== 'number' || typeof data.index !== 'number') {
    malformed(tag, 'turn/step/index must be numbers')
  }
  const payload = data[payloadKey]
  if (!Array.isArray(payload)
    || payload.length < MIN_PACKED_ROW_MEMBERS
    || payload.length > MAX_PACKED_ROW_MEMBERS
    || payload.some(member => typeof member !== 'string')) {
    malformed(tag, `${payloadKey} must contain ${MIN_PACKED_ROW_MEMBERS}..${MAX_PACKED_ROW_MEMBERS} strings`)
  }
  const gaps = data.dt
  if (!Array.isArray(gaps) || gaps.some(gap => !Number.isSafeInteger(gap))) {
    malformed(tag, 'dt must be an array of safe integers')
  }
  if (gaps.length !== payload.length - 1) malformed(tag, 'dt length must match the member count')
  if ((serializedBytes ?? Buffer.byteLength(JSON.stringify(data))) > MAX_PACKED_DATA_BYTES) {
    malformed(tag, `data exceeds ${MAX_PACKED_DATA_BYTES} UTF-8 bytes`)
  }
  return payload as string[]
}

function validateRow(
  value: Record<string, unknown>,
  tag: ChunkRow['type'],
  serializedBytes?: number,
): ChunkRow {
  if (!hasExactKeys(value, ['type', 'seq0', 'time0', 'data'])) malformed(tag, 'invalid envelope fields')
  if (!Number.isSafeInteger(value.seq0) || (value.seq0 as number) < 0) malformed(tag, 'seq0 must be non-negative')
  if (!Number.isSafeInteger(value.time0)) malformed(tag, 'time0 must be a safe integer')
  const data = value.data
  if (!isRecord(data)) malformed(tag, 'data must be an object')
  let payload: string[]
  if (tag === 'tool-call-chunks') {
    const withName = hasExactKeys(data, ['turn', 'step', 'index', 'id', 'name', 'dt', 'args'])
    if (!withName && !hasExactKeys(data, ['turn', 'step', 'index', 'id', 'dt', 'args'])) {
      malformed(tag, 'invalid tool-call data fields')
    }
    if (typeof data.id !== 'string' || (withName && typeof data.name !== 'string')) {
      malformed(tag, 'id and optional name must be strings')
    }
    payload = validateRunData(tag, data, 'args', serializedBytes)
  } else {
    if (!hasExactKeys(data, ['turn', 'step', 'index', 'dt', 'texts'])) malformed(tag, 'invalid text data fields')
    payload = validateRunData(tag, data, 'texts', serializedBytes)
  }
  if (!Number.isSafeInteger((value.seq0 as number) + payload.length - 1)) malformed(tag, 'member seqs exceed safe integers')
  let time = value.time0 as number
  for (const gap of data.dt as number[]) {
    time += gap
    if (!Number.isSafeInteger(time)) malformed(tag, 'member times exceed safe integers')
  }
  return value as unknown as ChunkRow
}

function expandRow(row: ChunkRow): SessionEvent[] {
  const members = row.type === 'tool-call-chunks' ? row.data.args : row.data.texts
  const events: SessionEvent[] = []
  let time = row.time0
  for (let index = 0; index < members.length; index += 1) {
    if (index > 0) time += row.data.dt[index - 1] as number
    let chunk: StreamChunk
    switch (row.type) {
      case 'text-chunks':
        chunk = { type: 'text-delta', index: row.data.index, text: members[index] as string }
        break
      case 'reasoning-chunks':
        chunk = { type: 'reasoning-delta', index: row.data.index, text: members[index] as string }
        break
      case 'tool-call-chunks':
        chunk = {
          type: 'tool-call-delta',
          index: row.data.index,
          id: row.data.id,
          ...Object.hasOwn(row.data, 'name') ? { name: row.data.name as string } : {},
          argumentsDelta: members[index] as string,
        }
        break
    }
    events.push({
      type: 'assistant/chunk',
      seq: row.seq0 + index,
      time,
      data: { turn: row.data.turn, step: row.data.step, chunk },
    })
  }
  return events
}

/**
 * Decode one scalar or packed schema-20 record.
 * @param value - parsed physical-record value.
 * @returns the represented logical events.
 */
export function decodeStorageRecord(value: unknown): SessionEvent[] {
  if (!isRecord(value)) return [value as SessionEvent]
  const tag = value.type
  if (tag !== 'text-chunks' && tag !== 'reasoning-chunks' && tag !== 'tool-call-chunks') {
    return [value as SessionEvent]
  }
  return expandRow(validateRow(value, tag))
}

/**
 * Decode one packed row from its exact uncompressed data value. The byte bound
 * rejects oversized input before JSON parsing and avoids serializing it again.
 * @param tag - validated packed physical type.
 * @param seq0 - first represented logical sequence number.
 * @param time0 - first represented logical timestamp.
 * @param serializedData - decoded SQLite data-column text.
 * @returns the represented logical events.
 */
export function decodeSerializedChunkRow(
  tag: ChunkRow['type'],
  seq0: number,
  time0: number,
  serializedData: string,
): SessionEvent[] {
  const bytes = Buffer.byteLength(serializedData)
  if (bytes > MAX_PACKED_DATA_BYTES) malformed(tag, `data exceeds ${MAX_PACKED_DATA_BYTES} UTF-8 bytes`)
  return expandRow(validateRow({ type: tag, seq0, time0, data: JSON.parse(serializedData) as unknown }, tag, bytes))
}
/* jscpd:ignore-end */
