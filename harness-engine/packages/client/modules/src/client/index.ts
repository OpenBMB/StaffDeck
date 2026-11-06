/**
 * Browser half (the standard `./client` export): the module-system class and
 * wire contract, plus the enrollment plugin face. The module system itself is
 * built by the shell kernel BEFORE cordis exists (the bootstrap exception —
 * the mechanism that loads plugins cannot arrive through itself). The host
 * parser-preloads this ordinary client bundle into the pending registration
 * queue. The HTML-installed loader facade materializes this bundle and calls
 * its bootstrap export, which constructs the system and retains the same
 * exports for this package's graph row. The plugin face only enrolls that
 * pre-existing instance by providing it as `ctx.modules`.
 * @module @deepseek-ai/dsh-client-modules/client
 */
import type { Context } from '@deepseek-ai/cordis'
import { ClientModuleSystem } from './system.ts'
import { parseBootManifest } from './manifest.ts'
import type {
  ClientBootstrapModule, ClientModuleCreateOptions, ClientModuleLoaderTarget,
} from './manifest.ts'

export { ClientModuleSystem }
export { parseBootManifest, stripClientSuffix } from './manifest.ts'
export type {
  BootManifest, BootModuleRow, BootPluginRow, ClientBootstrapModule, ClientBundleRegistration,
  ClientModuleCreateOptions, ClientModuleLoader, ClientModuleLoaderTarget, ClientModuleRecord,
  ClientModuleSystemOptions, DshWindow,
  WebBootEntry, WebBootGraph,
} from './manifest.ts'

let moduleSystem: ClientModuleSystem | undefined

/**
 * Build the live module system from the HTML facade's materialized modules bundle.
 * @param target - Stable registration facade whose pending queue becomes the live sink.
 * @param bootstrapModule - This bundle's id and already-materialized exports.
 * @param options - Raw boot graph, platform seed, and optional bundle transport.
 * @returns The created module system, also published for this package's Cordis plugin face.
 */
export function createClientModuleSystem(
  target: ClientModuleLoaderTarget,
  bootstrapModule: ClientBootstrapModule,
  options: ClientModuleCreateOptions,
): ClientModuleSystem {
  moduleSystem = new ClientModuleSystem({
    manifest: parseBootManifest(options.boot),
    staticModules: options.staticModules,
    registrationTarget: target,
    bootstrapModule,
    ...(options.loadBundle === undefined ? {} : { loadBundle: options.loadBundle }),
  })
  return moduleSystem
}

/**
 * Enroll the kernel-built module system as `ctx.modules`.
 * @param ctx - client root context.
 */
export function apply(ctx: Context): void {
  if (moduleSystem === undefined) {
    throw new Error('client-modules: createClientModuleSystem must run before plugin boot')
  }
  ctx.reflect.provide('modules', moduleSystem)
}
