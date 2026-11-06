import { describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import InvariantRegistry from '@deepseek-ai/dsh-invariants'
import * as SettingsControllerInvariant from '../src/invariant.ts'

describe('api-settings-controller invariant companion', () => {
  it('reserves the package name against duplicate registration', async () => {
    const ctx = new Context()
    await ctx.plugin(InvariantRegistry)
    await ctx.plugin(SettingsControllerInvariant)

    expect(() => {
      ctx.invariants.register('@deepseek-ai/dsh-api-settings-controller', () => {})
    }).toThrow(/already registered/)
  })
})
