/**
 * Loader fixture that holds the report child until its parent's spawn turn ends,
 * then parks the parent until child settlement follows the report.
 * @module subagent-report-fence
 */

import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent-loop'

/** Fixture plugin name. */
export const name = 'subagent-report-fence'

/**
 * Keep replay scheduling from folding settlement into the parent's first turn
 * or starting a second parent request between report and settlement.
 * @param ctx - assembled ACP-agent context.
 */
export function apply(ctx: Context): void {
  const childReady = Promise.withResolvers<undefined>()
  const parentStopped = Promise.withResolvers<undefined>()
  const childSettled = Promise.withResolvers<undefined>()
  let hasStopped = false
  let parentMaintenance: Promise<void> | undefined

  ctx.effect(() => {
    const disposeSession = ctx.root.on('session/event', (session, event) => {
      if (session.header.parentSession !== undefined || event.type !== 'turn/end' || event.data.turn !== 1) return
      hasStopped = true
      parentStopped.resolve(undefined)
    })
    const disposeStatus = ctx.root.on('agent/status', ({ agent, status }) => {
      if (
        agent.session.header.parentSession === undefined &&
        status === 'idle' &&
        hasStopped &&
        parentMaintenance === undefined
      ) {
        parentMaintenance = agent.runMaintenance(async () => {
          await childSettled.promise
        })
      }
    })
    const disposeInbox = ctx.root.on('agent/inbox/inserted', ({ agent, message }) => {
      if (
        agent.session.header.parentSession === undefined &&
        message.source.kind === 'subagent-settled'
      ) {
        childSettled.resolve(undefined)
      }
    })
    const disposeStep = ctx.root.on('agent/pre-step', async ({ agent, turn, step }, next) => {
      if (agent.session.header.parentSession !== undefined) {
        childReady.resolve(undefined)
        if (!hasStopped) await parentStopped.promise
      } else if (turn === 1 && step === 2) {
        await childReady.promise
      }
      return next()
    })
    return () => {
      childSettled.resolve(undefined)
      disposeStep()
      disposeInbox()
      disposeStatus()
      disposeSession()
    }
  }, 'subagent-report-fence.listeners')
}
