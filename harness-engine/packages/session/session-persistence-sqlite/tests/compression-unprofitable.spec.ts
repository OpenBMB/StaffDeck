import { describe, expect, it, vi } from 'vitest'
import type { SessionEvent } from '@deepseek-ai/dsh-session'

vi.mock('node:zlib', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:zlib')>()
  return {
    ...actual,
    zstdCompressSync: (input: ArrayBufferView) => Buffer.alloc(input.byteLength + 1),
  }
})

import { bindRecord } from '../src/compression.ts'

describe('SQLite compression fallback', () => {
  it('keeps data as text when its Zstandard frame is not smaller', () => {
    const event = {
      type: 'assistant/message',
      seq: 0,
      time: 1,
      data: { text: 'x' },
    } as unknown as SessionEvent

    expect(typeof bindRecord(event).data).toBe('string')
  })
})
