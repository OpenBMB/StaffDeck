/**
 * Package-owned invariant companion for `@deepseek-ai/dsh-client-locale`.
 * @module @deepseek-ai/dsh-client-locale/invariant
 */

/* jscpd:ignore-start */
import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-client-locale'

/** Cordis companion plugin name. */
export const name = 'client-locale-invariant'
/** Service required before the companion can reserve package ownership. */
export const inject = ['invariants']

/**
 * No runtime invariant: the locale catalog and dictionaries have no
 * independent runtime source to compare against; registration disposal,
 * preference resolution, and fallback lookup are asserted by behavior specs.
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
