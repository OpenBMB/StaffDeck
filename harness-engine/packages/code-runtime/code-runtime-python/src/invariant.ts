/**
 * Package-owned invariant companion for `@deepseek-ai/dsh-code-runtime-python`.
 * @module @deepseek-ai/dsh-code-runtime-python/invariant
 */

/* jscpd:ignore-start */
import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-code-runtime-python'

/** Cordis companion plugin name. */
export const name = 'code-runtime-python-invariant'
/** Service required before the companion can reserve package ownership. */
export const inject = ['invariants']

/**
 * No runtime invariant: this package ships only the fd-3 wire-protocol codec and its Python mirror,
 * exposing no runtime event sequence or mutable data relation; `protocol.spec.ts` and
 * `protocol-mirror.e2e.ts` cover the protocol's behavior.
 */
const install: InvariantInstaller = () => {}

/**
 * Register this package's invariant companion.
 * @param ctx - Cordis context carrying the invariant service.
 * @returns the installed registration's disposer after setup succeeds.
 */
export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
/* jscpd:ignore-end */
