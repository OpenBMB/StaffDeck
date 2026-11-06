import { describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import { credentialKey } from '@deepseek-ai/dsh-credentials'
import InvariantRegistry from '@deepseek-ai/dsh-invariants'
import AuthorizationService from '@deepseek-ai/dsh-authorization'
import * as AuthorizationInvariant from '../src/invariant.ts'
import { MemoryCredentials } from './memory.ts'

const KEY = credentialKey('llm-pi-ai', 'openai-codex')

describe('authorization invariant companion', () => {
  it('accepts an attempt that released its key before settling', async () => {
    const ctx = new Context()
    await ctx.plugin(InvariantRegistry)
    await ctx.plugin(AuthorizationInvariant)
    await ctx.plugin(MemoryCredentials)
    await ctx.plugin(AuthorizationService)
    ctx.authorization.registerFlow({
      key: KEY,
      label: 'ChatGPT (Codex)',
      methods: [{ id: 'oauth', label: 'Sign in' }],
      run: () => ctx.credentials
        .modifyRecord(KEY, () => Promise.resolve({ kind: 'grant', payload: {} }))
        .then(() => undefined),
    })

    await expect(ctx.authorization.begin({
      key: KEY,
      interaction: { notify: () => {}, prompt: () => Promise.reject(new Error('unused')) },
    })).resolves.toEqual({ status: 'authorized' })
  })

  it('fails a settlement that left its key in flight', async () => {
    const ctx = new Context()
    await ctx.plugin(InvariantRegistry)
    await ctx.plugin(AuthorizationInvariant)
    await ctx.plugin(MemoryCredentials)
    await ctx.plugin(AuthorizationService)
    const started = Promise.withResolvers<undefined>()
    ctx.authorization.registerFlow({
      key: KEY,
      label: 'ChatGPT (Codex)',
      methods: [{ id: 'oauth', label: 'Sign in' }],
      run: () => {
        started.resolve(undefined)
        return new Promise(() => {})
      },
    })
    void ctx.authorization.begin({
      key: KEY,
      interaction: { notify: () => {}, prompt: () => Promise.reject(new Error('unused')) },
    })
    await started.promise

    expect(() => { ctx.emit('authorization/settled', KEY, 'authorized') })
      .toThrow(/left the key in flight/)
  })

  it('fails a settlement emitted without a live service', async () => {
    const ctx = new Context()
    await ctx.plugin(InvariantRegistry)
    await ctx.plugin(AuthorizationInvariant)

    expect(() => { ctx.emit('authorization/settled', KEY, 'cancelled') })
      .toThrow(/without a live authorization service/)
  })

  it('accepts a settlement whose flow left during its own attempt', async () => {
    const ctx = new Context()
    await ctx.plugin(InvariantRegistry)
    await ctx.plugin(AuthorizationInvariant)
    await ctx.plugin(MemoryCredentials)
    await ctx.plugin(AuthorizationService)

    expect(() => { ctx.emit('authorization/settled', KEY, 'cancelled') }).not.toThrow()
  })

  it('reserves the package name against duplicate registration', async () => {
    const ctx = new Context()
    await ctx.plugin(InvariantRegistry)
    await ctx.plugin(AuthorizationInvariant)

    expect(() => {
      ctx.invariants.register('@deepseek-ai/dsh-authorization', () => {})
    }).toThrow(/already registered/)
  })
})
