/** Package-owned invariant companion for the GitHub webhook adapter. */

import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-webhook-github'

/** Cordis invariant-companion plugin name. */
export const name = 'webhook-github-invariant'
/** Registry required before reserving this package's invariant ownership. */
export const inject = ['invariants']

/**
 * No runtime invariant: authentication and input validation occur at the exact
 * HTTP operation; dsh-host-webserver owns route/disposer symmetry.
 */
const install: InvariantInstaller = () => {}

/**
 * Register this package's explained empty invariant.
 * @param ctx - Cordis context carrying the invariant registry.
 * @returns the invariant registration disposer.
 */
export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
