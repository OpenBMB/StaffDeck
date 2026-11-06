import { Context } from '@deepseek-ai/cordis'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import InvariantRegistry from '@deepseek-ai/dsh-invariants'
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session'
import { describe, expect, it } from 'vitest'
import { WebhookDeliveryId, WebhookRuleId, WebhookSourceId } from '../src/index.ts'
import * as WebhookInvariant from '../src/invariant.ts'

/** Install the invariant over one mutable Workspace projection. */
async function harness(): Promise<{
  ctx: Context
  workspaces: { path: string; sessionIds: readonly SessionId[] }[]
}> {
  const ctx = new Context()
  const workspaces: { path: string; sessionIds: readonly SessionId[] }[] = []
  ctx.provide('workspaceRegistry', { list: () => workspaces } as never)
  await ctx.plugin(SessionStore)
  await ctx.plugin(InvariantRegistry)
  await ctx.plugin(WebhookInvariant)
  return { ctx, workspaces }
}

/** Append one candidate webhook inbox insertion. */
function insert(ctx: Context, id: SessionId, cwd?: string): void {
  const session = ctx.sessions.create(id, { meta: { ...(cwd === undefined ? {} : { cwd }) } })
  session.append('agent/inbox/spliced', {
    target: 'next-turn',
    start: 0,
    inserted: [createUserMessage({
      content: [{ type: 'text', text: 'review' }],
      source: {
        kind: 'webhook',
        provider: 'github',
        source: WebhookSourceId('primary'),
        deliveryId: WebhookDeliveryId('delivery'),
        ruleId: WebhookRuleId('review'),
        form: 'notice',
        summary: 'review',
      },
    })],
  })
}

describe('webhook prompt invariant', () => {
  it('accepts prompt admission after matching Workspace attachment', async () => {
    const { ctx, workspaces } = await harness()
    const id = SessionId('attached')
    workspaces.push({ path: '/workspace', sessionIds: [id] })
    expect(() => { insert(ctx, id, '/workspace') }).not.toThrow()
    await ctx.fiber.dispose()
  })

  it('rejects a missing cwd, missing or duplicate Workspace, and path mismatch', async () => {
    const missingCwd = await harness()
    const noCwdId = SessionId('no-cwd')
    missingCwd.workspaces.push({ path: '/workspace', sessionIds: [noCwdId] })
    expect(() => { insert(missingCwd.ctx, noCwdId) }).toThrow(/has no cwd/)
    await missingCwd.ctx.fiber.dispose()

    const missing = await harness()
    expect(() => { insert(missing.ctx, SessionId('missing'), '/workspace') }).toThrow(/belongs to 0 Workspaces/)
    await missing.ctx.fiber.dispose()

    const duplicate = await harness()
    const duplicateId = SessionId('duplicate')
    duplicate.workspaces.push(
      { path: '/workspace', sessionIds: [duplicateId] },
      { path: '/workspace', sessionIds: [duplicateId] },
    )
    expect(() => { insert(duplicate.ctx, duplicateId, '/workspace') }).toThrow(/belongs to 2 Workspaces/)
    await duplicate.ctx.fiber.dispose()

    const mismatch = await harness()
    const mismatchId = SessionId('mismatch')
    mismatch.workspaces.push({ path: '/other', sessionIds: [mismatchId] })
    expect(() => { insert(mismatch.ctx, mismatchId, '/workspace') }).toThrow(/differs from its Workspace path/)
    await mismatch.ctx.fiber.dispose()
  })

  it('ignores non-webhook inbox messages', async () => {
    const { ctx } = await harness()
    const session = ctx.sessions.create(SessionId('human'), { meta: { cwd: '/workspace' } })
    expect(() => session.append('agent/inbox/spliced', {
      target: 'next-turn',
      start: 0,
      inserted: [createUserMessage({
        content: [{ type: 'text', text: 'hello' }],
        source: { kind: 'user' },
      })],
    })).not.toThrow()
    expect(() => session.append('todo/write', { todos: [] })).not.toThrow()
    await ctx.fiber.dispose()
  })
})
