/**
 * Package-owned invariant companion for `@deepseek-ai/dsh-client-ui-renderer`.
 * @module @deepseek-ai/dsh-client-ui-renderer/invariant
 */

/* jscpd:ignore-start */
/* oxlint-disable typescript/no-redundant-type-constituents --
 * `keyof SlotMap & string` is the declaration-merge key pattern: SlotMap is
 * empty in this compilation unit but consumers merge concrete keys into it. */
import type { Context } from '@deepseek-ai/cordis'
import type { SlotMap } from '@deepseek-ai/dsh-client-ui-slots'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-client-ui-renderer'

/** Cordis companion plugin name. */
export const name = 'client-ui-renderer-invariant'
/** Service required before the companion can reserve package ownership. */
export const inject = ['invariants']

/**
 * Verify that each `slots/changed` dispatch observes its mutation already
 * applied to the renderer-owned slot registry.
 */
const install: InvariantInstaller = (ctx, fail) => {
  ctx.on('internal/dispatch', (_mode, eventName, args) => {
    if (eventName !== 'slots/changed') return
    const key: unknown = args[0]
    if (typeof key !== 'string' || key === '') {
      fail("'slots/changed' dispatched without a slot key argument")
      return
    }
    const slots = ctx.get('slots')
    if (slots !== undefined && slots.getVersion(key as keyof SlotMap & string) === 0) {
      fail(`'slots/changed' fired for "${key}" before any mutation bumped its version — emission must follow the applied mutation`)
    }
  }, { global: true })
}

/**
 * Register this package's invariant companion.
 * @param ctx - Cordis context carrying the invariant service.
 * @returns the installed registration's disposer after setup succeeds.
 */
export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
/* jscpd:ignore-end */
