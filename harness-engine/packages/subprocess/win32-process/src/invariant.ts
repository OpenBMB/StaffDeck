/** Package-owned invariant companion for `@deepseek-ai/dsh-win32-process`. */

/* jscpd:ignore-start */
import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-win32-process'

export const name = 'win32-process-invariant'
export const inject = ['invariants']

/** No runtime invariant: operations own only call-local native handles. */
const install: InvariantInstaller = () => {}

export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
/* jscpd:ignore-end */
