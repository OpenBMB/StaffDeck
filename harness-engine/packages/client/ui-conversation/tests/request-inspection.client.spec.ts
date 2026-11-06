import { describe, expect, it } from 'vitest'
import type { SessionEvent } from '@deepseek-ai/dsh-session/types'
import { inspectRequestPrompt } from '../src/client/contract/request-inspection.ts'

const CONFIG = { provider: 'test', model: 'test' }

function header(
  seq: number,
  reason: SessionEvent<'request/header'>['data']['reason'],
  value: SessionEvent<'request/header'>['data']['header'],
): SessionEvent<'request/header'> {
  return {
    type: 'request/header',
    seq,
    time: 1_700_000_000_000 + seq,
    data: { reason, header: value },
  }
}

describe('inspectRequestPrompt', () => {
  it('classifies the first complete header as the initial prompt', () => {
    expect(inspectRequestPrompt(undefined, header(1, 'initial', {
      config: CONFIG,
      system: '# System\n\nFollow instructions.',
      tools: [{ name: 'read', description: 'Read a file', parameters: { type: 'object' } }],
    }))).toMatchObject({
      prompt: {
        config: CONFIG,
        system: '# System\n\nFollow instructions.',
        tools: [{ name: 'read' }],
      },
      change: { seq: 1, time: 1_700_000_000_001, kind: 'initial' },
    })
  })

  it('suppresses a resume header when the earlier prompt is outside the loaded window', () => {
    expect(inspectRequestPrompt(undefined, header(2, 'resume', {
      config: CONFIG,
      system: 'same prompt',
    }))).toEqual({
      prompt: { config: CONFIG, system: 'same prompt', tools: [] },
    })
  })

  it('classifies system, tool, and combined changes against the previous prompt', () => {
    const initial = inspectRequestPrompt(undefined, header(1, 'initial', {
      config: CONFIG,
      system: 'first',
      tools: [{ name: 'read', description: 'Read', parameters: { type: 'object' } }],
    })).prompt
    const system = inspectRequestPrompt(initial, header(2, 'change', {
      config: CONFIG,
      system: 'second',
      tools: [...initial.tools],
    }))
    const tools = inspectRequestPrompt(system.prompt, header(3, 'change', {
      config: CONFIG,
      system: 'second',
      tools: [{ name: 'write', description: 'Write', parameters: { type: 'object' } }],
    }))
    const combined = inspectRequestPrompt(tools.prompt, header(4, 'change', {
      config: CONFIG,
      system: 'third',
      tools: [],
    }))

    expect(system.change?.kind).toBe('system')
    expect(tools.change?.kind).toBe('tools')
    expect(combined.change?.kind).toBe('system-and-tools')
    expect(combined.change?.previous).toBe(tools.prompt)
  })

  it('omits a change when the prompt and tools are unchanged', () => {
    const previous = inspectRequestPrompt(undefined, header(1, 'initial', {
      config: CONFIG,
      system: 'same',
    })).prompt

    expect(inspectRequestPrompt(previous, header(2, 'resume', {
      config: { ...CONFIG, maxTokens: 1_024 },
      system: 'same',
    }))).toEqual({
      prompt: { config: { ...CONFIG, maxTokens: 1_024 }, system: 'same', tools: [] },
    })
  })
})
