/** Package-owned invariant companion for the Team tool adapter. */

import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-experimental-tool-agent-team'

/** Cordis companion plugin name. */
export const name = 'tool-team-invariant'
/** Invariant registry dependency. */
export const inject = ['invariants']

/** No runtime invariant: the Team service owns durable and authorization relations. */
const install: InvariantInstaller = () => {}

/** Register this package's invariant ownership. */
export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
