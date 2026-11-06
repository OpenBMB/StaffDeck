/**
 * Package-owned invariant companion for `@deepseek-ai/dsh-client-connection`.
 * @module @deepseek-ai/dsh-client-connection/invariant
 */

/* jscpd:ignore-start */
import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-client-connection'

/** Cordis companion plugin name. */
export const name = 'client-connection-invariant'
/** Service required before the companion can reserve package ownership. */
export const inject = ['invariants']

/**
 * No runtime invariant: browser-session verification reads the credential
 * record asynchronously at the request that authorizes work, while the
 * credentials companion owns record commit-event lifetime. Stream/reconnect
 * sequencing and rpcId round-trip discipline are exercised directly by
 * behavior specs, and route register/dispose symmetry is
 * audited by the webserver companion.
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
