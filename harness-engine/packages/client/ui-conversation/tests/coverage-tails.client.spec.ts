import { Context } from '@deepseek-ai/cordis'
import { describe, expect, it } from 'vitest'
import { apply as nodeApply } from '../src/index.ts'

describe('node apply tail', () => {
  it('tolerates a Host without settings', () => {
    expect(() => { nodeApply(new Context()) }).not.toThrow()
  })
})
