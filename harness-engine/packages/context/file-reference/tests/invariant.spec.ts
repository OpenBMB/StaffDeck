import { Context } from '@deepseek-ai/cordis'
import { describe, expect, it } from 'vitest'
import InvariantService from '@deepseek-ai/dsh-invariants'
import * as FileReferenceInvariant from '../src/invariant.ts'

describe('invariant companion', () => {
  it('registers the stateless seam under its package name', async () => {
    const ctx = new Context()
    await ctx.plugin(InvariantService, { enabled: true })
    await expect(ctx.plugin(FileReferenceInvariant).await()).resolves.toBeDefined()
  })
})
