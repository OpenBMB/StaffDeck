/** Packed history records become one event-shaped Client value per wire record. */

import { describe, expect, it } from 'vitest'
import { ToolCallId } from '@deepseek-ai/dsh-llm/brand'
import type { SessionHistoryRecord } from '../src/types.ts'
import {
  historyEntries,
  historyRecordFirstSeq,
  historyRecordLastSeq,
} from '../src/client/sessions/history-records.ts'

describe('Session history record projection', () => {
  it('retains an ordinary event and its point cursor', () => {
    const ordinary: SessionHistoryRecord = {
      type: 'event',
      event: { type: 'turn/start', seq: 7, time: 1, data: { turn: 1 } },
    }

    const records = [ordinary]
    const [entry] = historyEntries(records)

    expect(historyEntries(records)).toBe(records)
    expect(entry).toBe(ordinary)
    expect(historyRecordFirstSeq(ordinary)).toBe(7)
    expect(entry?.event.time).toBe(1)
    expect(historyRecordLastSeq(ordinary)).toBe(7)
  })

  it('retains one packed text row without copying or reshaping it', () => {
    const packed: SessionHistoryRecord = {
      type: 'chunks',
      event: {
        type: 'chunkrow/text-chunks',
        seq: 11,
        time: 20,
        data: { turn: 1, step: 2, index: 0, dt: [1, 2, 3], texts: ['a', 'b', 'c', 'd'] },
      },
    }

    const [entry] = historyEntries([packed])
    if (entry?.type !== 'chunks') throw new Error('expected packed history entry')
    const { event } = entry

    expect(entry).toBe(packed)
    expect(event).toBe(packed.event)
    expect(historyRecordFirstSeq(packed)).toBe(11)
    expect(event.time).toBe(20)
    expect(historyRecordLastSeq(packed)).toBe(14)
  })

  it('preserves a packed tool-call row and optional-name absence', () => {
    const packed: SessionHistoryRecord = {
      type: 'chunks',
      event: {
        type: 'chunkrow/tool-call-chunks',
        seq: 20,
        time: 200,
        data: {
          turn: 2,
          step: 4,
          index: 1,
          id: ToolCallId('call-1'),
          dt: [2, 3],
          args: ['', '{"x":', '1}'],
        },
      },
    }

    const [entry] = historyEntries([packed])
    if (entry?.type !== 'chunks') throw new Error('expected packed history entry')
    const { event } = entry

    if (event.type !== 'chunkrow/tool-call-chunks') throw new Error('expected packed history event')
    expect(event).toBe(packed.event)
    expect(Object.hasOwn(event.data, 'name')).toBe(false)
    expect(historyRecordLastSeq(packed)).toBe(22)
  })
})
